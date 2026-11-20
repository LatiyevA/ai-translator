import io
import json
import os
import socket
import subprocess
import sys
from contextlib import contextmanager, nullcontext
from http.client import HTTPResponse, IncompleteRead
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import BoundedSemaphore, Event, Thread
from time import monotonic, sleep
from urllib.error import HTTPError
from urllib.request import Request
from unittest import TestCase, skipIf
from unittest.mock import Mock, patch

from ai_translator.config import Model
from ai_translator.posix_job import run_posix_supervised
from ai_translator.provider import (
    Antigravity, AuthenticationError, ClaudeCode, Codex, InvalidStructuredOutput, OpenAIAPI, ProviderError, ProviderRefusal,
    _ResponseDeadline, _api_open, _run_cli,
)
from ai_translator.windows_job import main as windows_supervisor, run_supervised, taskkill_command

SCHEMA = {"type": "object"}
MODEL = Model("test-model", "low")


def codex_message(value):
    return {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(value)}}


class ProviderContracts(TestCase):
    @contextmanager
    def python_provider(self, program):
        # Keep the adapter's subprocess and pipe handling without using a provider or account.
        popen = subprocess.Popen
        processes = []

        def launch(args, **kwargs):
            command = args if os.name == "posix" else [sys.executable, "-c", program, *args[1:]]
            child = popen(command, **kwargs)
            processes.append(child)
            return child

        def supervise(args, execute):
            return run_posix_supervised([sys.executable, "-c", program, *args[1:]], execute)

        with TemporaryDirectory() as home, patch.dict("os.environ", {"CODEX_HOME": home}), patch(
            "ai_translator.provider.shutil.which", return_value=sys.executable,
        ), patch("ai_translator.provider.subprocess.Popen", side_effect=launch), patch(
            "ai_translator.provider.run_posix_supervised", side_effect=supervise
        ):
            try:
                yield processes
            finally:
                for child in processes:
                    if child.poll() is None:
                        child.kill()
                        child.wait()

    def test_codex_failed_turn_takes_priority_over_a_complete_reply(self):
        events = [
            {"type": "turn.started"}, codex_message({"stale": True}), {"type": "turn.completed"},
            {"type": "turn.failed", "error": {"message": "Test quota exhausted\nprivate source"}},
        ]

        with TemporaryDirectory() as home, patch.dict("os.environ", {"CODEX_HOME": home}), patch(
            "ai_translator.provider.shutil.which", return_value="/fake/codex"
        ), patch("ai_translator.provider._run_cli", return_value=subprocess.CompletedProcess(
            [], 0, "\n".join(map(json.dumps, events)), "",
        )):
            with self.assertRaisesRegex(ProviderError, "Достигнут лимит Codex") as failure:
                Codex(3).complete(MODEL, "instructions", "{}", SCHEMA)
        self.assertNotIn("private source", str(failure.exception))

    def test_codex_missing_output_and_cli_timeouts_are_reported(self):
        with TemporaryDirectory() as home, patch.dict("os.environ", {"CODEX_HOME": home}), patch(
            "ai_translator.provider.shutil.which", return_value="/fake/cli"
        ), patch("ai_translator.provider._run_cli") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            with self.assertRaisesRegex(ProviderError, "завершённый ответ"):
                Codex(3).complete(MODEL, "instructions", "{}", SCHEMA)
            run.side_effect = subprocess.TimeoutExpired("cli", 3)
            for backend in (Codex(3), ClaudeCode(3)):
                with self.subTest(backend=type(backend).__name__):
                    with self.assertRaisesRegex(ProviderError, "не ответил за 3"):
                        backend.complete(MODEL, "instructions", "{}", SCHEMA)

    def test_codex_rejects_invalid_utf8_in_structured_reply(self):
        with self.python_provider("import sys; sys.stdout.buffer.write(bytes([255]))"):
            with self.assertRaisesRegex(ProviderError, "Codex.*повреждённый UTF-8") as failure:
                Codex(3).complete(MODEL, "instructions", "{}", SCHEMA)
        self.assertIs(type(failure.exception), ProviderError)

    def test_codex_reports_quota_when_another_line_is_invalid_utf8(self):
        event = b'{"type":"turn.failed","error":{"message":"quota"}}'
        for lines in ((event, b"\xff"), (b"\xff", event)):
            with self.subTest(lines=lines):
                output = b"\n".join(lines) + b"\n"
                with self.python_provider(f"import sys; sys.stdout.buffer.write({output!r})"):
                    with self.assertRaisesRegex(ProviderError, "Достигнут лимит Codex"):
                        Codex(3).complete(MODEL, "instructions", "{}", SCHEMA)

    def test_codex_uses_canonical_sandbox_paths_through_symlinked_temp_directory(self):
        with TemporaryDirectory() as directory:
            real = Path(directory, "real")
            real.mkdir()
            alias = Path(directory, "alias")
            try:
                alias.symlink_to(real, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"Символические ссылки недоступны: {exc}")

            def run(args, **kwargs):
                work = Path(args[args.index("--cd") + 1])
                schema = Path(args[args.index("--output-schema") + 1])
                self.assertEqual(work.parent, real.resolve())
                self.assertEqual(schema.parent, work)
                events = [{"type": "turn.started"}, codex_message({"ok": True}), {"type": "turn.completed"}]
                return subprocess.CompletedProcess(args, 0, "\n".join(map(json.dumps, events)), "")

            with patch.dict("os.environ", {"CODEX_HOME": directory}), patch(
                "ai_translator.provider.shutil.which", return_value="/fake/codex",
            ), patch("ai_translator.provider.TemporaryDirectory", side_effect=lambda **kw: TemporaryDirectory(
                dir=alias, **kw,
            )), patch(
                "ai_translator.provider._run_cli", side_effect=run,
            ):
                self.assertEqual(Codex(5).complete(MODEL, "instructions", "{}", SCHEMA), {"ok": True})

    def test_codex_stream_reply_requires_completed_turn_and_uses_its_last_agent_message(self):
        events = [
            {"type": "turn.started"}, codex_message({"old": True}), codex_message({"final": True}),
            {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 2}},
            codex_message({"outside_turn": True}),
        ]

        def run(args, **kwargs):
            self.assertNotIn("-o", args)
            self.assertEqual(kwargs["errors"], "surrogateescape")
            self.assertEqual(kwargs["stderr_errors"], "replace")
            return subprocess.CompletedProcess(args, 0, "\n".join(map(json.dumps, events)), "")

        with TemporaryDirectory() as directory, patch.dict("os.environ", {"CODEX_HOME": directory}), patch(
            "ai_translator.provider.shutil.which", return_value="/fake/codex",
        ), patch(
            "ai_translator.provider._run_cli", side_effect=run,
        ):
            backend = Codex(5)
            self.assertEqual(backend.complete(MODEL, "instructions", "{}", SCHEMA), {"final": True})
            self.assertEqual((backend.usage.input, backend.usage.output), (10, 2))
            events.append(events[3].copy())
            backend = Codex(5)
            with self.assertRaisesRegex(ProviderError, "потоке JSONL"):
                backend.complete(MODEL, "instructions", "{}", SCHEMA)
            self.assertEqual((backend.usage.input, backend.usage.output), (10, 2))
            events.pop()
            started = events.pop(0)
            with self.assertRaisesRegex(ProviderError, "потоке JSONL"):
                backend.complete(MODEL, "instructions", "{}", SCHEMA)
            events.insert(0, started)
            for malformed in (["malformed envelope"], {}, {"type": 0}, {"type": "item.completed", "item": {}}):
                with self.subTest(malformed=malformed):
                    events.insert(3, malformed)
                    with self.assertRaisesRegex(ProviderError, "потоке JSONL"):
                        backend.complete(MODEL, "instructions", "{}", SCHEMA)
                    events.pop(3)
            events.insert(3, {"type": "new.event"})
            self.assertEqual(backend.complete(MODEL, "instructions", "{}", SCHEMA), {"final": True})
            events.pop(3)
            events.pop(3)
            with self.assertRaisesRegex(ProviderError, "завершённый ответ"):
                backend.complete(MODEL, "instructions", "{}", SCHEMA)
            events.extend(({"type": "turn.completed"}, {"type": "turn.failed", "error": {"message": "quota"}}))
            with self.assertRaisesRegex(ProviderError, "Достигнут лимит Codex"):
                backend.complete(MODEL, "instructions", "{}", SCHEMA)

    def test_claude_distinguishes_schema_retry_from_quota_error(self):
        with patch("ai_translator.provider.shutil.which", return_value="/fake/claude"), patch(
            "ai_translator.provider._run_cli"
        ) as run:
            for subtype, expected, message in (
                ("error_max_structured_output_retries", InvalidStructuredOutput, "структурированный ответ"),
                ("error_during_execution", ProviderError, "Достигнут лимит Claude Code"),
            ):
                envelope = {"type": "result", "subtype": subtype, "is_error": True,
                            "errors": ["quota exhausted: private detail"]}
                run.return_value = subprocess.CompletedProcess([], 1, json.dumps(envelope), "")
                with self.subTest(subtype=subtype), self.assertRaisesRegex(expected, message) as failure:
                    ClaudeCode(3).complete(MODEL, "instructions", "{}", SCHEMA)
                self.assertIs(type(failure.exception), expected)
                self.assertNotIn("private detail", str(failure.exception))

    def test_claude_requires_a_successful_result_envelope(self):
        with patch("ai_translator.provider.shutil.which", return_value="/fake/claude"), patch(
            "ai_translator.provider._run_cli",
        ) as run:
            backend = ClaudeCode(3)
            for fields in ({"type": "system"}, {"type": "result", "subtype": "unknown"},
                           {"type": "result", "subtype": "success", "is_error": None}):
                with self.subTest(fields=fields):
                    envelope = {"is_error": False, "structured_output": {"ok": True}, **fields}
                    run.return_value = subprocess.CompletedProcess([], 0, json.dumps(envelope), "")
                    with self.assertRaises(ProviderError) as failure:
                        backend.complete(MODEL, "instructions", "{}", SCHEMA)
                    self.assertIs(type(failure.exception), ProviderError)

    def test_antigravity_auth_and_translation_use_bounded_ndjson_stdin(self):
        original = '{"text":"雨停了。"}'
        structured = {"translations": [{"id": 1, "text": "Дождь стих."}]}

        def run(args, **kwargs):
            self.assertNotIn("GEMINI_API_KEY", kwargs["env"])
            self.assertNotIn("AGY_ADC_AUTH", kwargs["env"])
            settings = json.loads(args[args.index("--settings") + 1])
            self.assertFalse(settings["useG1Credits"])
            self.assertEqual(settings["modelProvider"], "")
            self.assertEqual(settings["toolPermission"], "strict")
            self.assertIn("--disable-slash-commands", args)
            self.assertNotIn("--continue", args)
            if kwargs["auth"]:
                self.assertEqual(kwargs["input"], "")
                self.assertLessEqual(kwargs["timeout"], 10)
                return subprocess.CompletedProcess(args, 0, "", "Authentication successful")

            self.assertEqual(args[args.index("--model") + 1], MODEL.name)
            self.assertEqual(args[args.index("--effort") + 1], MODEL.reasoning)
            self.assertEqual(json.loads(args[args.index("--json-schema") + 1]), SCHEMA)
            self.assertFalse(any(original in argument for argument in args))
            self.assertEqual(json.loads(kwargs["input"]), {"event": "user", "message": {"content": original}})
            agent = args[args.index("--agent") + 1]
            prompt = kwargs["cwd"].joinpath(".agents", "agents", f"{agent}.md").read_text(encoding="utf-8")
            self.assertIn("tools: []", prompt)
            self.assertTrue(prompt.endswith("instructions"))
            events = [{"event": "init"}, {"event": "result", "result": {
                "status": "SUCCESS", "structured_output": structured, "num_turns": 1,
                "usage": {"input_tokens": 50, "output_tokens": 8, "cache_read_tokens": 12},
            }}]
            return subprocess.CompletedProcess(args, 0, "\n".join(map(json.dumps, events)), "")

        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key", "AGY_ADC_AUTH": "true"}), patch(
            "ai_translator.provider.shutil.which", return_value="/fake/agy",
        ), patch("ai_translator.provider._run_cli", side_effect=run):
            backend = Antigravity(25)
            self.assertTrue(backend.check())
            self.assertEqual(backend.complete(MODEL, "instructions", original, SCHEMA), structured)
            self.assertEqual((backend.usage.input, backend.usage.output, backend.usage.cached_input), (50, 8, 12))

    def test_antigravity_classifies_auth_quota_timeout_and_malformed_responses(self):
        with patch("ai_translator.provider.shutil.which", return_value="/fake/agy"), patch(
            "ai_translator.provider._run_cli"
        ) as run:
            backend = Antigravity(25)
            for envelope, code, exception, message in (
                ({"status": "ERROR", "error": "quota exhausted"}, 1, ProviderError, "Достигнут лимит"),
                ({"status": "SUCCESS", "finished_reason": "print_timeout",
                  "structured_output": {"incomplete": True}}, 0, ProviderError, "Истекло время ожидания"),
                ({"status": "ERROR", "finished_reason": "interrupted"}, 1, ProviderError, "прервал выполнение"),
                ({"status": "ERROR", "finished_reason": "unfamiliar diagnostic"}, 1, ProviderError, "без результата"),
                ({"status": "SUCCESS"}, 0, InvalidStructuredOutput, "объект по схеме"),
                ([], 0, ProviderError, "завершающее событие"),
            ):
                with self.subTest(message=message):
                    run.return_value = subprocess.CompletedProcess(
                        [], code, json.dumps({"event": "result", "result": envelope}), "",
                    )
                    with self.assertRaisesRegex(exception, message) as failure:
                        backend.complete(Model("test-model"), "instructions", "{}", SCHEMA)
                    self.assertIs(type(failure.exception), exception)
                    self.assertNotIn("--effort", run.call_args.args[0])
            run.side_effect = subprocess.TimeoutExpired("agy", 25)
            with self.assertRaisesRegex(ProviderError, "не ответил за 25"):
                backend.complete(MODEL, "instructions", "{}", SCHEMA)

    def test_claude_ignores_invalid_usage_before_classifying_response(self):
        with patch("ai_translator.provider.shutil.which", return_value="/fake/claude"), patch(
            "ai_translator.provider._run_cli"
        ) as run:
            backend = ClaudeCode(3)
            run.return_value = subprocess.CompletedProcess(
                [], 0, '{"type":"result","subtype":"success","is_error":false,"structured_output":{"ok":true},"usage":[1]}', "",
            )
            self.assertEqual(backend.complete(MODEL, "instructions", "{}", SCHEMA), {"ok": True})
            run.return_value = subprocess.CompletedProcess(
                [], 1, json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": True,
                                   "usage": {"input_tokens": "unknown", "cache_read_input_tokens": 4}}), "",
            )
            with self.assertRaisesRegex(ProviderError, "завершился с ошибкой"):
                backend.complete(MODEL, "instructions", "{}", SCHEMA)
            self.assertEqual(backend.usage.input, 4)

    def test_claude_explicit_refusal_and_old_cli_are_classified_separately(self):
        with patch("ai_translator.provider.shutil.which", return_value="/fake/claude"), patch(
            "ai_translator.provider._run_cli",
        ) as run:
            backend = ClaudeCode(3)
            run.return_value = subprocess.CompletedProcess(
                [], 0, '{"type":"result","subtype":"success","stop_reason":"refusal","is_error":false}', "",
            )
            with self.assertRaisesRegex(ProviderRefusal, "отклонил"):
                backend.complete(MODEL, "instructions", "{}", SCHEMA)
            run.return_value = subprocess.CompletedProcess([], 1, "", "error: unknown option '--safe-mode'")
            with self.assertRaisesRegex(ProviderError, "Обновите Claude Code"):
                backend.check()

    def test_claude_rejects_invalid_utf8_without_substituting_a_character(self):
        with self.python_provider("import sys; sys.stdout.buffer.write(bytes([255]))"):
            with self.assertRaisesRegex(ProviderError, "повреждённый UTF-8"):
                ClaudeCode(3).complete(MODEL, "instructions", "{}", SCHEMA)

    def test_cli_captures_both_streams_and_utf8_prompt_without_deadlocking(self):
        program = """
import json
import sys

sys.stderr.write("d" * 100000)
sys.stdout.write(" " * 100000)
if sys.argv[-2:] == ["auth", "status"]:
    print(json.dumps({"loggedIn": True, "authMethod": "claude.ai"}))
else:
    prompt = json.load(sys.stdin.buffer)
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "structured_output": prompt}))
"""
        with self.python_provider(program):
            backend = ClaudeCode(10)
            backend.check()
            self.assertEqual(backend.complete(MODEL, "instructions", '{"text":"她来了。"}', SCHEMA),
                             {"text": "她来了。"})

    def test_cli_kills_provider_when_output_stream_is_too_large(self):
        for backend_type, name in ((Codex, "stdout"), (ClaudeCode, "stderr")):
            program = f"""
import sys
import time

sys.{name}.buffer.write(b"x" * 4097)
sys.{name}.buffer.flush()
time.sleep(30)
"""
            with self.subTest(backend=backend_type, stream=name), self.python_provider(program) as children, patch(
                "ai_translator.provider._MAX_CLI_STDOUT", 4096,
            ), patch("ai_translator.provider._MAX_CLI_STDERR", 4096):
                with self.assertRaisesRegex(ProviderError, f"поток {name} превышает") as failure:
                    backend_type(5).complete(MODEL, "instructions", "{}", SCHEMA)
                self.assertIs(type(failure.exception), ProviderError)
                self.assertIsNotNone(children[0].poll())

    def test_authentication_output_is_bounded_separately(self):
        program = 'import sys, time; sys.stdout.write("x" * 4097); sys.stdout.flush(); time.sleep(30)'
        for backend_type in (Codex, ClaudeCode):
            with self.subTest(backend=backend_type), self.python_provider(program) as children, patch(
                "ai_translator.provider._MAX_CLI_AUTH", 4096,
            ):
                with self.assertRaisesRegex(ProviderError, "поток stdout превышает"):
                    backend_type(5).check()
                self.assertIsNotNone(children[0].poll())

    def test_cli_times_out_even_if_provider_closes_output_before_exiting(self):
        program = "import os, time; os.close(1); os.close(2); time.sleep(30)"
        with self.python_provider(program) as children:
            with self.assertRaisesRegex(ProviderError, "не ответил за 1"):
                Codex(1).complete(MODEL, "instructions", "{}", SCHEMA)
            self.assertIsNotNone(children[0].poll())

    @skipIf(os.name != "posix", "Тест группы процессов POSIX")
    def test_cli_cleans_up_descendants_after_provider_exits_and_preserves_status(self):
        import fcntl

        descendant = """
import fcntl, os, sys, time

with open(sys.argv[1], "w") as marker:
    fcntl.flock(marker, fcntl.LOCK_EX)
    marker.write(str(os.getpid()))
    marker.flush()
    time.sleep(30)
"""
        launcher = """
import subprocess, sys, time
from pathlib import Path

marker = Path(sys.argv[1])
output = None if sys.argv[4] == "inherit" else subprocess.DEVNULL
subprocess.Popen([sys.executable, "-c", sys.argv[2], str(marker)],
                 stdin=subprocess.DEVNULL, stdout=output, stderr=output)
while not marker.is_file() or not marker.stat().st_size:
    time.sleep(0.01)
print("result")
sys.exit(int(sys.argv[3]))
"""
        for code, streams in ((0, "discard"), (17, "discard"), (0, "inherit"), (17, "inherit")):
            with self.subTest(code=code, streams=streams), TemporaryDirectory() as directory:
                marker = Path(directory, "descendant.pid")
                needs_cleanup = True
                try:
                    result = _run_cli([sys.executable, "-c", launcher, str(marker), descendant, str(code), streams],
                                      provider="local stub", env={}, timeout=5)
                    self.assertEqual((result.returncode, result.stdout), (code, "result\n"))
                    deadline = monotonic() + 2
                    with marker.open() as stream:
                        while True:
                            try:
                                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                                needs_cleanup = False
                                break
                            except BlockingIOError:
                                if monotonic() >= deadline:
                                    self.fail("Потомок продолжает удерживать файловую блокировку.")
                                sleep(0.02)
                finally:
                    if needs_cleanup and marker.is_file() and marker.stat().st_size:
                        try:
                            os.kill(int(marker.read_text()), 9)
                        except ProcessLookupError:
                            pass

    @skipIf(os.name != "posix", "Тест группы процессов POSIX")
    def test_killing_independent_cli_stops_provider_and_descendant(self):
        import fcntl

        descendant = """
import fcntl, os, sys, time
with open(sys.argv[1], "w") as marker:
    fcntl.flock(marker, fcntl.LOCK_EX)
    marker.write(str(os.getpid()))
    marker.flush()
    time.sleep(30)
"""
        provider = """
import fcntl, os, subprocess, sys, time
subprocess.Popen([sys.executable, "-c", sys.argv[1], sys.argv[3]])
with open(sys.argv[2], "w") as marker:
    fcntl.flock(marker, fcntl.LOCK_EX)
    marker.write(str(os.getpid()))
    marker.flush()
    time.sleep(30)
"""
        cli = """
import sys
from ai_translator.provider import _run_cli
_run_cli([sys.executable, "-c", *sys.argv[1:]], provider="local stub", env={}, timeout=20)
"""
        with TemporaryDirectory() as directory:
            markers = [Path(directory, name) for name in ("provider.pid", "descendant.pid")]
            with subprocess.Popen([sys.executable, "-c", cli, provider, descendant, *map(str, markers)]) as process:
                locked = set(markers)
                try:
                    deadline = monotonic() + 5
                    while not all(path.is_file() and path.stat().st_size for path in markers):
                        self.assertIsNone(process.poll())
                        if monotonic() >= deadline:
                            self.fail("Провайдер и потомок не запустились.")
                        sleep(0.02)
                    process.kill()
                    process.wait(timeout=3)
                    deadline = monotonic() + 3
                    while locked:
                        for path in tuple(locked):
                            with path.open("r") as marker:
                                try:
                                    fcntl.flock(marker, fcntl.LOCK_EX | fcntl.LOCK_NB)
                                except BlockingIOError:
                                    continue
                                locked.remove(path)
                        if locked and monotonic() >= deadline:
                            self.fail("Провайдер или потомок пережил принудительное завершение CLI.")
                        if locked:
                            sleep(0.02)
                finally:
                    if process.poll() is None:
                        process.kill()
                    for path in locked:
                        if path.is_file() and path.stat().st_size:
                            try:
                                os.kill(int(path.read_text()), 9)
                            except ProcessLookupError:
                                pass

    @skipIf(os.name != "posix", "Тест группы процессов POSIX")
    def test_cli_overflow_releases_lock_held_by_descendant_with_inherited_pipes(self):
        import fcntl

        child = """
import fcntl, os, sys, time
with open(sys.argv[1], "w") as marker:
    fcntl.flock(marker, fcntl.LOCK_EX)
    marker.write(str(os.getpid()))
    marker.flush()
    time.sleep(30)
"""
        with TemporaryDirectory() as directory:
            marker = Path(directory, "child.pid")
            program = f"""
import subprocess, sys, time
from pathlib import Path

marker = Path({str(marker)!r})
subprocess.Popen([sys.executable, "-c", {child!r}, str(marker)])
while not marker.is_file() or not marker.stat().st_size:
    time.sleep(0.01)
sys.stdout.buffer.write(b"x" * 4097)
sys.stdout.buffer.flush()
time.sleep(30)
"""
            try:
                with self.python_provider(program), patch(
                    "ai_translator.provider._MAX_CLI_STDOUT", 4096,
                ):
                    with self.assertRaisesRegex(ProviderError, "поток stdout превышает"):
                        Codex(5).complete(MODEL, "instructions", "{}", SCHEMA)
                deadline = monotonic() + 1
                with marker.open("r") as stream:
                    while True:
                        try:
                            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            break
                        except BlockingIOError:
                            if monotonic() >= deadline:
                                self.fail("Потомок удерживает канал и файловую блокировку.")
                            sleep(0.02)
            finally:
                if marker.is_file() and marker.stat().st_size:
                    try:
                        os.kill(int(marker.read_text()), 9)
                    except ProcessLookupError:
                        pass

    def test_codex_can_answer_with_an_existing_large_provider_log(self):
        with TemporaryDirectory() as directory:
            log = Path(directory, "provider.log")
            with log.open("wb") as stream:
                stream.truncate(16 * 1024 * 1024)
            script = Path(directory, "codex.py")
            events = [{"type": "turn.started"}, codex_message({"ok": True}), {"type": "turn.completed"}]
            event_stream = "\n".join(map(json.dumps, events))
            program = f"""
from pathlib import Path
with Path(__file__).with_name("provider.log").open("ab") as stream:
    stream.write(b"next event")
print({event_stream!r})
"""
            script.write_text(program, encoding="utf-8")
            if os.name == "nt":
                executable = script.with_suffix(".cmd")
                executable.write_text(f'@"{sys.executable}" "{script}" %*\n', encoding="utf-8")
            else:
                executable = script
                script.write_text(f"#!{sys.executable}\n{program}", encoding="utf-8")
                script.chmod(0o700)
            with patch.dict("os.environ", {"CODEX_HOME": directory}), patch(
                "ai_translator.provider.shutil.which", return_value=str(executable),
            ):
                self.assertEqual(Codex(5).complete(MODEL, "instructions", "{}", SCHEMA), {"ok": True})
            self.assertEqual(log.stat().st_size, 16 * 1024 * 1024 + len(b"next event"))

    @skipIf(os.name != "nt", "Тест Windows Job Object")
    def test_windows_job_stops_descendant_after_provider_exits_without_waiting(self):
        child = """
import os, sys, time
from pathlib import Path
Path(sys.argv[1]).write_text(str(os.getpid()))
time.sleep(30)
"""
        with TemporaryDirectory() as directory:
            marker = Path(directory, "child.pid")
            parent = f"""
import subprocess, sys, time
from pathlib import Path
marker = Path({str(marker)!r})
subprocess.Popen([sys.executable, "-c", {child!r}, str(marker)],
                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                 creationflags=subprocess.CREATE_NO_WINDOW)
while not marker.is_file() or not marker.stat().st_size:
    time.sleep(0.01)
sys.stdout.buffer.write(sys.stdin.buffer.read())
sys.exit(17)
"""
            result = _run_cli([sys.executable, "-c", parent], provider="Codex",
                              env=os.environ.copy(), input="雨停了。", timeout=10)
            self.assertEqual(result.returncode, 17)
            self.assertEqual(result.stdout.rstrip(), "雨停了。")
            self._assert_windows_process_stopped(int(marker.read_text()))

    @skipIf(os.name != "nt", "Тест ожидания родительского процесса Windows")
    def test_windows_job_stops_provider_when_only_worker_is_killed(self):
        with TemporaryDirectory() as directory:
            marker = Path(directory, "child.pid")
            provider = f"""
import os, time
from pathlib import Path
Path({str(marker)!r}).write_text(str(os.getpid()))
time.sleep(30)
"""
            worker = f"""
import os, sys
from ai_translator.provider import _run_cli
_run_cli([sys.executable, "-c", {provider!r}], provider="Codex", env=os.environ.copy(), timeout=30)
"""
            with subprocess.Popen([sys.executable, "-c", worker], stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW) as process:
                try:
                    deadline = monotonic() + 8
                    while not marker.is_file() or not marker.stat().st_size:
                        self.assertIsNone(process.poll(), "Не удалось запустить провайдера в Windows job.")
                        self.assertLess(monotonic(), deadline, "Провайдер не сообщил PID.")
                        sleep(0.02)
                    pid = int(marker.read_text())
                finally:
                    process.kill()  # Deliberately avoid taskkill: the inherited handle must signal the exit.
                    process.wait(timeout=5)
            self._assert_windows_process_stopped(pid)

    def _assert_windows_process_stopped(self, pid):
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not handle:
            self.assertEqual(ctypes.get_last_error(), 87)  # ERROR_INVALID_PARAMETER
            return
        wait = kernel.WaitForSingleObject(handle, 2000)
        try:
            self.assertEqual(wait, 0)
        finally:
            kernel.CloseHandle(handle)
            if wait != 0:
                subprocess.run(taskkill_command(pid), timeout=2, check=False,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def test_windows_supervisor_spawns_only_after_job_and_worker_checks(self):
        for job_error, parent_alive, code in ((None, True, 17), (None, False, 130),
                                               (OSError("nested jobs unavailable"), True, 125)):
            with self.subTest(exit_code=code), TemporaryDirectory() as directory, patch(
                "ai_translator.windows_job._attach_current_process", side_effect=job_error,
            ) as attach, patch("ai_translator.windows_job._watch_parent", return_value=parent_alive) as watch:
                def start(command, **kwargs):
                    attach.assert_called_once()
                    watch.assert_called_once_with(93)
                    self.assertEqual(command, ["provider", "аргумент с пробелом"])
                    self.assertEqual((kwargs["stdin"], kwargs["stdout"], kwargs["stderr"]), (0, 1, 2))
                    return 17

                rejected = Path(directory, "rejected")
                with patch("ai_translator.windows_job.subprocess.call", side_effect=start) as launch:
                    self.assertEqual(windows_supervisor(93, rejected, ["provider", "аргумент с пробелом"]), code)
                self.assertEqual(launch.call_count, int(code == 17))
                self.assertEqual(rejected.is_file(), job_error is not None)

    def test_windows_breakaway_only_retries_job_setup_and_never_a_provider_exit_code(self):
        command = ["provider", "аргумент"]
        with patch("ai_translator.windows_job._parent_handle", return_value=nullcontext(93)), patch.multiple(
            "ai_translator.windows_job.subprocess", create=True,
            STARTUPINFO=Mock(return_value={"handle_list": [93]}),
            CREATE_NO_WINDOW=0x08000000, CREATE_BREAKAWAY_FROM_JOB=0x01000000,
        ):
            flags = []

            def execute(args, options):
                self.assertEqual(args[3], "93")
                self.assertEqual(args[5:], command)
                self.assertTrue(options["close_fds"])
                self.assertEqual(options["startupinfo"]["handle_list"], [93])
                flags.append(options["creationflags"])
                if len(flags) == 1:
                    Path(args[4]).touch()
                return subprocess.CompletedProcess(args, 125)

            # The second code 125 belongs to the already-started provider because it has no setup marker.
            self.assertEqual(run_supervised(command, execute).returncode, 125)
            self.assertEqual(flags, [0x08000000, 0x09000000])

            def rejected(args, options):
                Path(args[4]).touch()
                return subprocess.CompletedProcess(args, 125)

            with self.assertRaisesRegex(OSError, "не разрешила создать управляемую группу"):
                run_supervised(command, rejected)

    def test_claude_distinguishes_invalid_envelope_from_invalid_model_output(self):
        with patch("ai_translator.provider.shutil.which", return_value="/fake/claude"), patch(
            "ai_translator.provider._run_cli",
        ) as run:
            for payload, expected in (
                ("{", ProviderError), ("[]", ProviderError), ("{}", ProviderError),
                ('{"type":"result","subtype":"success","is_error":false,"structured_output":[]}',
                 InvalidStructuredOutput),
            ):
                run.return_value = subprocess.CompletedProcess([], 0, payload, "")
                with self.subTest(payload=payload):
                    with self.assertRaises(expected) as failure:
                        ClaudeCode(3).complete(MODEL, "instructions", "{}", SCHEMA)
                    self.assertIs(type(failure.exception), expected)

    def test_openai_incomplete_vs_invalid_structured_responses(self):
        cases = [
            (b'{"status":"incomplete","usage":[1]}', ProviderError),
            (b'[]', ProviderError),
            (b'<html>error</html>', ProviderError),
            (b'{"status":"completed","output":[null,{"content":[null]}]}', ProviderError),
            (b'{"status":"completed","output":[{"content":[{"type":"refusal"}]}]}', ProviderError),
            (b'{"status":"completed","output":[{"type":"reasoning","status":"incomplete","content":'
             b'[{"type":"output_text","text":"{}"}]}]}', ProviderError),
            (b'{"status":"completed","output":[{"type":"message","status":"incomplete","content":'
             b'[{"type":"output_text","text":"{}"}]}]}', ProviderError),
            (b'{"status":"completed","output":[{"type":"message","status":"completed","content":'
             b'[{"type":"output_text","text":"[]"}]}]}', InvalidStructuredOutput),
        ]
        with patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}), patch(
            "ai_translator.provider._api_open"
        ) as urlopen:
            for envelope, expected in cases:
                urlopen.return_value = io.BytesIO(envelope)
                with self.subTest(envelope=envelope), self.assertRaises(expected) as failure:
                    OpenAIAPI(3).complete(MODEL, "instructions", "{}", SCHEMA)
                self.assertIs(type(failure.exception), expected)

    def test_openai_unexpected_status_is_not_copied_into_an_error(self):
        body = json.dumps({"status": {"example": "test-key"}}).encode()
        with patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}), patch(
            "ai_translator.provider._api_open", return_value=io.BytesIO(body),
        ):
            with self.assertRaises(ProviderError) as failure:
                OpenAIAPI(3).complete(MODEL, "instructions", "{}", SCHEMA)
        self.assertEqual(str(failure.exception), "OpenAI API не завершил ответ.")

    def test_openai_refusal_stops_instead_of_requesting_a_schema_retry(self):
        body = {"status": "completed", "output": [{"type": "message", "status": "completed",
                "content": [{"type": "refusal", "refusal": "test refusal"}]}]}
        with patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}), patch(
            "ai_translator.provider._api_open", return_value=io.BytesIO(json.dumps(body).encode()),
        ):
            with self.assertRaisesRegex(ProviderRefusal, "отклонил"):
                OpenAIAPI(3).complete(MODEL, "instructions", "{}", SCHEMA)

    def test_openai_reassembles_completed_output_text_in_order_and_honors_refusal(self):
        payload = '{"text":"Она обернулась."}'
        envelope = {"status": "completed", "output": [
            {"type": "message", "status": "completed", "content": [
                {"type": "output_text", "text": payload[:13]},
                {"type": "output_text", "text": payload[13:18]},
            ]},
            {"type": "message", "status": "incomplete", "content": [{"type": "output_text", "text": "игнорировать"}]},
            {"type": "message", "status": "completed", "content": [{"type": "output_text", "text": payload[18:]}]},
        ]}
        with patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}), patch(
            "ai_translator.provider._api_open",
            side_effect=lambda *_args, **_kwargs: io.BytesIO(json.dumps(envelope).encode()),
        ):
            self.assertEqual(OpenAIAPI(3).complete(MODEL, "instructions", "{}", SCHEMA),
                             {"text": "Она обернулась."})
            envelope["output"][-1]["content"].append({"type": "refusal", "refusal": "test refusal"})
            with self.assertRaises(ProviderRefusal):
                OpenAIAPI(3).complete(MODEL, "instructions", "{}", SCHEMA)

    def test_openai_rejects_damaged_output_text_next_to_a_valid_object(self):
        for broken in ({}, {"text": None}, {"text": 17}):
            with self.subTest(fragment=broken):
                envelope = {"status": "completed", "output": [
                    {"type": "message", "status": "completed", "content": [
                        {"type": "output_text", "text": '{"accepted":true}'},
                        {"type": "output_text", **broken},
                    ]},
                ]}
                with patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}), patch(
                    "ai_translator.provider._api_open", return_value=io.BytesIO(json.dumps(envelope).encode()),
                ):
                    with self.assertRaisesRegex(ProviderError, "повреждённый блок") as failure:
                        OpenAIAPI(3).complete(MODEL, "instructions", "{}", SCHEMA)
                    self.assertIs(type(failure.exception), ProviderError)

    def test_openai_stops_reading_at_response_limit(self):
        with patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}), patch(
            "ai_translator.provider._api_open", return_value=io.BytesIO(b"x" * 100),
        ), patch("ai_translator.provider._MAX_API_RESPONSE", 10):
            with self.assertRaisesRegex(ProviderError, "превышает"):
                OpenAIAPI(3).complete(MODEL, "instructions", "{}", SCHEMA)

    def test_openai_does_not_replace_invalid_utf8_in_model_reply(self):
        with patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}), patch(
            "ai_translator.provider._api_open", return_value=io.BytesIO(b'{"status":"completed","output":"\xff"}'),
        ):
            with self.assertRaisesRegex(ProviderError, "UTF-8"):
                OpenAIAPI(3).complete(MODEL, "instructions", "{}", SCHEMA)

    def test_openai_declared_body_length_detects_truncated_valid_json(self):
        payload = (
            b'{"status":"completed","output":[{"type":"message","status":"completed",'
            b'"content":[{"type":"output_text","text":"{}"}]}]}'
        )
        wire = f"HTTP/1.1 200 OK\r\nContent-Length: {len(payload) + 4}\r\n\r\n".encode() + payload
        response = HTTPResponse(Mock(**{"makefile.return_value": io.BytesIO(wire)}))
        response.begin()
        with patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}), patch(
            "ai_translator.provider._api_open", return_value=response,
        ):
            with self.assertRaisesRegex(ProviderError, "прервано до конца ответа") as failure:
                OpenAIAPI(3).complete(MODEL, "instructions", "{}", SCHEMA)
        self.assertIs(type(failure.exception), ProviderError)

    def test_openai_http_error_status_survives_unreadable_error_body(self):
        body = Mock(**{"read.side_effect": IncompleteRead(b"partial")})
        error = HTTPError("https://example.test", 401, "Unauthorized", {}, body)
        with patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}), patch(
            "ai_translator.provider._api_open", side_effect=error,
        ):
            with self.assertRaisesRegex(AuthenticationError, "HTTP 401"):
                OpenAIAPI(3).complete(MODEL, "instructions", "{}", SCHEMA)

    def test_openai_http_error_status_survives_deeply_nested_error_body(self):
        for status, expected in ((401, AuthenticationError), (429, ProviderError)):
            body = io.BytesIO(b"[" * 2000 + b"0" + b"]" * 2000)
            error = HTTPError("https://example.test", status, "Mock error", {}, body)
            with self.subTest(status=status), patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}), patch(
                "ai_translator.provider._api_open", side_effect=error,
            ):
                with self.assertRaisesRegex(expected, f"HTTP {status}") as failure:
                    OpenAIAPI(3).complete(MODEL, "instructions", "{}", SCHEMA)
                self.assertIs(type(failure.exception), expected)
                self.assertTrue(body.closed)

    def test_openai_rejects_invalid_key_without_sending_or_echoing_it(self):
        for key in ("hide-this\ninvalid-key", "hide-this\x7f", "hide-this你"):
            with self.subTest(key=key), patch.dict("os.environ", {"OPENAI_API_KEY": key}), patch(
                "ai_translator.provider._api_open",
            ) as urlopen:
                backend = OpenAIAPI(3)
                with self.assertRaisesRegex(AuthenticationError, "недопустимые символы"):
                    backend.check()
                with self.assertRaises(AuthenticationError) as failure:
                    backend.complete(MODEL, "instructions", "{}", SCHEMA)
                self.assertNotIn("hide-this", str(failure.exception))
                urlopen.assert_not_called()

    def test_request_with_secret_is_not_followed_to_another_url(self):
        seen = []

        class RedirectHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                seen.append(self.path)
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                self.send_response(302)
                self.send_header("Location", "/second")
                self.end_headers()

            def do_GET(self):
                seen.append(self.path)
                self.send_error(400)

            def log_message(self, *args):
                pass

        with HTTPServer(("127.0.0.1", 0), RedirectHandler) as server:
            worker = Thread(target=server.serve_forever)
            worker.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}/first"
                request = Request(url, data=b"book excerpt", headers={"Authorization": "Bearer fake"})
                with _ResponseDeadline(3) as deadline:
                    with self.assertRaises(HTTPError) as failure:
                        _api_open(request, timeout=3, deadline=deadline)
                    failure.exception.close()
                self.assertEqual(failure.exception.code, 302)
                self.assertEqual(seen, ["/first"])
            finally:
                server.shutdown()
                worker.join()

    def test_openai_deadline_stops_slow_body_and_preserves_known_http_error(self):
        for status, expected, message in ((200, ProviderError, "не ответил за 1 с"),
                                          (401, AuthenticationError, "HTTP 401")):
            with self.subTest(status=status):
                self._check_slow_openai_body(status, expected, message)

    def test_openai_deadline_stops_a_proxy_trickling_connect_headers(self):
        stop = Event()
        connect_targets = []

        class SlowProxy(BaseHTTPRequestHandler):
            def do_CONNECT(self):
                connect_targets.append(self.path)
                try:
                    self.wfile.write(b"HTTP/1.1 200 Connection Established\r\nX-Slow: ")
                    for _ in range(80):
                        self.wfile.write(b"x")
                        self.wfile.flush()
                        if stop.wait(0.05):
                            break
                except OSError:
                    pass

            def log_message(self, *args):
                pass

        with HTTPServer(("127.0.0.1", 0), SlowProxy) as server:
            worker = Thread(target=server.serve_forever)
            worker.start()
            proxy = f"http://127.0.0.1:{server.server_port}"
            try:
                with patch.dict("os.environ", {
                    "OPENAI_API_KEY": "local-test", "https_proxy": proxy, "HTTPS_PROXY": proxy,
                    "no_proxy": "", "NO_PROXY": "",
                }):
                    start = monotonic()
                    with self.assertRaisesRegex(ProviderError, "не ответил за 1 с"):
                        OpenAIAPI(1).complete(MODEL, "instructions", "{}", SCHEMA)
                    self.assertLess(monotonic() - start, 2.5)
                self.assertEqual(connect_targets, ["api.openai.com:443"])
            finally:
                stop.set()
                server.shutdown()
                worker.join()

    def test_openai_stops_waiting_for_dns_and_bounds_concurrent_stuck_lookups(self):
        release = Event()

        def delayed(*args, **kwargs):
            release.wait(3)
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 443))]

        with patch("ai_translator.provider._DNS_SLOTS", BoundedSemaphore(2)), patch(
            "ai_translator.provider.socket.getaddrinfo", side_effect=delayed,
        ) as resolve, patch("ai_translator.provider.socket.socket") as connect:
            try:
                start = monotonic()
                for _ in range(3):
                    with _ResponseDeadline(0.15) as deadline:
                        with self.assertRaises(TimeoutError):
                            deadline.dial(("mock.invalid", 443), 1, None)
                self.assertLess(monotonic() - start, 1.5)
                self.assertEqual(resolve.call_count, 2)
                connect.assert_not_called()
            finally:
                release.set()

    def _check_slow_openai_body(self, status, expected, message):
        stop = Event()

        class SlowHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(status)
                self.send_header("Content-Length", "80")
                self.end_headers()
                try:
                    for _ in range(80):
                        self.wfile.write(b" ")
                        self.wfile.flush()
                        if stop.wait(0.05):
                            break
                except OSError:
                    pass

            def log_message(self, *args):
                pass

        with HTTPServer(("127.0.0.1", 0), SlowHandler) as server:
            worker = Thread(target=server.serve_forever)
            worker.start()

            def loopback(request, **kwargs):
                url = f"http://127.0.0.1:{server.server_port}/"
                return _api_open(Request(url, data=request.data), **kwargs)

            try:
                with patch.dict("os.environ", {"OPENAI_API_KEY": "local-test", "no_proxy": "127.0.0.1"}), patch(
                    "ai_translator.provider._api_open", side_effect=loopback,
                ):
                    start = monotonic()
                    with self.assertRaisesRegex(expected, message):
                        OpenAIAPI(1).complete(MODEL, "instructions", "{}", SCHEMA)
                    self.assertLess(monotonic() - start, 2.5)
            finally:
                stop.set()
                server.shutdown()
                worker.join()
