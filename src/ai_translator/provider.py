import json
import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import Model


class ProviderError(ValueError):
    pass


class InvalidStructuredOutput(ProviderError):
    pass


def _cli_environment(config_var: str) -> dict[str, str]:
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
        self.input += data.get("input_tokens") or 0
        self.output += data.get("output_tokens") or 0
        if "cached_input_tokens" in data:
            self.cached_input += data["cached_input_tokens"] or 0
        else:
            details = data.get("input_tokens_details") or {}
            self.cached_input += details.get("cached_tokens") or 0


def _decode(text: str) -> dict:
    try:
        result = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InvalidStructuredOutput("Модель вернула некорректный JSON.") from exc
    if not isinstance(result, dict):
        raise InvalidStructuredOutput("Ответ модели должен быть объектом JSON.")
    return result


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
            status = subprocess.run(
                [self.executable, "-c", self.auth_override, "login", "status"], env=self.env,
                capture_output=True, text=True, timeout=min(self.timeout, 20), check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ProviderError("Codex не ответил при проверке авторизации.") from exc
        if status.returncode or "logged in using chatgpt" not in (
            status.stdout + status.stderr
        ).lower():
            raise ProviderError("Требуется вход через ChatGPT. Выполните codex login.")

    def complete(self, model: Model, instructions: str, data: str, schema: dict) -> dict:
        with TemporaryDirectory(prefix="ai-translator-") as directory:
            directory = Path(directory)
            schema_path = directory / "schema.json"
            output_path = directory / "output.json"
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
                "--output-schema", str(schema_path), "-o", str(output_path),
                "--model", model.name,
            ]
            if model.reasoning:
                args += ["-c", f"model_reasoning_effort={json.dumps(model.reasoning)}"]
            args.append("-")
            try:
                result = subprocess.run(
                    args, input=data,
                    env=self.env, capture_output=True, encoding="utf-8", errors="replace",
                    timeout=self.timeout, check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise ProviderError(f"Codex не ответил за {self.timeout} с.") from exc
            detail = ""
            for line in result.stdout.splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get("type") == "turn.completed":
                    self.usage.add(event.get("usage") or {})
                elif event.get("type") == "turn.failed":
                    detail = event.get("error", {}).get("message", "")
            if result.returncode or not output_path.is_file():
                if not detail:
                    detail = next(
                        (line for line in result.stderr.splitlines() if line.startswith("error:")), ""
                    )
                detail = re.sub(r"\x1b\[[0-9;]*m", "", detail)
                raise ProviderError(f"Codex завершился без результата. {detail[:500]}".rstrip())
            return _decode(output_path.read_text(encoding="utf-8"))


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
                return subprocess.run(
                    [*self.command, *args], cwd=directory, input=prompt, env=self.env,
                    capture_output=True, encoding="utf-8", errors="replace", timeout=timeout, check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise ProviderError(f"Claude Code не ответил за {timeout} с.") from exc

    def check(self) -> None:
        result = self._run(["auth", "status"], min(self.timeout, 20))
        if result.returncode == 0:
            try:
                status = _decode(result.stdout)
            except InvalidStructuredOutput:
                status = {}
            if status.get("loggedIn") is True and status.get("authMethod") == "claude.ai":
                return
        raise ProviderError("Войдите в Claude Code через аккаунт Claude командой claude auth login.")

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
            envelope = _decode(result.stdout)
        except InvalidStructuredOutput:
            if not result.returncode:
                raise
            envelope = {}
        usage = envelope.get("usage") or {}
        cached = usage.get("cache_read_input_tokens") or 0
        self.usage.add({
            "input_tokens": (usage.get("input_tokens") or 0) + cached
            + (usage.get("cache_creation_input_tokens") or 0),
            "cached_input_tokens": cached,
            "output_tokens": usage.get("output_tokens"),
        })
        if envelope.get("subtype") == "error_max_structured_output_retries":
            raise InvalidStructuredOutput("Claude Code не смог сформировать структурированный ответ.")
        if result.returncode or envelope.get("is_error"):
            raise ProviderError("Claude Code завершился с ошибкой. Проверьте вход, модель и лимиты подписки.")
        response = envelope.get("structured_output")
        if not isinstance(response, dict):
            raise InvalidStructuredOutput("Claude Code не вернул структурированный ответ.")
        return response


class OpenAIAPI:
    def __init__(self, timeout: int):
        self.timeout = timeout
        self.usage = Usage()

    def check(self) -> None:
        if not os.environ.get("OPENAI_API_KEY"):
            raise ProviderError("Задайте OPENAI_API_KEY для провайдера openai-api.")

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
        request = Request(
            "https://api.openai.com/v1/responses", data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}",
                     "Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                result = json.load(response)
        except HTTPError as exc:
            try:
                detail = json.load(exc).get("error", {}).get("message", "")
            except (ValueError, OSError):
                detail = ""
            detail = detail.replace(os.environ["OPENAI_API_KEY"], "[скрыто]")
            raise ProviderError(f"OpenAI API HTTP {exc.code}. {detail[:500]}".rstrip()) from exc
        except (URLError, TimeoutError) as exc:
            raise ProviderError(f"OpenAI API недоступен. {exc}") from exc
        self.usage.add(result.get("usage") or {})
        if result.get("status") != "completed":
            raise ProviderError(f"OpenAI API не завершил ответ. Статус: {result.get('status')}.")
        for item in result.get("output", []):
            for content in item.get("content", []):
                if content.get("type") == "output_text":
                    return _decode(content["text"])
        raise ProviderError("OpenAI API не вернул текст перевода.")
