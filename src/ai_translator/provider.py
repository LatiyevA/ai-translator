import errno
import json
import os
import re
import select
import shutil
import signal
import socket
import subprocess
import tomllib
from http.client import HTTPConnection, HTTPException, HTTPSConnection, IncompleteRead
from io import StringIO
from pathlib import Path
from queue import Empty, Queue
from tempfile import TemporaryDirectory, TemporaryFile
from threading import BoundedSemaphore, Thread, Timer
from time import monotonic
from urllib.error import HTTPError
from urllib.request import HTTPHandler, HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from .config import Model
from .posix_job import run_posix_supervised
from .windows_job import run_supervised


class ProviderError(ValueError):
    pass


class AuthenticationError(ProviderError):
    pass


class InvalidStructuredOutput(ProviderError):
    pass


class ProviderRefusal(ProviderError):
    pass


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_DNS_SLOTS = BoundedSemaphore(2)


class _ResponseDeadline:
    def __init__(self, timeout: int):
        self.end = monotonic() + timeout
        self.socket = None
        self.timer = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if self.timer is not None:
            self.timer.cancel()
            self.timer.join()
        if self.socket is not None:
            self.socket.close()

    def expired(self) -> bool:
        return monotonic() >= self.end

    def remaining(self) -> float:
        delay = self.end - monotonic()
        if delay <= 0:
            raise TimeoutError
        return delay

    def _resolve(self, address):
        # System DNS cannot be cancelled portably. Bound the wait and the number of stranded lookups.
        slots = _DNS_SLOTS
        if not slots.acquire(timeout=self.remaining()):
            raise TimeoutError
        completed = Queue(maxsize=1)

        def resolve():
            try:
                completed.put(socket.getaddrinfo(*address, type=socket.SOCK_STREAM))
            except Exception as exc:
                completed.put(exc)
            finally:
                slots.release()

        try:
            Thread(target=resolve, daemon=True).start()
        except RuntimeError:
            slots.release()
            raise OSError("Не удалось запустить поиск адреса OpenAI API.") from None
        try:
            result = completed.get(timeout=self.remaining())
        except Empty:
            raise TimeoutError from None
        if isinstance(result, OSError):
            raise result
        if isinstance(result, Exception):
            raise OSError("Не удалось определить адрес OpenAI API.") from result
        return result

    def dial(self, address, _timeout, source_address):
        addresses = self._resolve(address)
        last_error = None
        for family, kind, protocol, _, remote in addresses:
            delay = self.remaining()
            peer = None
            try:
                peer = socket.socket(family, kind, protocol)
                peer.settimeout(delay)
                if source_address:
                    peer.bind(source_address)
                peer.connect(remote)
            except OSError as exc:
                if peer is not None:
                    peer.close()
                last_error = exc
                continue
            except BaseException:
                if peer is not None:
                    peer.close()
                raise
            try:
                delay = self.remaining()
                # A duplicate remains valid through proxy CONNECT, TLS wrapping and urllib header handling.
                self.socket = socket.socket(fileno=socket.dup(peer.fileno()))
                timer = Timer(delay, self._interrupt_read)
                timer.daemon = True
                timer.start()
                self.timer = timer
                return peer
            except BaseException:
                peer.close()
                raise
        raise last_error or OSError("Нет доступных адресов для OpenAI API.")

    def connection_factory(self, connection_type):
        def make_connection(*args, **kwargs):
            connection = connection_type(*args, **kwargs)
            connection._create_connection = self.dial
            connect = connection.connect

            def tracked_connect():
                connect()
                self.remaining()

            connection.connect = tracked_connect
            return connection
        return make_connection

    def _interrupt_read(self):
        try:
            self.socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


class _DeadlineHandler(HTTPHandler, HTTPSHandler):
    def __init__(self, deadline: _ResponseDeadline):
        super().__init__()
        self.deadline = deadline

    def http_open(self, request):
        return self.do_open(self.deadline.connection_factory(HTTPConnection), request)

    def https_open(self, request):
        return self.do_open(self.deadline.connection_factory(HTTPSConnection), request, context=self._context)


def _api_open(request: Request, timeout: int, *, deadline: _ResponseDeadline):
    return build_opener(_NoRedirects(), _DeadlineHandler(deadline)).open(request, timeout=timeout)


_MAX_API_RESPONSE = 16 * 1024 * 1024
_MAX_CLI_STDOUT = 16 * 1024 * 1024
_MAX_CLI_STDERR = 2 * 1024 * 1024
_MAX_CLI_AUTH = 256 * 1024
_UNDECODABLE_UTF8 = re.compile(r"[\udc80-\udcff]")


def _read_cli_pipe(stream, name: str, limit: int, provider: str, completed: Queue) -> None:
    data = bytearray()
    try:
        while chunk := stream.read(min(65536, limit + 1 - len(data))):
            data.extend(chunk)
            if len(data) > limit:
                raise ProviderError(f"{provider}: поток {name} превышает допустимый объём.")
    except (OSError, ValueError) as exc:
        completed.put((name, exc))
    else:
        completed.put((name, data))


def _cli_exited(process: subprocess.Popen, *, own_group: bool) -> bool:
    # Keep the group leader's PID reserved until the group has been signalled.
    if own_group and hasattr(os, "waitid"):
        return os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
    if own_group and hasattr(select, "kqueue"):
        event = select.kevent(process.pid, filter=select.KQ_FILTER_PROC, flags=select.KQ_EV_ADD,
                              fflags=select.KQ_NOTE_EXIT)
        with select.kqueue() as queue:
            try:
                notices = queue.control([event], 1, 0)
            except ProcessLookupError:
                return True
        for notice in notices:
            if notice.flags & select.KQ_EV_ERROR and notice.data != errno.ESRCH:
                raise OSError(notice.data, os.strerror(notice.data))
        return bool(notices)
    return process.poll() is not None


def _stop_cli_group(group: int) -> None:
    try:
        os.killpg(group, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _run_cli(args, *, provider: str, env: dict[str, str], timeout: int, input: str = "",
             cwd=None, errors: str = "strict", stderr_errors: str | None = None,
             auth: bool = False):
    own_group = os.name == "posix"
    deadline = monotonic() + timeout

    def execute(command, options, *, status_fd: int | None = None):
        if monotonic() >= deadline:
            raise subprocess.TimeoutExpired(args, timeout)
        status_watch = None
        if status_fd is not None:
            status_watch = select.poll()
            status_watch.register(status_fd, select.POLLIN)
        # A regular file supplies stdin while both output pipes drain independently on every platform.
        with TemporaryFile() as prompt:
            prompt.write(input.encode("utf-8"))
            prompt.seek(0)
            with subprocess.Popen(command, stdin=prompt, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  cwd=cwd, env=env, bufsize=0, **options) as process:
                completed = Queue()
                captured = {}
                group = process.pid if own_group else None
                exited = False
                provider_status = None
                try:
                    for stream, name, limit in ((process.stdout, "stdout", _MAX_CLI_STDOUT),
                                                (process.stderr, "stderr", _MAX_CLI_STDERR)):
                        Thread(target=_read_cli_pipe, daemon=True,
                               args=(stream, name, _MAX_CLI_AUTH if auth else limit, provider, completed)).start()
                    while True:
                        if not exited and status_watch is not None and status_watch.poll(0):
                            provider_status = int(os.read(status_fd, 32))
                            exited = True
                        if not exited and (own_group or len(captured) == 2):
                            exited = _cli_exited(process, own_group=own_group)
                        if exited and group is not None:
                            _stop_cli_group(group)
                            group = None
                        if exited and len(captured) == 2:
                            break
                        remaining = deadline - monotonic()
                        if remaining <= 0:
                            raise subprocess.TimeoutExpired(args, timeout)
                        try:
                            name, result = completed.get(timeout=min(remaining, 0.05))
                        except Empty:
                            continue
                        if isinstance(result, ProviderError):
                            raise result
                        if isinstance(result, (OSError, ValueError)):
                            raise ProviderError(f"Не удалось прочитать {name} процесса {provider}.") from result
                        captured[name] = result
                except BaseException:
                    if not own_group and process.poll() is None:
                        process.kill()
                    raise
                finally:
                    if group is not None:
                        _stop_cli_group(group)
                launcher_status = process.wait()
                status = launcher_status if provider_status is None else provider_status
                return subprocess.CompletedProcess(args, status, captured["stdout"].decode("utf-8", errors),
                                                   captured["stderr"].decode("utf-8", stderr_errors or errors))

    if os.name == "nt":
        return run_supervised(args, execute)
    if own_group:
        return run_posix_supervised(args, execute)
    return execute(args, {})


def _token_count(value: object) -> int:
    return value if type(value) is int and value >= 0 else 0


def _cli_environment(config_var: str = "") -> dict[str, str]:
    runtime_vars = {
        "PATH", "HOME", "USER", "LOGNAME", config_var, "TMPDIR", "LANG", "LC_ALL",
        "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "DBUS_SESSION_BUS_ADDRESS",
        "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "SSL_CERT_FILE", "SSL_CERT_DIR",
        "SYSTEMROOT", "WINDIR", "APPDATA", "LOCALAPPDATA", "USERPROFILE", "TMP", "TEMP",
    }
    return {key: value for key, value in os.environ.items() if key.upper() in runtime_vars}

class Usage:
    def __init__(self) -> None:
        self.input = self.output = self.cached_input = 0

    def add(self, data: dict) -> None:
        if not isinstance(data, dict):
            return
        details = data.get("input_tokens_details")
        cached = details.get("cached_tokens") if isinstance(details, dict) else None
        for attribute, value in (
            ("input", data.get("input_tokens")),
            ("output", data.get("output_tokens")),
            ("cached_input", data.get("cached_input_tokens", cached)),
        ):
            if type(value) is int and value >= 0:
                setattr(self, attribute, getattr(self, attribute) + value)


def _decode(text: str) -> dict:
    try:
        result = json.loads(text)
    except (ValueError, RecursionError) as exc:
        raise InvalidStructuredOutput("Модель вернула некорректный JSON.") from exc
    if not isinstance(result, dict):
        raise InvalidStructuredOutput("Ответ модели должен быть объектом JSON.")
    return result


def _decode_envelope(text: str, provider: str) -> dict:
    try:
        return _decode(text)
    except InvalidStructuredOutput as exc:
        raise ProviderError(f"{provider} вернул некорректный служебный ответ.") from exc


def _codex_failure(detail: str, default: str = "Codex завершился без результата.") -> str:
    detail = detail.casefold()
    if any(phrase in detail for phrase in ("quota", "rate limit", "usage limit", "too many requests")):
        return "Достигнут лимит Codex."
    if any(phrase in detail for phrase in ("unexpected argument", "unknown option", "unrecognized option")):
        return "Версия Codex CLI не поддерживает параметры запуска переводчика."
    if "model" in detail and any(phrase in detail for phrase in ("not supported", "not available", "not found")):
        return "Выбранная модель недоступна в Codex."
    return default


class Codex:
    def __init__(self, timeout: int):
        self.timeout = timeout
        self.usage = Usage()
        self.executable = shutil.which("codex")
        if not self.executable:
            raise ProviderError("Установите Codex CLI и выполните codex login.")
        self.env = _cli_environment("CODEX_HOME")
        auth_config = Path(self.env.get("CODEX_HOME") or Path.home() / ".codex") / "config.toml"
        credentials = "file"
        if auth_config.is_file():
            with auth_config.open("rb") as stream:
                credentials = tomllib.load(stream).get("cli_auth_credentials_store", "file")
        if credentials not in ("file", "keyring", "auto"):
            raise ProviderError("Для перевода нужен постоянный вход Codex через ChatGPT.")
        self.auth_override = f"cli_auth_credentials_store={json.dumps(credentials)}"

    def check(self) -> None:
        try:
            status = _run_cli(
                [self.executable, "-c", self.auth_override, "login", "status"], provider="Codex", env=self.env,
                errors="replace", timeout=min(self.timeout, 20), auth=True,
            )
        except subprocess.TimeoutExpired as exc:
            raise ProviderError("Codex не ответил при проверке авторизации.") from exc
        lines = [line.strip().removeprefix("error:").strip()
                 for line in f"{status.stdout}\n{status.stderr}".casefold().splitlines()]
        if not status.returncode and any(line.startswith("logged in using chatgpt") for line in lines):
            return
        if any(line.startswith(("not logged in", "not signed in", "login required")) for line in lines) or (
            not status.returncode and any(line.startswith("logged in using ") for line in lines)
        ):
            raise AuthenticationError("Требуется вход через ChatGPT. Выполните codex login.")
        raise ProviderError(_codex_failure(" ".join(lines), "Не удалось подтвердить вход через Codex."))

    def complete(self, model: Model, instructions: str, data: str, schema: dict) -> dict:
        with TemporaryDirectory(prefix="ai-translator-") as directory:
            directory = Path(directory).resolve()
            schema_path = directory / "schema.json"
            instructions_path = directory / "instructions.txt"
            schema_path.write_text(json.dumps(schema), encoding="utf-8")
            instructions_path.write_text(instructions, encoding="utf-8")
            args = [
                self.executable, "--ask-for-approval", "never", "exec",
                "--cd", str(directory), "--skip-git-repo-check", "--ephemeral",
                "--ignore-user-config", "--ignore-rules", "--sandbox", "read-only",
                "--disable", "shell_tool", "--disable", "multi_agent", "--disable", "multi_agent_v2",
                "--disable", "apps", "--disable", "plugins", "--disable", "hooks",
                "--disable", "remote_plugin", "--disable", "image_generation", "--disable", "goals",
                "--disable", "skill_search", "--enable", "skip_host_skill_discovery",
                "-c", 'web_search="disabled"', "--json",
                "-c", f"model_instructions_file={json.dumps(str(instructions_path), ensure_ascii=False)}",
                "-c", "project_doc_max_bytes=0",
                "-c", self.auth_override,
                "--output-schema", str(schema_path),
                "--model", model.name,
            ]
            if model.reasoning:
                args += ["-c", f"model_reasoning_effort={json.dumps(model.reasoning)}"]
            args.append("-")
            try:
                result = _run_cli(
                    args, input=data, provider="Codex", env=self.env,
                    # Preserve invalid bytes so a damaged line cannot hide a valid failure event.
                    errors="surrogateescape", stderr_errors="replace", timeout=self.timeout,
                )
            except subprocess.TimeoutExpired as exc:
                raise ProviderError(f"Codex не ответил за {self.timeout} с.") from exc
            detail = ""
            failed = False
            turn_started = turn_completed = False
            last_message = None
            malformed_stream = False
            damaged_utf8 = False
            for line in StringIO(result.stdout):
                if not line.strip():
                    continue
                if _UNDECODABLE_UTF8.search(line):
                    damaged_utf8 = True
                    continue
                try:
                    event = json.loads(line)
                except (ValueError, RecursionError):
                    malformed_stream = True
                    continue
                if not isinstance(event, dict):
                    malformed_stream = True
                    continue
                kind = event.get("type")
                if not isinstance(kind, str) or not kind:
                    malformed_stream = True
                    continue
                if kind == "turn.failed":
                    failed = True
                    error = event.get("error")
                    if isinstance(error, dict) and isinstance(error.get("message"), str):
                        detail = error["message"]
                elif kind == "turn.started":
                    if turn_started:
                        malformed_stream = True
                    turn_started = True
                elif kind == "turn.completed":
                    if not turn_started or turn_completed:
                        malformed_stream = True
                        continue
                    turn_completed = True
                    self.usage.add(event.get("usage"))
                elif kind == "item.completed" and turn_started and not turn_completed:
                    item = event.get("item")
                    item_type = item.get("type") if isinstance(item, dict) else None
                    if not isinstance(item_type, str) or not item_type:
                        malformed_stream = True
                    elif item_type == "agent_message":
                        last_message = item.get("text")
                        if not isinstance(last_message, str):
                            malformed_stream = True
            if failed or result.returncode:
                if not detail:
                    detail = next(
                        (line for line in StringIO(result.stderr) if line.startswith("error:")), ""
                    )
                raise ProviderError(_codex_failure(detail))
            if damaged_utf8:
                raise ProviderError("Ответ Codex содержит повреждённый UTF-8.")
            if malformed_stream or not turn_completed or not isinstance(last_message, str):
                raise ProviderError("Codex не вернул завершённый ответ в потоке JSONL.")
            return _decode(last_message)


class Antigravity:
    _AUTH_FAILURES = (
        "authentication required", "authentication failed", "authentication interrupted",
        "authentication timed out", "authentication expired", "login required", "not signed in", "not authenticated",
    )

    def __init__(self, timeout: int):
        self.timeout = timeout
        self.usage = Usage()
        executable = shutil.which("agy")
        if not executable:
            raise ProviderError("Установите Antigravity CLI и войдите в личный аккаунт Google через agy.")
        self.executable = os.path.abspath(executable)
        self.env = _cli_environment()

    def _run(self, directory: Path, flags: list[str], prompt: str, *, auth: bool = False) -> tuple[dict | None, bool]:
        args = [
            self.executable, "--input-format", "stream-json", "--output-format", "stream-json",
            "--log-file", str(directory / "agy.log"), "--disable-slash-commands",
            "--settings", json.dumps({
                "toolPermission": "strict", "agentMode": "", "allowNonWorkspaceAccess": False,
                "modelProvider": "", "useG1Credits": False,
            }),
            *flags,
        ]
        timeout = min(self.timeout, 10) if auth else self.timeout
        try:
            result = _run_cli(args, provider="Antigravity", env=self.env, cwd=directory, input=prompt,
                              timeout=timeout, auth=auth, stderr_errors="replace")
        except subprocess.TimeoutExpired as exc:
            if auth:
                raise ProviderError(f"Проверка входа через Antigravity не завершилась за {timeout} с.") from exc
            raise ProviderError(f"Antigravity не ответил за {timeout} с.") from exc
        except UnicodeDecodeError as exc:
            raise ProviderError("Ответ Antigravity содержит повреждённый UTF-8.") from exc

        diagnostic = result.stderr.casefold()
        if auth and any(phrase in diagnostic for phrase in self._AUTH_FAILURES):
            self._fail(diagnostic)
        authenticated = auth and any(line.strip() == "authentication successful" for line in diagnostic.splitlines())
        reply = None
        for line in StringIO(result.stdout):
            if not line.strip():
                continue
            try:
                event = _decode_envelope(line, "Antigravity")
            except ProviderError:
                if result.returncode:
                    self._fail(diagnostic)
                raise
            event_type = event.get("event")
            if not isinstance(event_type, str) or not event_type:
                raise ProviderError("В потоке Antigravity отсутствует тип события.")
            if auth and event_type == "init":
                init = event.get("init")
                conversation = event.get("conversation_id")
                authenticated |= (isinstance(init, dict) and isinstance(init.get("cwd"), str)
                                  and isinstance(conversation, str) and bool(conversation))
            if event_type == "result":
                if reply is not None or not isinstance(event.get("result"), dict):
                    raise ProviderError("Antigravity вернул некорректное завершающее событие.")
                reply = event["result"]
        if reply is not None:
            error = reply.get("error")
            reason = reply.get("finished_reason")
            if isinstance(error, str):
                diagnostic += " " + error.casefold()
            if result.returncode or reply.get("status") != "SUCCESS" or reason or error:
                self._fail(diagnostic, reason)
        if result.returncode:
            self._fail(diagnostic)
        return reply, authenticated

    @classmethod
    def _fail(cls, diagnostic: str, reason: object = None) -> None:
        if any(phrase in diagnostic for phrase in cls._AUTH_FAILURES):
            raise AuthenticationError("Войдите в личный аккаунт Google через agy.")
        if reason == "print_timeout":
            raise ProviderError("Истекло время ожидания ответа Antigravity.")
        if reason == "interrupted":
            raise ProviderError("Antigravity прервал выполнение запроса.")
        if any(phrase in diagnostic for phrase in ("provided but not defined", "unknown flag", "unknown option")):
            raise ProviderError("Обновите Antigravity CLI до версии с поддержкой настроек запуска и JSONL.")
        if any(phrase in diagnostic for phrase in ("quota", "rate limit", "out of credits", "resource_exhausted")):
            raise ProviderError("Достигнут лимит Antigravity.")
        if "model" in diagnostic and any(phrase in diagnostic for phrase in ("not found", "not available", "unknown")):
            raise ProviderError("Выбранная модель недоступна в Antigravity.")
        raise ProviderError("Antigravity завершился без результата.")

    def check(self) -> bool:
        # Empty stdin can end a session without reporting its login state.
        with TemporaryDirectory(prefix="ai-translator-agy-") as directory:
            _, authenticated = self._run(Path(directory).resolve(), [], "", auth=True)
        return authenticated

    def complete(self, model: Model, instructions: str, data: str, schema: dict) -> dict:
        with TemporaryDirectory(prefix="ai-translator-agy-") as directory:
            directory = Path(directory).resolve()
            agents = directory / ".agents" / "agents"
            agents.mkdir(parents=True)
            agent = "ai-translator-literary-translation"
            (agents / f"{agent}.md").write_text(
                "---\n"
                f"name: {agent}\n"
                "description: Literary translation and editing without tools\n"
                "mainAgent: true\n"
                "subagent: false\n"
                "tools: []\n"
                "skills: []\n"
                "plugins: []\n"
                "commandExecutionPolicy: off\n"
                "---\n" + instructions,
                encoding="utf-8",
            )
            flags = ["--agent", agent, "--model", model.name, "--json-schema", json.dumps(schema)]
            if model.reasoning:
                flags += ["--effort", model.reasoning]
            prompt = json.dumps({"event": "user", "message": {"content": data}}, ensure_ascii=False) + "\n"
            reply, _ = self._run(directory, flags, prompt)
        if reply is None:
            raise ProviderError("Antigravity не вернул завершённый ответ в потоке JSONL.")
        if "num_turns" in reply and (type(reply["num_turns"]) is not int or reply["num_turns"] != 1):
            raise ProviderError("Antigravity вернул результат с неожиданным числом ходов.")
        if isinstance(usage := reply.get("usage"), dict):
            self.usage.add({**usage, "cached_input_tokens": usage.get("cache_read_tokens")})
        structured = reply.get("structured_output")
        if not isinstance(structured, dict):
            raise InvalidStructuredOutput("Antigravity не вернул объект по схеме.")
        return structured


class ClaudeCode:
    def __init__(self, timeout: int):
        self.timeout = timeout
        self.usage = Usage()
        executable = shutil.which("claude")
        if not executable:
            raise ProviderError("Установите Claude Code и выполните claude auth login.")
        self.command = [executable, "--safe-mode", "--setting-sources", ""]
        self.env = _cli_environment("CLAUDE_CONFIG_DIR")

    def _run(self, args: list[str], timeout: int, prompt: str = "") -> subprocess.CompletedProcess:
        with TemporaryDirectory(prefix="ai-translator-claude-") as directory:
            try:
                return _run_cli(
                    [*self.command, *args], provider="Claude Code", cwd=Path(directory).resolve(),
                    input=prompt, env=self.env,
                    timeout=timeout, auth=args == ["auth", "status"],
                )
            except subprocess.TimeoutExpired as exc:
                raise ProviderError(f"Claude Code не ответил за {timeout} с.") from exc
            except UnicodeDecodeError as exc:
                raise ProviderError("Ответ Claude Code содержит повреждённый UTF-8.") from exc

    def check(self) -> None:
        result = self._run(["auth", "status"], min(self.timeout, 20))
        if result.returncode and "unknown option" in result.stderr.lower():
            raise ProviderError("Обновите Claude Code: установленная версия не поддерживает параметры безопасного запуска.")
        if result.returncode in (0, 1):
            try:
                status = _decode_envelope(result.stdout, "Claude Code")
            except ProviderError:
                status = {}
            logged_in = status.get("loggedIn")
            method = status.get("authMethod")
            if logged_in is False:
                raise AuthenticationError("Войдите в Claude Code через аккаунт Claude командой claude auth login.")
            if result.returncode == 0 and logged_in is True and isinstance(method, str) and method:
                if method == "claude.ai":
                    return
                raise AuthenticationError("Войдите в Claude Code через аккаунт Claude командой claude auth login.")
        raise ProviderError("Не удалось подтвердить вход через Claude Code.")

    def complete(self, model: Model, instructions: str, data: str, schema: dict) -> dict:
        args = [
            "--print", "--output-format", "json", "--json-schema", json.dumps(schema),
            "--system-prompt", instructions, "--model", model.name, "--tools", "",
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--disallowedTools", "mcp__*", "--no-session-persistence",
        ]
        if model.reasoning:
            args += ["--effort", model.reasoning]
        result = self._run(args, self.timeout, data)
        try:
            envelope = _decode_envelope(result.stdout, "Claude Code")
        except ProviderError:
            if not result.returncode:
                raise
            envelope = {}
        if envelope.get("type") != "result":
            if result.returncode:
                raise self._failure(result.stderr, {})
            raise ProviderError("Claude Code вернул служебное сообщение вместо результата.")
        supplied_usage = envelope.get("usage")
        usage = supplied_usage if isinstance(supplied_usage, dict) else {}
        cached = _token_count(usage.get("cache_read_input_tokens"))
        self.usage.add({
            "input_tokens": _token_count(usage.get("input_tokens")) + cached
            + _token_count(usage.get("cache_creation_input_tokens")),
            "cached_input_tokens": cached,
            "output_tokens": usage.get("output_tokens"),
        })
        if envelope.get("stop_reason") == "refusal":
            raise ProviderRefusal("Claude Code отклонил запрос.")
        subtype = envelope.get("subtype")
        if subtype == "error_max_structured_output_retries" and envelope.get("is_error") is True:
            raise InvalidStructuredOutput("Claude Code не смог сформировать структурированный ответ.")
        if result.returncode or envelope.get("is_error") is True:
            raise self._failure(result.stderr, envelope)
        if subtype != "success" or envelope.get("is_error") is not False:
            raise ProviderError("Claude Code не подтвердил успешное завершение запроса.")
        response = envelope.get("structured_output")
        if response is None:
            raise ProviderError("Claude Code не вернул структурированный ответ.")
        if not isinstance(response, dict):
            raise InvalidStructuredOutput("Структурированный ответ Claude Code должен быть объектом JSON.")
        return response

    @staticmethod
    def _failure(stderr: str, envelope: dict) -> ProviderError:
        errors = envelope.get("errors")
        details = [stderr, envelope.get("result"), envelope.get("error"), errors]
        if isinstance(errors, list):
            details.extend(errors)
        diagnostic = " ".join(detail for detail in details if isinstance(detail, str)).casefold()
        if any(phrase in diagnostic for phrase in ("quota", "rate limit", "usage limit", "too many requests")):
            return ProviderError("Достигнут лимит Claude Code.")
        return ProviderError("Claude Code завершился с ошибкой. Проверьте вход, модель и лимиты подписки.")


def _openai_key() -> str:
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        raise AuthenticationError("Задайте OPENAI_API_KEY для провайдера openai-api.")
    if not all("!" <= char <= "~" for char in key):
        raise AuthenticationError("OPENAI_API_KEY содержит недопустимые символы.")
    return key


def _read_openai_response(request: Request, key: str, timeout: int) -> dict:
    with _ResponseDeadline(timeout) as deadline:
        try:
            with _api_open(request, timeout=timeout, deadline=deadline) as response:
                expected_length = getattr(response, "length", None)
                raw = response.read(_MAX_API_RESPONSE + 1)
        except HTTPError as exc:
            try:
                with exc:
                    envelope = json.loads(exc.read(65536).decode("utf-8"))
                error_details = envelope.get("error") if isinstance(envelope, dict) else None
                detail = error_details.get("message") if isinstance(error_details, dict) else None
            except (ValueError, OSError, HTTPException, RecursionError):
                detail = None
            detail = (detail if isinstance(detail, str) else "").replace(key, "[скрыто]")
            error = AuthenticationError if exc.code in (401, 403) else ProviderError
            raise error(f"OpenAI API HTTP {exc.code}. {detail[:500]}".rstrip()) from exc
        except (OSError, HTTPException) as exc:
            if deadline.expired() or isinstance(exc, TimeoutError):
                raise ProviderError(f"OpenAI API не ответил за {timeout} с.") from exc
            if isinstance(exc, IncompleteRead):
                raise ProviderError("Соединение с OpenAI API прервано до конца ответа.") from exc
            detail = type(exc).__name__ if isinstance(exc, HTTPException) else str(exc).replace(key, "[скрыто]")
            raise ProviderError(f"OpenAI API недоступен. {detail[:500]}") from exc
        if deadline.expired():
            raise ProviderError(f"OpenAI API не ответил за {timeout} с.")
    if len(raw) > _MAX_API_RESPONSE:
        raise ProviderError("Ответ OpenAI API превышает допустимые 16 МиБ.")
    if type(expected_length) is int and len(raw) < expected_length:
        raise ProviderError("Соединение с OpenAI API прервано до конца ответа.")
    try:
        return _decode_envelope(raw.decode("utf-8"), "OpenAI API")
    except UnicodeDecodeError as exc:
        raise ProviderError("Ответ OpenAI API содержит повреждённый UTF-8.") from exc


class OpenAIAPI:
    def __init__(self, timeout: int):
        self.timeout = timeout
        self.usage = Usage()

    def check(self) -> None:
        _openai_key()

    def complete(self, model: Model, instructions: str, data: str, schema: dict) -> dict:
        body = {
            "model": model.name,
            "store": False,
            "instructions": instructions,
            "input": data,
            "text": {"format": {
                "type": "json_schema", "name": "translator", "strict": True, "schema": schema,
            }},
        }
        if model.reasoning:
            body["reasoning"] = {"effort": model.reasoning}
        key = _openai_key()
        request = Request(
            "https://api.openai.com/v1/responses", data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"},
        )
        result = _read_openai_response(request, key, self.timeout)
        self.usage.add(result.get("usage") or {})
        status = result.get("status")
        if status == "incomplete":
            raise ProviderError("OpenAI API вернул неполный ответ.")
        if status != "completed":
            raise ProviderError("OpenAI API не завершил ответ.")
        output = result.get("output")
        text_output = []
        malformed = False
        if isinstance(output, list):
            for item in output:
                if not isinstance(item, dict) or item.get("type") != "message" or item.get("status") != "completed":
                    continue
                content_list = item.get("content")
                if not isinstance(content_list, list):
                    malformed = True
                    continue
                for content in content_list:
                    if not isinstance(content, dict):
                        malformed = True
                        continue
                    if content.get("type") == "refusal":
                        raise ProviderRefusal("OpenAI API отклонил запрос.")
                    if content.get("type") == "output_text":
                        text = content.get("text")
                        if isinstance(text, str):
                            text_output.append(text)
                        else:
                            malformed = True
        if malformed:
            raise ProviderError("OpenAI API вернул повреждённый блок ответа.")
        if text_output:
            return _decode("".join(text_output))
        raise ProviderError("OpenAI API не вернул текст перевода.")
