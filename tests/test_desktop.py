import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from unittest import TestCase, skipIf
from unittest.mock import patch

from ai_translator.config import Model, load_config, parse_config
from ai_translator.paths import CACHE_DIRECTORY, default_output, path_too_long
from ai_translator.pipeline import Requests, default_state_path, load_glossary, open_state, read_request_budget

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from ai_translator.desktop import QApplication, GlossaryDialog, QSettings, Window, _confirm_replace, main
    from PySide6.QtCore import QCoreApplication, QEvent, QTimer, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QFileDialog, QMessageBox, QPlainTextEdit
except ImportError:
    QApplication = None


def _process_is_live(pid: int) -> bool:
    if os.name != "nt":
        if sys.platform == "linux":
            try:
                state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
            except (FileNotFoundError, ProcessLookupError):
                return False
            if state == "Z":
                return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True

    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return ctypes.get_last_error() != 87  # ERROR_INVALID_PARAMETER for a nonexistent PID
    try:
        code = wintypes.DWORD()
        return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
    finally:
        kernel.CloseHandle(handle)


@skipIf(QApplication is None, "Нужен Qt для десктопных тестов")
class DesktopTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self._open_window()
        self.addCleanup(self._close_window)
        self.source = self.directory / "книга с пробелами.txt"
        self.source.write_text("她轻轻地推开门。\n", encoding="utf-8")
        self.window.source.setText(str(self.source))
        self.window.auto_glossary.setChecked(False)
        stub = self.directory / ("codex_stub.py" if os.name == "nt" else "codex")
        stub.write_text(f"#!{sys.executable}\n" + """
import json
import sys
import time
from pathlib import Path

if sys.argv[-2:] == ["login", "status"]:
    print("Logged in using ChatGPT")
    sys.exit(0)
payload = json.load(sys.stdin.buffer)
if "等待" in payload.get("blocks", [{}])[0].get("text", ""):
    Path(__file__).with_name(".provider-pid").write_text(str(__import__("os").getpid()))
    time.sleep(20)
if "changed" in payload:
    schema = json.loads(Path(sys.argv[sys.argv.index("--output-schema") + 1]).read_text(encoding="utf-8"))
    fields = schema["properties"]["choices"]["items"]["properties"]
    if "text" in fields:
        response = {"choices": [
            {"id": block["id"], "version": "repaired", "text":
             "Она заговорила с сестрой Гермионы." if "妙丽" in block["text"] else "Она осторожно приоткрыла дверь."}
            for block in payload["blocks"]
        ]}
    else:
        response = {"choices": [{"id": key, "version": "draft"} for key in payload["changed"]]}
else:
    text = "Она бесшумно отворила дверь." if "draft" in payload else "Она тихонько отворила дверь."
    response = {"translations": [
        {"id": block["id"], "text": text + (" Вместе с сестрой Гермионы." if "妙丽" in block["text"] else "")}
        for block in payload["blocks"]
    ]}
print(json.dumps({"type": "turn.started"}))
print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(response)}}))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 20, "output_tokens": 7}}))
""", encoding="utf-8")
        stub.chmod(0o700)
        if os.name == "nt":
            (self.directory / "codex.cmd").write_text(f'@"{sys.executable}" "{stub}" %*\n', encoding="utf-8")
        env = patch.dict(os.environ, {
            "PATH": str(self.directory) + os.pathsep + os.environ["PATH"],
            "CODEX_HOME": str(self.directory),
        })
        env.start()
        self.addCleanup(env.stop)

    def _open_window(self):
        prefs = QSettings(str(self.directory / "desktop.ini"), QSettings.Format.IniFormat)
        with patch("ai_translator.desktop.QSettings", return_value=prefs):
            self.window = Window()
        self.window.show()

    def _close_window(self):
        if self.window.process is not None:
            self.window.cancel()
            self._until(lambda: self.window.process is None)
        self.window.close()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.window = None

    def _until(self, condition):
        deadline = monotonic() + 20
        while not condition() and monotonic() < deadline:
            QTest.qWait(20)
        self.assertTrue(condition(), self.window.log.toPlainText())

    def test_plan_summary_distinguishes_read_only_partial_and_complete_cache(self):
        window = self.window
        window.edit.setChecked(True)
        window.review_edits.setChecked(True)
        window.budget.setValue(0)
        window.inspect.click()
        self._until(lambda: window.process is None)
        self.assertIs(window.results.currentWidget(), window.plan_summary)
        self.assertIn("Доступно новых запросов · 0", window.plan_summary.toPlainText())
        self.assertIn("сверка изменённых пакетов", window.plan_summary.toPlainText())
        state = default_state_path(self.source)
        self.assertFalse(state.exists())

        window.budget.setValue(1)
        self.assertIn("Оценка устарела", window.plan_summary.toPlainText())
        window.run.click()
        self._until(lambda: window.process is None)
        window.inspect.click()
        self._until(lambda: window.process is None)
        self.assertIn("ответов из кеша: 1", window.plan_summary.toPlainText())
        self.assertIn("Запросов к модели: 1", window.run_summary.toPlainText())
        self.assertIn("20 на входе", window.run_summary.toPlainText())
        with open_state(state) as db:
            self.assertEqual(read_request_budget(db), (1, None))

        window.budget.setValue(2)
        window.run.click()
        self._until(lambda: window.process is None)
        window.budget.setValue(0)
        window.inspect.click()
        self._until(lambda: window.process is None)
        self.assertIn("Кеш покрывает весь выбранный запуск", window.plan_summary.toPlainText())
        with open_state(state) as db:
            db.execute("UPDATE request_budget SET ceiling = 3")
            db.commit()
        self._until(lambda: "Оценка устарела" in window.plan_summary.toPlainText())
        window.inspect.click()
        self._until(lambda: window.process is None)
        self.assertIn("В базе · 3 / 3", window.plan_summary.toPlainText())
        profile = self.directory / "new.toml"
        profile.write_text('style = "Простой стиль"\n', encoding="utf-8")
        window._load_profile(profile)
        self.assertIn("Оценка устарела", window.plan_summary.toPlainText())

    def test_filtered_catalog_keyboard_boundaries_and_multi_selection_restore(self):
        window = self.window
        self.source.write_text("".join(f"第{i}章 {'石门' if i in {1, 1000} else '雨'}\n春雨来了。\n"
                                       for i in range(1, 1001)), encoding="utf-8")
        window.resize(730, 590)
        window.operation.setCurrentIndex(window.operation.findData("sample"))
        window.load_chapters.click()
        self._until(lambda: window.process is None)
        window.chapter_search.setText("石门")
        self.assertIn("2 из 1000", window.chapter_count.text())
        QTest.keyClick(window.chapter_search, Qt.Key.Key_Backspace)
        self.assertIsNone(window.process)
        window.chapter_search.setText("1000")
        QTest.keyClick(window.chapter_search, Qt.Key.Key_Return)
        self.assertEqual(window.chapter.currentData(), 1000)
        window.chapter_boundaries.click()
        self._until(lambda: window.process is None)
        self.assertIn("Первый блок · 第1000章 石门", window.run_summary.toPlainText())
        self.assertIn("Последний блок · 春雨来了。", window.run_summary.toPlainText())
        self.assertFalse(default_state_path(self.source).exists())

        selections = []

        def select_first():
            picker = self.app.activeModalWidget()
            picker.search.setText("雨")
            picker.select_visible.click()
            selections.append((picker.count.text(), picker.selection()))
            picker.search.setText("石门")
            picker.clear_visible.click()
            selections.append((picker.count.text(), picker.selection()))
            picker.select_visible.click()
            selections.append((picker.count.text(), picker.selection()))
            picker.list.setCurrentIndex(picker.model.index(0, 0))
            QTest.keyClick(picker.list, Qt.Key.Key_Space)
            selections.append((picker.count.text(), picker.selection()))
            picker.search.setText("雨")
            picker.clear_visible.click()
            picker.search.setText("石门")
            QTest.keyClick(picker.list, Qt.Key.Key_Space)
            picker.accept()

        QTimer.singleShot(0, select_first)
        window.chapter_multiple.click()
        hidden = tuple(range(2, 1000))
        self.assertEqual(selections, [
            ("Совпадений 998 · выбрано 999", (*hidden, 1000)),
            ("Совпадений 2 · выбрано 998", hidden),
            ("Совпадений 2 · выбрано 1000", (1, *hidden, 1000)),
            ("Совпадений 2 · выбрано 999", (*hidden, 1000)),
        ])
        self.assertEqual(window.chapter.currentData(), (1, 1000))
        window.run.click()
        self._until(lambda: window.process is None)
        output = default_output(self.source, "sample", chapter=(1, 1000))
        self.assertEqual(window.last_output, output)
        self.assertEqual(len(window.preview.pairs), 4)
        self._close_window()
        self._open_window()
        self.window.restore_run.click()
        self.assertEqual(self.window.chapter.currentData(), (1, 1000))
        self.assertEqual(Path(self.window.output.text()), output)

    def test_large_selections_are_passed_in_one_worker_file(self):
        window = self.window
        self.source.write_text("".join(f"第{i}章 雨{' 山路' if i <= 4000 else ''}{' 石门' if i % 2 else ''}\n"
                                       f"第{i}段落。\n" for i in range(1, 8001)), encoding="utf-8")
        window.operation.setCurrentIndex(window.operation.findData("sample"))
        window.load_chapters.click()
        self._until(lambda: window.process is None)
        self.assertEqual(len(window.chapter_entries), 8000)
        for query, numbers in (("山路", range(1, 4001)), ("石门", range(1, 8000, 2))):
            with self.subTest(last=numbers[-1]):
                selected = tuple(numbers)

                def select_matches():
                    picker = self.app.activeModalWidget()
                    picker.clear_visible.click()
                    picker.search.setText(query)
                    picker.select_visible.click()
                    picker.accept()

                QTimer.singleShot(0, select_matches)
                window.chapter_multiple.click()
                self.assertEqual(window.chapter.currentData(), selected)
                window.chapter_boundaries.click()
                self.assertIsNotNone(window.process, window.status.text())
                args = window.process.arguments()
                self.assertLess(len(subprocess.list2cmdline([sys.executable, *args]).encode("utf-16-le")) // 2, 2048)
                self.assertNotIn("--chapter", args)
                chapter_file = Path(next(arg.split("=", 1)[1] for arg in args if arg.startswith("--chapter-file=")))
                self.assertEqual(chapter_file.parent, Path(window.job_config.name))
                self.assertEqual(json.loads(chapter_file.read_text(encoding="utf-8")),
                                 {"version": 1, "chapters": list(selected), "revision": window.chapter_revision})
                self._until(lambda: window.process is None)
                self.assertIsNone(window.job_config)
                self.assertFalse(chapter_file.parent.exists())
                self.assertIn("Выбрано глав: 4000", window.run_summary.toPlainText())
                self.assertIn("Первый блок · 第1章 雨", window.run_summary.toPlainText())
                self.assertIn(f"Последний блок · 第{numbers[-1]}段落。", window.run_summary.toPlainText())
                self.assertFalse(default_state_path(self.source).exists())

        window.inspect.click()
        self._until(lambda: window.process is None)
        self.assertIn("выбрано глав: 4000", window.plan_summary.toPlainText().splitlines()[0])
        self.assertIn("Последний блок · 第7999段落。", window.plan_summary.toPlainText())
        self.assertFalse(default_state_path(self.source).exists())

    def test_worker_rejects_a_chapter_file_after_source_changes_and_removes_job_directory(self):
        window = self.window
        original = "第一章 山路\n她走路。\n"
        self.source.write_text(original, encoding="utf-8")
        window.operation.setCurrentIndex(window.operation.findData("sample"))
        window.load_chapters.click()
        self._until(lambda: window.process is None)
        window.chapter.setCurrentIndex(window.chapter.findData(1))
        build_arguments = window._arguments

        def change_source_after_preflight(*args):
            command = build_arguments(*args)
            self.source.write_text("序言\n" + original, encoding="utf-8")
            return command

        with patch.object(window, "_arguments", side_effect=change_source_after_preflight):
            window.run.click()
        self.assertIsNotNone(window.process)
        job_dir = Path(window.job_config.name)
        self._until(lambda: window.process is None)
        self.assertIn("Выбор главы устарел", window.status.text())
        self.assertFalse(job_dir.exists())
        self.assertFalse(default_state_path(self.source).exists())
        self.assertFalse(default_output(self.source, "sample", chapter=1).exists())

    def test_map_editor_can_mark_and_remove_boundary_and_load_result(self):
        self.source.write_bytes("序言\r\nPart One\r\n春雨来了。\r\n\r\nPart Two\r\n她回家了。\r\n".encode("gb18030"))
        window = self.window
        window.operation.setCurrentIndex(window.operation.findData("sample"))
        window.encoding.setText("gb18030")
        destination = self.directory / "book.chapters.json"

        def edit():
            editor = self.app.activeModalWidget()
            editor.line.setValue(2)
            editor.add.click()
            editor.remove.click()
            editor.line.setValue(4)
            editor.add.click()
            editor.accept()

        with patch("ai_translator.desktop_panels.QInputDialog.getText", return_value=("Глава", True)), patch(
            "ai_translator.desktop.QFileDialog.getSaveFileName", return_value=(str(destination), "")
        ):
            QTimer.singleShot(0, edit)
            window.txt_map_editor.click()
            self._until(lambda: window.process is None)
        self.assertEqual(json.loads(destination.read_text(encoding="utf-8"))["anchors"], [{"line": 4, "title": "Глава"}])
        window.chapter.setCurrentIndex(window.chapter.findData(2))
        window.chapter_boundaries.click()
        self._until(lambda: window.process is None)
        self.assertIn("她回家了。", window.run_summary.toPlainText())
        self.assertNotIn("春雨来了。", window.run_summary.toPlainText())

    def test_preview_changes_and_glossary_alias_keep_the_published_pair(self):
        window = self.window
        window.operation.setCurrentIndex(window.operation.findData("sample"))
        self.source.write_text("春雨来了。\n妙丽推开门。\n", encoding="utf-8")
        glossary = self.directory / "terms.json"
        glossary.write_text('{"赫敏":{"ru":"Гермиона","aliases":["妙丽"],"forms":["Гермионы"]}}',
                            encoding="utf-8")
        window.glossary.setText(str(glossary))
        window.edit.setChecked(True)
        window.review_edits.setChecked(True)
        window.repair_review.setChecked(True)
        window.run.click()
        self._until(lambda: window.process is None)
        preview = window.preview
        self.assertIn("Совпадений: 2", preview.change_count.text())
        preview.change_filter.setCurrentIndex(preview.change_filter.findData("repair"))
        preview.next_change.click()
        self.assertEqual(preview.position.value(), 2)
        self.assertEqual(preview.terms(preview._current_pair()), [("赫敏", "妙丽")])
        published = window.last_output.read_bytes()

        observed = []

        def check_context():
            dialog = self.app.activeModalWidget()
            try:
                observed.extend((dialog.table.currentRow(), dialog.table.item(0, 3).text(), dialog.search.text(),
                                 dialog.findChild(QPlainTextEdit).toPlainText()))
            finally:
                dialog.reject()

        QTimer.singleShot(0, check_context)
        preview.term.click()
        self.assertEqual(observed[:3], [0, "Гермионы", "赫敏"])
        self.assertIn("妙丽推开门。", observed[3])
        self.assertIn("Гермионы", observed[3])
        self.assertEqual(window.last_output.read_bytes(), published)
        self.assertEqual(preview.original.toPlainText(), "妙丽推开门。")

    def test_recent_runs_migrate_remain_bounded_and_distinguish_same_book_names(self):
        window = self.window
        old = window._remember_last_run("translate", self.source, default_output(self.source, "translate"),
                                       window._profile_text())
        window.preferences.remove("runHistory")
        window._update_last_run()
        self.assertTrue(window.preferences.contains("runHistory"))
        folder = self.directory / "elsewhere"
        folder.mkdir()
        second = folder / self.source.name
        second.write_text("她回家了。", encoding="utf-8")
        window._remember_last_run("translate", second, default_output(second, "translate"), window._profile_text())
        self.assertEqual([Path(record["source"]) for record in window._history()], [second, self.source])
        window.source.setText(str(second))
        window._restore_last_run(old)
        self.assertEqual(Path(window.source.text()), self.source)
        self.source.rename(self.directory / "renamed.txt")
        window._restore_last_run(old)
        self.assertIn("Исходный файл не найден", window.status.text())
        result = default_output(second, "translate")
        result.write_text("Книга", encoding="utf-8")
        for number in range(22):
            window._remember_last_run("translate", second, folder / f"{number}.txt", window._profile_text())
        self.assertEqual(len(window._history()), 20)
        window.forget_run.click()
        self.assertEqual(window._history(), [])
        self.assertEqual(result.read_text(encoding="utf-8"), "Книга")

    def test_desktop_summary_reports_empty_output_and_paginates_published_findings(self):
        window = self.window
        self.source.write_text("Hello\n", encoding="utf-8")
        window.budget.setValue(0)
        window.run.click()
        self._until(lambda: window.process is None)
        self.assertIn("Китайский текст не найден", window.status.text())
        self.assertIn(str(window.last_output), window.run_summary.toPlainText())
        self.assertIn("Запросов к модели: 0", window.run_summary.toPlainText())
        self.source.write_text("她推开门。\n" * 102, encoding="utf-8")
        window.budget.setValue(10)
        window.edit.setChecked(True)
        window.review_edits.setChecked(True)
        window.repair_review.setChecked(True)
        window.proofreading.setChecked(True)
        with patch("ai_translator.desktop._confirm_replace", return_value=True):
            window.run.click()
            self._until(lambda: window.process is None)
        report = window.review_summary
        self.assertIs(window.results.currentWidget(), report)
        self.assertIn("сверено частей 102 из 102", report.summary.text())
        self.assertEqual(report.page.maximum(), 2)
        self.assertEqual(len(report.rows.toPlainText().splitlines()), 100)
        report.page.stepUp()
        self.assertIn("1/102/1", report.rows.toPlainText())
        self.assertEqual(len(report.rows.toPlainText().splitlines()), 2)
        spool = report.items.file
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self._open_window()
        self.assertTrue(spool.closed)

    def test_switching_and_restoring_books_clears_review_spool_and_boundaries(self):
        window = self.window
        other = self.directory / "другая книга.txt"
        other.write_text("他看见窗外的雨。\n", encoding="utf-8")
        restored = window._remember_last_run("translate", other, default_output(other, "translate"),
                                             window._profile_text())
        self.source.write_text("她推开门。\n" * 102, encoding="utf-8")
        window.edit.setChecked(True)
        window.review_edits.setChecked(True)
        window.proofreading.setChecked(True)
        window.run.click()
        self._until(lambda: window.process is None)
        report = window.review_summary
        self.assertIs(window.results.currentWidget(), report)
        report.page.setValue(2)
        self.assertEqual(report.page.value(), 2)
        self.assertIn("1/102/1", report.rows.toPlainText())
        self.assertIn("她推开门。", report.rows.toPlainText())
        spool = report.items.file
        self.assertFalse(spool.closed)
        self.assertIn(str(window.last_output), window.run_summary.toPlainText())

        window.source.setText(str(other))
        self.assertTrue(spool.closed)
        self.assertIsNone(report.items)
        self.assertEqual(report.rows.toPlainText(), "")
        self.assertEqual((report.page.value(), report.page.maximum()), (1, 1))
        self.assertFalse(report.page.isEnabled())
        self.assertNotIn(str(self.source), report.summary.text())
        self.assertEqual(window.run_summary.toPlainText(), "")

        window.source.setText(str(self.source))
        window.operation.setCurrentIndex(window.operation.findData("sample"))
        window.chapter_boundaries.click()
        self._until(lambda: window.process is None)
        self.assertIn("她推开门。", window.run_summary.toPlainText())
        window._restore_last_run(restored)
        self.assertEqual(window.source.text(), str(other))
        self.assertEqual(window.run_summary.toPlainText(), "")
        self.assertEqual(report.rows.toPlainText(), "")

    def test_worker_for_previous_source_cannot_publish_results_after_switch(self):
        window = self.window
        other = self.directory / "другая книга.txt"
        other.write_text("他看见窗外的雨。\n", encoding="utf-8")
        window.edit.setChecked(True)
        window.review_edits.setChecked(True)
        window.proofreading.setChecked(True)
        read_events = window._read_events
        staged = []

        def switch_after_findings_arrive():
            read_events()
            if window.pending_review is not None:
                staged.append(window.pending_review.file)
                window.source.setText(str(other))
                window.cancel()

        with patch.object(window, "_read_events", side_effect=switch_after_findings_arrive):
            window.run.click()
            self.assertIsNotNone(window.process, window.status.text())
            self._until(lambda: window.process is None)
        self.assertEqual(len(staged), 1, window.log.toPlainText())
        self.assertTrue(staged[0].closed)
        self.assertTrue(default_output(self.source, "translate").is_file(), window.log.toPlainText())
        self.assertIsNone(window.review_summary.items)
        self.assertIsNone(window.pending_review)
        self.assertEqual(window.review_summary.rows.toPlainText(), "")
        self.assertEqual(window.run_summary.toPlainText(), "")
        self.assertFalse(window.open_result.isEnabled())
        self.assertFalse(window.budget_status.isVisible())
        self.assertEqual(window.status.text(), "Выберите действие для текущей книги.")

        window.source.setText(str(self.source))
        window.operation.setCurrentIndex(window.operation.findData("sample"))
        window.chapter_boundaries.click()
        self.assertIsNotNone(window.process)
        window.source.setText(str(other))
        window.source.setText(str(self.source))
        self._until(lambda: window.process is None)
        self.assertIn("她轻轻地推开门。", window.log.toPlainText())
        self.assertEqual(window.run_summary.toPlainText(), "")

    def test_launcher_opens_the_book_named_on_the_command_line(self):
        book = "-книга с пробелами.epub"
        with patch("ai_translator.desktop.QApplication") as app, patch(
            "ai_translator.desktop.Window", return_value=self.window,
        ):
            app.return_value.exec.return_value = 0
            self.assertEqual(main(["--", book]), 0)
        self.assertEqual(self.window.source.text(), book)

    def test_long_book_title_uses_same_default_output_as_the_worker(self):
        source = self.directory / ("雨" * (251 if os.name == "nt" else 83) + ".txt")
        try:
            source.write_text("她轻轻地推开门。\n", encoding="utf-8")
        except OSError as exc:
            if path_too_long(source):
                self.skipTest(str(exc))
            raise
        if not path_too_long(source.with_name(source.stem + ".sample.ru.txt")):
            self.skipTest("Файловая система допускает длинное имя пробы.")
        self.window.source.setText(str(source))
        self.window.operation.setCurrentIndex(self.window.operation.findData("sample"))
        output = default_output(source, "sample")
        self.assertEqual(self.window.output.placeholderText(), str(output))
        cache_directory = source.parent / CACHE_DIRECTORY
        cache_directory.write_text("obstruction", encoding="utf-8")
        self.window.run.click()
        self.assertIsNone(self.window.process)
        self.assertIn("Для служебных файлов требуется обычная папка", self.window.status.text())
        cache_directory.unlink()
        self.window.run.click()
        self._until(lambda: self.window.process is None)
        self.assertEqual(self.window.last_output, output)
        self.assertEqual(self.window.preview.original.toPlainText(), "她轻轻地推开门。")
        self.assertEqual(default_state_path(source).parent, source.parent / CACHE_DIRECTORY / source.name)
        self.assertTrue(default_state_path(source).is_file())
        too_long = source.with_name(source.stem + ".sample.ru.txt")
        self.window.output.setText(str(too_long))
        self.window.run.click()
        self.assertIsNone(self.window.process)
        self.assertIn("Слишком длинный путь к выходному файлу", self.window.status.text())

    def test_doctor_reports_invalid_api_key_without_exposing_it(self):
        self.window.provider.setCurrentIndex(self.window.provider.findData("openai-api"))
        with patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic-secret\ninvalid"}):
            self.window.check_login.click()
            self._until(lambda: self.window.process is None)
        self.assertIn("недопустимые символы", self.window.status.text())
        self.assertNotIn("synthetic-secret", self.window.log.toPlainText())

    def test_doctor_displays_successful_verification_in_the_status(self):
        self.window.check_login.click()
        self._until(lambda: self.window.process is None)
        self.assertEqual(self.window.status.text(), "Вход через codex подтверждён.")
        self.assertFalse(default_state_path(self.source).exists())

    def test_incompatible_paths_are_reported_before_overwrite_confirmation(self):
        original = self.source.read_bytes()
        with patch("ai_translator.desktop._confirm_replace") as confirm:
            self.window.output.setText(str(self.source))
            self.window.run.click()
            self.assertIn("должны различаться", self.window.status.text())
            self.assertIsNone(self.window.process)

            self.window.output.clear()
            self.window.cache.setText(str(self.source))
            self.window.run.click()
            self.assertIn("Кеш должен храниться отдельно", self.window.status.text())
            self.assertIsNone(self.window.process)

            self.window.cache.clear()
            glossary = self.directory / "Исходный глоссарий.json"
            glossary.write_text("{}", encoding="utf-8")
            self.window.glossary.setText(str(glossary))
            self.window.save_glossary.setText(str(glossary))
            self.window.run.click()
            self.assertIn("должны различаться", self.window.status.text())
            self.assertIsNone(self.window.process)
            confirm.assert_not_called()
        self.assertEqual(self.source.read_bytes(), original)
        self.assertEqual(glossary.read_text(encoding="utf-8"), "{}")
        self.assertFalse(default_state_path(self.source).exists())

    def test_cache_directory_is_reported_before_overwrite_confirmation(self):
        output = default_output(self.source, "translate")
        output.write_text("Предыдущий перевод", encoding="utf-8")
        self.window.cache.setText(str(self.directory))
        with patch("ai_translator.desktop._confirm_replace") as confirm:
            self.window.run.click()
            confirm.assert_not_called()
        self.assertIsNone(self.window.process)
        self.assertIn("Для кеша нужен путь к файлу SQLite", self.window.status.text())
        self.assertEqual(output.read_text(encoding="utf-8"), "Предыдущий перевод")

    def test_chapter_can_be_selected_inspected_run_and_restored_in_desktop(self):
        source = self.directory / "Книга с главами.fb2"
        source.write_text('<FictionBook><body><section><title><p>山路</p></title><p>哈利来了。</p></section></body>'
                          '<body><title><p>石门</p></title>'
                          '<section><p>赫敏来了。</p><p>灯熄灭了。</p></section></body></FictionBook>',
                          encoding="utf-8")
        self.window.source.setText(str(source))
        self.window.operation.setCurrentIndex(self.window.operation.findData("sample"))
        self.window.load_chapters.click()
        self._until(lambda: self.window.process is None)
        self.assertFalse(default_state_path(source).exists())
        self.assertEqual(self.window.chapter.count(), 4)
        self.window.chapter.setCurrentIndex(self.window.chapter.findData(2))
        self.assertEqual(self.window.chapter.currentText(), "2. 石门")
        self.assertFalse(self.window.batches.isEnabled())
        self.assertFalse(self.window.start_batch.isEnabled())
        self.window.inspect.click()
        self._until(lambda: self.window.process is None)
        self.assertIn("Глава 2 · 石门", self.window.log.toPlainText())
        self.assertFalse(default_state_path(source).exists())
        self.window.run.click()
        self._until(lambda: self.window.process is None)
        output = default_output(source, "sample", chapter=2)
        self.assertEqual(self.window.last_output, output)
        self.assertEqual(self.window.preview.original.toPlainText(), "石门")
        published = output.read_text(encoding="utf-8")
        self.assertIn("赫敏来了。", published)
        self.assertIn("灯熄灭了。", published)
        self.assertNotIn("哈利来了。", published)
        self.window.chapter.setCurrentIndex(self.window.chapter.findData(1))
        self.assertFalse(self.window.open_result.isEnabled())
        self.assertEqual(self.window.preview.original.toPlainText(), "")

        self._close_window()
        self._open_window()
        self.window.restore_run.click()
        self.assertEqual(self.window.chapter.currentData(), 2)
        self.assertEqual(self.window.output.text(), str(output))
        self.window.source.setText(str(self.source))
        self.assertEqual(self.window.chapter.count(), 1)

    def test_desktop_requires_reselecting_a_saved_chapter_after_book_changes(self):
        source = self.directory / "Книга.txt"
        old_text = "第一章 山路\n哈利来了。\n第二章 石门\n赫敏来了。\n"
        source.write_text(old_text, encoding="utf-8")
        self.window.source.setText(str(source))
        self.window.operation.setCurrentIndex(self.window.operation.findData("sample"))
        self.window.load_chapters.click()
        self._until(lambda: self.window.process is None)
        self.window.chapter.setCurrentIndex(self.window.chapter.findData(2))
        output = self.directory / "B.txt"
        self.window.output.setText(str(output))
        self.window.run.click()
        self._until(lambda: self.window.process is None)
        published = output.read_bytes()
        source.write_text("第一章 新引子\n雨停了。\n" + old_text, encoding="utf-8")

        self._close_window()
        self._open_window()
        self.window.restore_run.click()
        self.assertIn("Загрузите оглавление заново", self.window.status.text())
        self.assertEqual(self.window.chapter.currentData(), 2)
        with patch("ai_translator.desktop._confirm_replace") as confirm:
            self.window.run.click()
            self.assertIsNone(self.window.process)
            self.window.inspect.click()
            self.assertIsNone(self.window.process)
            confirm.assert_not_called()
        self.assertEqual(output.read_bytes(), published)
        self.window.load_chapters.click()
        self._until(lambda: self.window.process is None)
        self.assertIsNone(self.window.chapter.currentData())
        self.assertEqual(self.window.chapter.itemText(self.window.chapter.findData(3)), "3. 第二章 石门")
        self.window.chapter.setCurrentIndex(self.window.chapter.findData(2))
        self.assertEqual(self.window.output.text(), "")
        self.assertEqual(self.window.output.placeholderText(), str(default_output(source, "sample", chapter=2)))
        self.window.chapter.setCurrentIndex(self.window.chapter.findData(3))
        self.window.run.click()
        self._until(lambda: self.window.process is None)
        self.assertEqual(default_output(source, "sample", chapter=3).read_bytes(), published)

    def test_reports_broken_publication_journal_before_opening_cache(self):
        output = self.directory / "existing.txt"
        output.write_text("Старая книга", encoding="utf-8")
        journal = self.directory / f".ai-translator-transaction-{'0' * 32}.json"
        journal.write_text('{"committed":false}', encoding="utf-8")
        output.with_name(f".{output.name}.ai-translator-publish.json").write_text(str(journal), encoding="utf-8")
        self.window.output.setText(str(output))
        self.window.save_glossary.setText(str(self.directory / "names.json"))
        with patch("ai_translator.desktop._confirm_replace", return_value=True):
            self.window.run.click()
        self._until(lambda: self.window.process is None)
        self.assertIn("Повреждён журнал", self.window.status.text())
        self.assertEqual(output.read_text(encoding="utf-8"), "Старая книга")
        self.assertFalse(default_state_path(self.source).is_file())

    def test_worker_accepts_relative_path_options_starting_with_dash(self):
        source = Path("-雨.txt")
        glossary = "-имена.json"
        saved = "-словарь.json"
        output = Path("-雨.sample.ru.txt")
        (self.directory / source).write_text("她轻轻地推开门。\n", encoding="utf-8")
        (self.directory / glossary).write_text("{}", encoding="utf-8")
        for path in (output, saved):
            (self.directory / path).write_text("Старый результат", encoding="utf-8")
        config = self.directory / "profile.toml"
        config.write_text(self.window._profile_text(), encoding="utf-8")
        self.window.glossary.setText(glossary)
        self.window.save_glossary.setText(saved)
        self.window.cache.setText("-кеш")
        command = self.window._arguments("sample", config, source, output, [str(output), saved])
        run = subprocess.run([sys.executable, *command], cwd=self.directory, capture_output=True,
                             encoding="utf-8", timeout=12, check=False)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("Она тихонько отворила дверь", (self.directory / output).read_text(encoding="utf-8"))
        self.assertEqual(load_glossary(self.directory / saved), {})
        self.assertTrue((self.directory / "-кеш").is_file())
        self.assertFalse(default_state_path(self.directory / source).exists())

    def test_inspects_resumes_and_restores_cli_job_with_custom_cache(self):
        config = self.directory / "profile.toml"
        config.write_text(self.window._profile_text(), encoding="utf-8")
        cache = self.directory / "-общий кеш.sqlite3"
        first_output = self.directory / "cli.txt"
        run = subprocess.run(
            [sys.executable, "-m", "ai_translator.cli", "sample", f"--config={config}",
             f"--state={cache.name}", f"--output={first_output}", "--no-auto-glossary",
             "--max-calls", "1", "--max-total-calls", "1", "--", str(self.source)],
            cwd=self.directory, capture_output=True, encoding="utf-8", timeout=12, check=False,
        )
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

        self.window.cache.setText(str(cache))
        self.window.operation.setCurrentIndex(self.window.operation.findData("sample"))
        snapshot = cache.read_bytes()
        self.window.inspect.click()
        self._until(lambda: self.window.process is None)
        self.assertIn("Кеш покрывает весь выбранный запуск", self.window.log.toPlainText())
        self.assertIn("1 / 1", self.window.budget_status.text())
        self.assertEqual(cache.read_bytes(), snapshot)

        output = self.directory / "qt.txt"
        self.window.output.setText(str(output))
        self.window.budget.setValue(0)
        self.window.run.click()
        self._until(lambda: self.window.process is None)
        self.assertEqual(output.read_bytes(), first_output.read_bytes())
        self.assertIn("Запросов к модели: 0. Ответов из кеша: 1.", self.window.log.toPlainText())
        self.assertFalse(default_state_path(self.source).exists())

        self._close_window()
        self._open_window()
        self.window.restore_run.click()
        self.assertEqual(self.window.cache.text(), str(cache))
        self.assertEqual(self.window.output.text(), str(output))
        self.assertEqual(self.window.budget.value(), 0)
        self.assertTrue(self.window.budget_status.isVisible())
        self.assertIn("1 / 1", self.window.budget_status.text())
        self.assertIsNone(self.window.process)

        self.window.total_budget_mode.setCurrentIndex(1)
        self.window.cache.setText(str(self.directory / "другой кеш.sqlite3"))
        self.assertEqual(self.window.total_budget_mode.currentIndex(), 0)
        self.assertFalse(self.window.budget_status.isVisible())
        self.window.source.setText(str(self.directory / "другая книга.txt"))
        self.assertEqual(self.window.cache.text(), "")

    def test_cli_resumes_desktop_edit_with_custom_cache_and_shared_budget(self):
        cache = self.directory / "общий кеш.sqlite3"
        self.window.cache.setText(str(cache))
        self.window.operation.setCurrentIndex(self.window.operation.findData("sample"))
        self.window.edit.setChecked(True)
        self.window.budget.setValue(1)
        self.window.total_budget_mode.setCurrentIndex(1)
        self.window.total_budget.setValue(2)
        config = self.directory / "profile.toml"
        config.write_text(self.window._profile_text(), encoding="utf-8")
        self.window.run.click()
        self._until(lambda: self.window.process is None)
        self.assertIn("Запросов к модели: 1. Ответов из кеша: 0.", self.window.log.toPlainText())
        self.assertIn("1 / 2", self.window.budget_status.text())

        output = self.directory / "cli.txt"
        self.assertFalse(self.source.with_suffix(".sample.ru.txt").exists())
        run = subprocess.run(
            [sys.executable, "-m", "ai_translator.cli", "sample", f"--config={config}", f"--state={cache}",
             f"--output={output}", "--no-auto-glossary", "--edit", "--max-calls=1", "--json-events",
             "--", str(self.source)],
            cwd=self.directory, capture_output=True, encoding="utf-8", timeout=12, check=False,
        )
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        events = {event["event"]: event for event in map(json.loads, run.stdout.splitlines())}
        self.assertEqual(events["requests"]["calls"], 1)
        self.assertEqual(events["requests"]["cache_hits"], 1)
        self.assertEqual(events["request_budget"]["attempts"], 2)
        self.assertEqual(events["request_budget"]["ceiling"], 2)
        self.assertIn("Она бесшумно отворила дверь.", output.read_text(encoding="utf-8"))
        self.assertFalse(default_state_path(self.source).exists())

    def test_auth_inspect_sample_full_reuse_and_overwrite_confirmation(self):
        self.window.start("doctor")
        self._until(lambda: self.window.process is None)
        self.assertIn("подтверждён", self.window.log.toPlainText())
        self.window.start("inspect")
        self._until(lambda: self.window.process is None)
        self.assertIn("Пакетов: 1", self.window.log.toPlainText())
        self.window.operation.setCurrentIndex(self.window.operation.findData("sample"))
        self.window.run.click()
        self._until(lambda: self.window.process is None)
        self.assertEqual(self.window.preview.original.toPlainText(), "她轻轻地推开门。")
        self.assertIn("Она тихонько отворила дверь", self.window.preview.translation.toPlainText())
        self.window.operation.setCurrentIndex(self.window.operation.findData("translate"))
        self.window.budget.setValue(0)
        self.window.run.click()
        self._until(lambda: self.window.process is None)
        self.assertTrue(self.window.last_output.is_file())
        self.assertIn("Запросов к модели: 0", self.window.log.toPlainText())
        self.assertTrue(self.window.open_result.isEnabled())
        with patch("ai_translator.desktop._confirm_replace", return_value=False):
            self.window.run.click()
        self.assertIsNone(self.window.process)
        self.window.start("inspect")
        self._until(lambda: self.window.process is None)
        self.assertIn("Кеш покрывает весь выбранный запуск", self.window.log.toPlainText())

    def test_preview_navigates_pairs_and_clears_when_no_blocks_are_found(self):
        self.source.write_text("她轻轻地推开门。\n他看见窗外的雨。\n", encoding="utf-8")
        self.window.operation.setCurrentIndex(self.window.operation.findData("sample"))
        self.window.run.click()
        self._until(lambda: self.window.process is None)
        preview = self.window.preview
        self.assertIs(self.window.results.currentWidget(), preview)
        self.assertEqual(preview.position.maximum(), 2)
        self.assertFalse(preview.previous.isEnabled())
        preview.next.click()
        self.assertEqual(preview.original.toPlainText(), "他看见窗外的雨。")
        self.assertIn("Она тихонько отворила дверь", preview.translation.toPlainText())
        self.assertFalse(preview.next.isEnabled())
        preview.previous.click()
        self.assertEqual(preview.original.toPlainText(), "她轻轻地推开门。")
        self.assertNotIn("她轻轻地推开门。", self.window.log.toPlainText())

        empty = self.directory / "empty.txt"
        empty.write_text("Already in English.\n", encoding="utf-8")
        self.window.source.setText(str(empty))
        self.window.run.click()
        self._until(lambda: self.window.process is None)
        self.assertEqual(preview.filename.text(), "empty.sample.ru.txt")
        self.assertFalse(preview.position.isEnabled())
        self.assertEqual(preview.original.toPlainText(), "")
        self.assertEqual(preview.translation.toPlainText(), "")

    def test_output_path_follows_book_and_operation(self):
        epub = self.directory / "book.epub"
        epub.touch()
        self.window.source.setText(str(epub))
        self.window.output.setText(str(self.directory / "published.epub"))
        self.window.operation.setCurrentIndex(self.window.operation.findData("sample"))
        self.assertEqual(self.window.output.text(), "")
        self.window.output.setText(str(self.directory / "example.txt"))
        self.window.operation.setCurrentIndex(self.window.operation.findData("translate"))
        self.assertEqual(Path(self.window.output.text()).name, "published.epub")
        self.window.source.setText(str(self.source))
        self.assertEqual(self.window.output.text(), "")
        self.window.source.setText(str(epub))
        self.assertEqual(Path(self.window.output.text()).name, "published.epub")
        self.window.operation.setCurrentIndex(self.window.operation.findData("sample"))
        self.assertEqual(Path(self.window.output.text()).name, "example.txt")
        self.window.output.setText(str(self.directory / "wrong.epub"))
        self.window.run.click()
        self.assertIsNone(self.window.process)
        self.assertIn(".TXT", self.window.status.text())

    def test_switching_books_cannot_open_or_preview_an_earlier_result(self):
        self.window.operation.setCurrentIndex(self.window.operation.findData("sample"))
        self.window.run.click()
        self._until(lambda: self.window.process is None)
        self.assertTrue(self.window.open_result.isEnabled())
        other = self.directory / "другая книга.txt"
        other.write_text("他看见窗外的雨。\n", encoding="utf-8")
        self.window.source.setText(str(other))
        self.assertEqual(self.window.preview.original.toPlainText(), "")
        self.assertFalse(self.window.open_result.isEnabled())
        self.window.budget.setValue(0)
        self.window.run.click()
        self._until(lambda: self.window.process is None)
        self.assertIn("лимит", self.window.status.text())
        self.assertFalse(self.window.open_result.isEnabled())
        self.assertEqual(self.window.preview.translation.toPlainText(), "")

    def test_switching_sample_batches_clears_the_stale_result(self):
        first, second, third = ("雨" * 380 + "。", "门" * 380 + "。", "灯" * 380 + "。")
        self.source.write_text(f"{first}\n{second}\n{third}\n", encoding="utf-8")
        self.window.batch_size.setValue(400)
        self.window.operation.setCurrentIndex(self.window.operation.findData("sample"))
        self.window.run.click()
        self._until(lambda: self.window.process is None)
        self.assertTrue(self.window.open_result.isEnabled())
        self.assertEqual(self.window.preview.original.toPlainText(), first)

        self.window.start_batch.setValue(2)
        self.assertFalse(self.window.open_result.isEnabled())
        self.assertEqual(self.window.preview.original.toPlainText(), "")
        target = self.directory / "second.txt"
        self.window.output.setText(str(target))
        self.window.run.click()
        self._until(lambda: self.window.process is None)
        self.assertTrue(self.window.open_result.isEnabled())
        self.assertEqual(self.window.preview.original.toPlainText(), second)
        self.window.batches.setValue(2)
        self.assertFalse(self.window.open_result.isEnabled())
        self.assertEqual(self.window.preview.original.toPlainText(), "")
        self.assertIn(second, target.read_text(encoding="utf-8"))

    def test_glossary_paths_follow_the_book(self):
        original = self.directory / "approved.json"
        original.write_text("{}", encoding="utf-8")
        output = self.directory / "collected.json"
        self.window.glossary.setText(str(original))
        self.window.save_glossary.setText(str(output))
        other = self.directory / " другая книга.txt"
        other.write_text("有人进来了。\n", encoding="utf-8")
        self.window.source.setText(str(other))
        self.assertEqual(self.window.glossary.text(), "")
        self.assertEqual(self.window.save_glossary.text(), "")
        self.window.run.click()
        self._until(lambda: self.window.process is None)
        self.assertFalse(output.exists())
        self.window.source.setText(str(self.source))
        self.assertEqual(self.window.glossary.text(), str(original))
        self.assertEqual(self.window.save_glossary.text(), str(output))

    def test_desktop_refreshes_committed_ceiling_after_kill_before_acknowledgment(self):
        script = """
import sys
import time
from pathlib import Path
from ai_translator.config import load_config
from ai_translator.pipeline import Requests, default_state_path, open_state
source = Path(sys.argv[1])
with open_state(default_state_path(source)) as db:
    Requests(db, load_config(None, None), max_total_calls=12)
    source.with_suffix(".ready").touch()
    time.sleep(20)
"""
        self.window.total_budget_mode.setCurrentIndex(1)
        with patch.object(self.window, "_arguments", return_value=["-c", script, str(self.source)]):
            self.window.run.click()
        self._until(lambda: self.source.with_suffix(".ready").exists())
        self.window.process.kill()
        self._until(lambda: self.window.process is None)
        self.assertEqual(self.window.total_budget_mode.currentIndex(), 0)
        self.assertIn("0 / 12", self.window.budget_status.text())
        self.window.restore_run.click()
        self.assertEqual(self.window.total_budget_mode.currentIndex(), 0)
        self.assertNotIn("pending_total_limit", json.loads(self.window.preferences.value("lastRun")))

    def test_desktop_retains_tighter_ceiling_after_worker_killed_before_commit(self):
        cache = self.directory / "общий кеш.sqlite3"
        with open_state(cache) as db:
            Requests(db, load_config(None, None))
        ready = self.directory / "ready"
        script = """
import sys
import time
from pathlib import Path
from ai_translator.pipeline import open_state
with open_state(Path(sys.argv[1])) as db:
    db.execute("UPDATE request_budget SET ceiling = 0 WHERE id = 1")
    Path(sys.argv[2]).touch()
    time.sleep(20)
"""
        self.window.cache.setText(str(cache))
        self.window.total_budget_mode.setCurrentIndex(1)
        self.window.total_budget.setValue(0)
        with patch.object(self.window, "_arguments", return_value=["-c", script, str(cache), str(ready)]):
            self.window.run.click()
        self._until(ready.exists)
        self.window.process.kill()
        self._until(lambda: self.window.process is None)
        self.assertEqual(self.window.total_budget_mode.currentIndex(), 1)
        with open_state(cache) as db:
            self.assertEqual(read_request_budget(db), (0, None))

        self._close_window()
        self._open_window()
        self.window.restore_run.click()
        self.assertEqual(self.window.cache.text(), str(cache))
        self.assertEqual(self.window.total_budget_mode.currentIndex(), 1)
        self.assertEqual(self.window.total_budget.value(), 0)
        self.window.run.click()
        self._until(lambda: self.window.process is None)
        self.assertIn("Запросов к модели: 0", self.window.log.toPlainText())
        with open_state(cache) as db:
            self.assertEqual(read_request_budget(db), (0, 0))
        self.assertNotIn("pending_total_limit", json.loads(self.window.preferences.value("lastRun")))

    def test_restored_limit_preserves_stricter_budget_set_in_cli_after_restore(self):
        cache = self.directory / "общий кеш.sqlite3"
        self.window.cache.setText(str(cache))
        self.window.budget.setValue(0)
        self.window.total_budget_mode.setCurrentIndex(1)
        self.window.total_budget.setValue(10)
        # Save the same snapshot as a window interrupted before the worker's acknowledgment.
        snapshot = self.window._remember_last_run("translate", self.source, default_output(self.source, "translate"),
                                                  self.window._profile_text())
        self.assertIsNotNone(snapshot)
        self.window.restore_run.click()
        self.assertEqual(self.window.total_budget_mode.currentText(), "Ужесточить")
        self.assertEqual(self.window.total_budget.value(), 10)

        run = subprocess.run(
            [sys.executable, "-m", "ai_translator.cli", "translate", f"--state={cache}", "--no-auto-glossary",
             "--max-calls=0", "--max-total-calls=2", "--", str(self.source)],
            cwd=self.directory, capture_output=True, encoding="utf-8", timeout=12, check=False,
        )
        self.assertEqual(run.returncode, 3, run.stdout + run.stderr)
        self.window.run.click()
        self._until(lambda: self.window.process is None)
        self.assertIn("0 / 2", self.window.budget_status.text())
        self.assertEqual(self.window.total_budget_mode.currentIndex(), 0)
        self.assertNotIn("pending_total_limit", json.loads(self.window.preferences.value("lastRun")))
        with open_state(cache) as db:
            self.assertEqual(read_request_budget(db), (0, 2))

    def test_desktop_persists_total_budget_for_cli_worker(self):
        self.window.total_budget_mode.setCurrentIndex(1)
        self.window.total_budget.setValue(0)
        self.window.run.click()
        self._until(lambda: self.window.process is None)
        self.assertIn("Исчерпан общий лимит", self.window.status.text())
        self.assertIn("0 / 0", self.window.budget_status.text())
        self.assertEqual(self.window.total_budget_mode.currentIndex(), 0)
        self.window.total_budget_mode.setCurrentIndex(0)
        self.window.run.click()
        self._until(lambda: self.window.process is None)
        self.assertIn("Исчерпан общий лимит", self.window.status.text())
        self.window.total_budget_mode.setCurrentIndex(2)
        self.window.run.click()
        self._until(lambda: self.window.process is None)
        self.assertTrue(self.window.last_output.is_file())
        self.assertIn("· 1", self.window.budget_status.text())
        self.window.edit.setChecked(True)
        with patch("ai_translator.desktop._confirm_replace", return_value=True):
            self.window.run.click()
        self._until(lambda: self.window.process is None)
        self.assertIn("· 2", self.window.budget_status.text())

    def test_invalid_glossary_keeps_unsaved_budget_selection_across_restart(self):
        glossary = self.directory / "unreadable.json"
        glossary.write_text("{", encoding="utf-8")
        cache = self.directory / "общий кеш.sqlite3"
        self.window.cache.setText(str(cache))
        self.window.glossary.setText(str(glossary))
        self.window.total_budget_mode.setCurrentIndex(1)
        self.window.total_budget.setValue(0)
        self.window.run.click()
        self._until(lambda: self.window.process is None)
        self.assertIn("Ошибка", self.window.status.text())
        self.assertEqual(self.window.total_budget_mode.currentIndex(), 1)
        self.assertEqual(self.window.total_budget.value(), 0)
        self.assertFalse(cache.exists())

        self._close_window()
        self._open_window()
        self.window.restore_run.click()
        self.assertEqual(self.window.cache.text(), str(cache))
        self.assertEqual(self.window.total_budget_mode.currentIndex(), 1)
        self.assertEqual(self.window.total_budget.value(), 0)
        self.assertIsNone(self.window.process)

        glossary.write_text("{}", encoding="utf-8")
        self.window.run.click()
        self._until(lambda: self.window.process is None)
        self.assertIn("Исчерпан общий лимит", self.window.status.text())
        self.assertIn("Запросов к модели: 0", self.window.log.toPlainText())
        self.assertIn("0 / 0", self.window.budget_status.text())
        self.assertEqual(self.window.total_budget_mode.currentIndex(), 0)
        self.window.restore_run.click()
        self.assertEqual(self.window.total_budget_mode.currentIndex(), 0)

    def test_sqlite_rejected_budget_change_survives_restart(self):
        cache = self.directory / "общий кеш.sqlite3"
        with open_state(cache) as db:
            Requests(db, load_config(None, None))
            db.execute(
                "CREATE TRIGGER reject_budget BEFORE UPDATE OF ceiling ON request_budget "
                "BEGIN SELECT RAISE(ABORT, 'test budget write rejected'); END"
            )
        self.window.cache.setText(str(cache))
        self.window.total_budget_mode.setCurrentIndex(1)
        self.window.total_budget.setValue(0)
        self.window.run.click()
        self._until(lambda: self.window.process is None)
        self.assertIn("Запись общего лимита…", self.window.log.toPlainText())
        self.assertIn("test budget write rejected", self.window.status.text())
        self.assertEqual(self.window.total_budget_mode.currentIndex(), 1)
        with open_state(cache) as db:
            self.assertEqual(read_request_budget(db), (0, None))
            db.execute("DROP TRIGGER reject_budget")

        self._close_window()
        self._open_window()
        self.window.restore_run.click()
        self.assertEqual(self.window.cache.text(), str(cache))
        self.assertEqual(self.window.total_budget_mode.currentIndex(), 1)
        self.assertEqual(self.window.total_budget.value(), 0)
        self.window.run.click()
        self._until(lambda: self.window.process is None)
        self.assertIn("Исчерпан общий лимит", self.window.status.text())
        self.assertIn("Запросов к модели: 0", self.window.log.toPlainText())
        self.assertEqual(self.window.total_budget_mode.currentIndex(), 0)
        with open_state(cache) as db:
            self.assertEqual(read_request_budget(db), (0, 0))

    def test_failed_set_limit_retries_without_increasing_a_newer_cli_ceiling(self):
        cache = self.directory / "общий кеш.sqlite3"
        glossary = self.directory / "невалидный.json"
        glossary.write_text("{", encoding="utf-8")
        self.window.cache.setText(str(cache))
        self.window.glossary.setText(str(glossary))
        self.window.budget.setValue(0)
        self.window.total_budget_mode.setCurrentIndex(1)
        self.window.total_budget.setValue(10)
        self.window.run.click()
        self._until(lambda: self.window.process is None)
        self.assertIn("Ошибка", self.window.status.text())
        self.assertEqual(self.window.total_budget_mode.currentText(), "Ужесточить")

        self._close_window()
        self._open_window()
        self.window.restore_run.click()
        self.assertEqual(self.window.total_budget_mode.currentText(), "Ужесточить")
        run = subprocess.run(
            [sys.executable, "-m", "ai_translator.cli", "translate", f"--state={cache}", "--no-auto-glossary",
             "--max-calls=0", "--max-total-calls=2", "--", str(self.source)],
            cwd=self.directory, capture_output=True, encoding="utf-8", timeout=12, check=False,
        )
        self.assertEqual(run.returncode, 3, run.stdout + run.stderr)
        glossary.write_text("{}", encoding="utf-8")
        self.window.run.click()
        self._until(lambda: self.window.process is None)
        self.assertIn("0 / 2", self.window.budget_status.text())
        with open_state(cache) as db:
            self.assertEqual(read_request_budget(db), (0, 2))

    def test_restores_interrupted_sample_in_a_new_window_without_rerunning_draft(self):
        self.source.write_text("她轻轻地推开门。\n" * 64 + "他看见窗外的雨。\n", encoding="utf-8")
        glossary = self.directory / "имена.json"
        glossary.write_text('{"窗":"окно"}', encoding="utf-8")
        saved_glossary = self.directory / "словарь.json"
        output = self.directory / "моя проба.txt"
        self.window.operation.setCurrentIndex(self.window.operation.findData("sample"))
        self.window.start_batch.setValue(2)
        self.window.output.setText(str(output))
        self.window.glossary.setText(str(glossary))
        self.window.save_glossary.setText(str(saved_glossary))
        self.window.encoding.setText("utf-8")
        self.window.edit.setChecked(True)
        self.window._set_style("В примере нужен пробел.")
        self.window.models[2][0].setEditText("test-editor")
        self.window.budget.setValue(1)
        self.window.total_budget_mode.setCurrentIndex(1)
        self.window.total_budget.setValue(2)
        original_profile = self.window._profile_text()
        self.window.run.click()
        self._until(lambda: self.window.process is None)
        self.assertIn("Запросов к модели: 1. Ответов из кеша: 0.", self.window.log.toPlainText())
        self.assertFalse(output.exists())
        self.assertFalse(saved_glossary.exists())
        self.window._set_style("Другой стиль")
        self.window.budget.setValue(9)
        previous_version = json.loads(self.window.preferences.value("lastRun"))
        previous_version.pop("state")
        previous_version.pop("repair_review")
        self.window.preferences.setValue("lastRun", json.dumps(previous_version))
        self._close_window()
        self._open_window()

        self.assertEqual(self.window.source.text(), "")
        self.window.restore_run.click()
        self.assertIsNone(self.window.process)
        self.assertEqual(self.window.source.text(), str(self.source))
        self.assertEqual(self.window.operation.currentData(), "sample")
        self.assertEqual(self.window.start_batch.value(), 2)
        self.assertEqual(self.window.output.text(), str(output))
        self.assertEqual(self.window.glossary.text(), str(glossary))
        self.assertEqual(self.window.save_glossary.text(), str(saved_glossary))
        self.assertEqual(self.window.encoding.text(), "utf-8")
        self.assertFalse(self.window.auto_glossary.isChecked())
        self.assertTrue(self.window.edit.isChecked())
        self.assertEqual(self.window._profile_text(), original_profile)
        self.assertEqual(self.window.budget.value(), 1)
        self.assertEqual(self.window.total_budget_mode.currentIndex(), 0)
        self.assertIn("1 / 2", self.window.budget_status.text())
        self.assertFalse(output.exists())

        output.write_text("Занятый путь", encoding="utf-8")
        with patch("ai_translator.desktop._confirm_replace", return_value=False) as confirm:
            self.window.run.click()
        self.assertIsNone(self.window.process)
        self.assertIn(str(output), confirm.call_args.args[2])
        self.assertEqual(output.read_text(encoding="utf-8"), "Занятый путь")
        output.unlink()
        self.window.run.click()
        self._until(lambda: self.window.process is None)
        self.assertIn("Запросов к модели: 1. Ответов из кеша: 1.", self.window.log.toPlainText())
        self.assertIn("2 / 2", self.window.budget_status.text())
        self.assertEqual(self.window.last_output, output)
        self.assertEqual(self.window.preview.original.toPlainText(), "他看见窗外的雨。")
        self.assertEqual(load_glossary(saved_glossary), load_glossary(glossary))

        self.window.forget_run.click()
        self.assertFalse(self.window.restore_run.isEnabled())
        self.assertTrue(default_state_path(self.source).is_file())
        self._close_window()
        self._open_window()
        self.assertFalse(self.window.restore_run.isEnabled())

    def test_unreadable_last_run_preserves_current_form(self):
        self.window.preferences.setValue("lastRun", "{damaged")
        self.window._update_last_run()
        self.window.restore_run.click()
        self.assertIsNone(self.window.process)
        self.assertIn("Не удалось восстановить", self.window.status.text())
        self.assertEqual(self.window.source.text(), str(self.source))
        self.assertEqual(self.window.operation.currentData(), "translate")

    def test_desktop_can_run_optional_review_and_restore_draft(self):
        self.assertFalse(self.window.review_edits.isEnabled())
        self.window.edit.setChecked(True)
        self.window.review_edits.setChecked(True)
        self.window.run.click()
        self._until(lambda: self.window.process is None)
        self.assertIn("Сверка 1/1", self.window.log.toPlainText())
        self.assertIn("Запросов к модели: 3", self.window.log.toPlainText())
        self.assertEqual(self.window.last_output.read_text(encoding="utf-8"), "Она тихонько отворила дверь.\n")

    def test_desktop_compares_sample_versions_and_keeps_published_result(self):
        self.window.operation.setCurrentIndex(self.window.operation.findData("sample"))
        self.window.edit.setChecked(True)
        self.window.review_edits.setChecked(True)
        self.window.run.click()
        self._until(lambda: self.window.process is None)
        preview = self.window.preview
        self.assertTrue(preview.version_row.isVisible())
        self.assertEqual(preview.review_choice.text(), "Сверка выбрала черновик.")
        source = preview.original.toPlainText()
        final = preview.translation.toPlainText()
        output = self.window.last_output
        self.assertEqual(output.read_text(encoding="utf-8"), f"{source}\n{final}\n")

        preview.version.setCurrentIndex(preview.version.findData("edited"))
        self.assertEqual(preview.translation.toPlainText(), "Она бесшумно отворила дверь.")
        self.assertEqual(preview.original.toPlainText(), source)
        preview.version.setCurrentIndex(preview.version.findData("draft"))
        self.assertEqual(preview.translation.toPlainText(), final)
        self.assertEqual(output.read_text(encoding="utf-8"), f"{source}\n{final}\n")
        self.assertIn("Запросов к модели: 3.", self.window.log.toPlainText())

        self.window.source.setText(str(self.directory / "Другая книга.txt"))
        self.assertFalse(preview.version_row.isVisible())
        self.assertFalse(preview.review_choice.isVisible())
        self.assertEqual(preview.translation.toPlainText(), "")

    def test_desktop_can_estimate_and_restore_repaired_sample(self):
        self.window.operation.setCurrentIndex(self.window.operation.findData("sample"))
        self.assertFalse(self.window.repair_review.isEnabled())
        self.window.edit.setChecked(True)
        self.window.review_edits.setChecked(True)
        self.assertTrue(self.window.repair_review.isEnabled())
        self.window.repair_review.setChecked(True)
        self.assertIn("каждый пакет", self.window.review_edits.toolTip())
        self.window.inspect.click()
        self._until(lambda: self.window.process is None)
        self.assertIn("сверка с исправлениями: 1", self.window.log.toPlainText())

        self.window.run.click()
        self._until(lambda: self.window.process is None)
        preview = self.window.preview
        self.assertEqual(preview.review_choice.text(), "Сверка исправила текст для итоговой версии.")
        self.assertEqual(preview.translation.toPlainText(), "Она осторожно приоткрыла дверь.")
        self.assertIn("Она осторожно приоткрыла дверь.", self.window.last_output.read_text(encoding="utf-8"))
        preview.version.setCurrentIndex(preview.version.findData("edited"))
        self.assertEqual(preview.translation.toPlainText(), "Она бесшумно отворила дверь.")
        self._close_window()
        self._open_window()
        self.window.restore_run.click()
        self.assertTrue(self.window.edit.isChecked())
        self.assertTrue(self.window.review_edits.isChecked())
        self.assertTrue(self.window.repair_review.isChecked())
        self.assertTrue(self.window.repair_review.isEnabled())

    def test_cancel_does_not_publish_an_incomplete_book(self):
        self.source.write_text("她站在门外等待。\n", encoding="utf-8")
        self.window.run.click()
        pid_file = self.directory / ".provider-pid"
        self._until(lambda: pid_file.is_file() and bool(pid_file.read_text()))
        pid = int(pid_file.read_text())
        self.window.stop.click()
        self._until(lambda: self.window.process is None)
        self.assertIn("Остановлено", self.window.status.text())
        self.assertFalse(self.source.with_name(self.source.stem + ".ru.txt").exists())
        self._until(lambda: not _process_is_live(pid))

        self.source.write_text("第一章 门外\n她推开门。\n第二章 台阶\n她下楼。\n第三章 等待\n她站住。\n",
                               encoding="utf-8")
        pid_file.unlink()
        window = self.window
        window.operation.setCurrentIndex(window.operation.findData("sample"))
        window.load_chapters.click()
        self._until(lambda: window.process is None)
        window._filter_chapters(selected=(1, 3))
        window._update_form()
        window.run.click()
        self._until(lambda: pid_file.is_file() and bool(pid_file.read_text()))
        pid = int(pid_file.read_text())
        window.stop.click()
        self._until(lambda: window.process is None)
        self._until(lambda: not _process_is_live(pid))
        self.assertFalse(default_output(self.source, "sample", chapter=(1, 3)).exists())
        with open_state(default_state_path(self.source)) as state:
            self.assertEqual(read_request_budget(state), (3, None))
        window.inspect.click()
        self._until(lambda: window.process is None)
        self.assertIn("ответов из кеша: 1", window.plan_summary.toPlainText())

    @skipIf(os.name != "posix", "Тест группы процессов POSIX")
    def test_killing_desktop_worker_stops_provider_in_its_private_group(self):
        stub = self.directory / "codex"
        stub.write_text(f"#!{sys.executable}\n" + """
import os
import subprocess
import sys
import time
from pathlib import Path

if sys.argv[-2:] == ["login", "status"]:
    print("Logged in using ChatGPT")
    sys.exit(0)
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
Path(__file__).with_name(".provider-pids").write_text(f"{os.getpid()} {child.pid}")
time.sleep(30)
""", encoding="utf-8")
        self.source.write_text("她站在门外等待。\n", encoding="utf-8")
        self.window.run.click()
        marker = self.directory / ".provider-pids"
        self._until(lambda: marker.is_file() and bool(marker.read_text()))
        pid, child = map(int, marker.read_text().split())
        worker = self.window.process
        try:
            self.assertNotEqual(os.getpgid(pid), worker.processId())
            self.assertEqual(os.getpgid(pid), os.getpgid(child))
            worker.kill()
            self._until(lambda: self.window.process is None)
            self._until(lambda: not (_process_is_live(pid) or _process_is_live(child)))
            self.assertFalse(default_output(self.source, "translate").exists())
        finally:
            if self.window.process is not None:
                self.window.process.kill()
                self._until(lambda: self.window.process is None)
            for process in (pid, child):
                if _process_is_live(process):
                    os.kill(process, 9)

    @skipIf(os.name != "posix", "Тест группы процессов POSIX")
    def test_cancel_kills_remaining_process_after_worker_has_exited(self):
        program = """
import subprocess
import sys
import time

child = '''
import os
import signal
import sys
import time
from pathlib import Path

signal.signal(signal.SIGINT, signal.SIG_IGN)
Path(sys.argv[1]).write_text(str(os.getpid()))
time.sleep(30)
'''
subprocess.Popen([sys.executable, "-c", child, sys.argv[1]],
                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    time.sleep(30)
except KeyboardInterrupt:
    raise SystemExit(130)
"""
        marker = self.directory / "stubborn.pid"
        with patch.object(self.window, "_arguments", return_value=["-c", program, str(marker)]):
            self.window.run.click()
        self._until(lambda: marker.is_file() and bool(marker.read_text()))
        pid = int(marker.read_text())
        try:
            self.assertTrue(_process_is_live(pid))
            self.window.stop.click()
            self._until(lambda: self.window.process is None)
            self.assertIn("Остановлено", self.window.status.text())
            self._until(lambda: not _process_is_live(pid))
        finally:
            if _process_is_live(pid):
                os.kill(pid, 9)

    def test_toml_profiles_round_trip_between_desktop_and_cli(self):
        source = self.directory / "вход.toml"
        source.write_text(
            'provider = "claude-code"\nbatch_chars = 1600\ntimeout = 4100\n'
            'fandom = "Фэндом \\"Луна\\""\nstyle = """Живая\u00a0речь\nКраткие реплики"""\n'
            '[translate]\nmodel = "opus"\nreasoning = "high"\n', encoding="utf-8",
        )
        with patch.object(QFileDialog, "getOpenFileName", return_value=(str(source), "")):
            self.window.load_profile.click()
        self.assertEqual(self.window.provider.currentData(), "claude-code")
        destination = self.directory / "профиль"
        with patch.object(QFileDialog, "getSaveFileName", return_value=(str(destination), "")):
            self.window.save_profile.click()
        exported = destination.with_suffix(".toml")
        self.assertEqual(load_config(source, None), load_config(exported, None))
        self.window.start("inspect")
        self._until(lambda: self.window.process is None)
        self.assertIn("translate=opus/high", self.window.log.toPlainText())

    def test_toml_profile_round_trip_with_long_fandom_and_timeout(self):
        fandom = "夜🌙" * 12000
        source = self.directory / "длинный профиль.toml"
        large_identifier = "a" * 40000
        source.write_text(
            f'fandom = "{fandom}"\ntimeout = 2147483648\nstyle = "A\\u007fБ"\n'
            f'[translate]\nmodel = "{large_identifier}"\nreasoning = "{large_identifier}"\n',
            encoding="utf-8",
        )
        expected = load_config(source, None)
        self.window._load_profile(source)
        self.assertEqual(parse_config(self.window._profile_text()), expected)
        self._close_window()
        self._open_window()
        self.assertEqual(parse_config(self.window._profile_text()), expected)

    def test_model_choices_remember_custom_values_per_provider_and_support_auto(self):
        model, effort = self.window.models[1]
        self.assertGreaterEqual(model.findText("gpt-6.2-astra"), 0)
        model.setEditText("custom-model-2026")
        effort.setCurrentIndex(effort.findText("Авто"))
        self.assertEqual(parse_config(self.window._profile_text()).models["translate"], Model("custom-model-2026"))
        self.window.provider.setCurrentIndex(self.window.provider.findData("claude-code"))
        self.assertGreaterEqual(model.findText("opus"), 0)
        self.assertEqual(model.findText("gpt-6.2-astra"), -1)
        self.assertEqual(effort.findText("ultra"), -1)
        self.assertGreaterEqual(effort.findText("ultracode"), 0)
        model.setCurrentIndex(model.findText("opus"))
        effort.setCurrentIndex(effort.findText("high"))
        self.assertEqual(parse_config(self.window._profile_text()).models["translate"], Model("opus", "high"))
        self.window.provider.setCurrentIndex(self.window.provider.findData("openai-api"))
        self.assertIn("OPENAI_API_KEY", self.window.provider_hint.text())
        self.assertGreaterEqual(effort.findText("none"), 0)
        self.window.provider.setCurrentIndex(self.window.provider.findData("codex"))
        self.assertEqual(parse_config(self.window._profile_text()).models["translate"], Model("custom-model-2026"))
        self.window.reset_models.click()
        self.assertEqual(parse_config(self.window._profile_text()), load_config(None, "codex"))
        self.window.provider.setCurrentIndex(self.window.provider.findData("claude-code"))
        self.assertEqual(parse_config(self.window._profile_text()).models["translate"], Model("opus", "high"))

    def test_desktop_translates_through_antigravity_and_reuses_its_cache(self):
        stub = self.directory / ("agy_stub.py" if os.name == "nt" else "agy")
        stub.write_text(f"#!{sys.executable}\n" + """
import json
import sys

line = sys.stdin.buffer.readline()
if not line:
    sys.exit(0)
data = json.loads(json.loads(line)["message"]["content"])
assert sys.argv[sys.argv.index("--effort") + 1] == "high"
result = {"translations": [{"id": block["id"], "text": "Она тихо открыла дверь."} for block in data["blocks"]]}
print(json.dumps({"event": "result", "result": {"status": "SUCCESS", "structured_output": result}}))
""", encoding="utf-8")
        stub.chmod(0o700)
        if os.name == "nt":
            (self.directory / "agy.cmd").write_text(f'@"{sys.executable}" "{stub}" %*\n', encoding="utf-8")

        self.window.provider.setCurrentIndex(self.window.provider.findData("antigravity"))
        self.assertIn("аккаунт Google", self.window.provider_hint.text())
        self.window.check_login.click()
        self._until(lambda: self.window.process is None)
        self.assertIn("не подтвердил вход", self.window.status.text())
        self.assertFalse(default_state_path(self.source).exists())
        model, effort = self.window.models[1]
        self.assertEqual(model.currentText(), "gemini-3.8-flash")
        effort.setCurrentIndex(effort.findText("high"))
        self.window.operation.setCurrentIndex(self.window.operation.findData("sample"))
        self.window.run.click()
        self._until(lambda: self.window.process is None)
        self.assertEqual(self.window.preview.translation.toPlainText(), "Она тихо открыла дверь.")
        self.assertIn("Запросов к модели: 1.", self.window.log.toPlainText())
        first_output = self.window.last_output.read_bytes()

        stub.unlink()
        self.window.start("inspect")
        self._until(lambda: self.window.process is None)
        self.assertIn("translate=gemini-3.8-flash/high", self.window.log.toPlainText())
        self.assertIn("Кеш покрывает весь выбранный запуск", self.window.log.toPlainText())
        output = self.directory / "antigravity-replay.txt"
        self.window.output.setText(str(output))
        self.window.budget.setValue(0)
        self.window.run.click()
        self._until(lambda: self.window.process is None)
        self.assertEqual(output.read_bytes(), first_output)
        self.assertIn("Запросов к модели: 0. Ответов из кеша: 1.", self.window.log.toPlainText())

    def test_bad_model_is_reported_before_output_confirmation_or_worker_launch(self):
        default_output(self.source, "translate").write_text("Предыдущий результат", encoding="utf-8")
        self.window.models[1][0].setEditText("model with spaces")
        with patch("ai_translator.desktop._confirm_replace") as confirm:
            self.window.run.click()
        confirm.assert_not_called()
        self.assertIsNone(self.window.process)
        self.assertEqual(self.window.options.tabText(self.window.options.currentIndex()), "Модели и стиль")
        self.assertIn("[translate] model", self.window.status.text())
        self.assertFalse(default_state_path(self.source).exists())

    def test_bad_glossary_extension_is_reported_before_overwrite_confirmation(self):
        glossary = self.directory / "существующий.txt"
        glossary.write_text("Существующий файл", encoding="utf-8")
        self.window.save_glossary.setText(str(glossary))
        with patch("ai_translator.desktop._confirm_replace") as confirm:
            self.window.run.click()
        confirm.assert_not_called()
        self.assertIsNone(self.window.process)
        self.assertIn(".json", self.window.status.text())
        self.assertEqual(glossary.read_text(encoding="utf-8"), "Существующий файл")

    def test_invalid_profile_does_not_change_current_settings(self):
        original = self.window._profile_text()
        source = self.directory / "bad.toml"
        source.write_text('provider = "claude-code"\nbatch_chars = 1\n', encoding="utf-8")
        with patch.object(QFileDialog, "getOpenFileName", return_value=(str(source), "")):
            self.window.load_profile.click()
        self.assertEqual(self.window._profile_text(), original)
        self.assertIn("Не удалось загрузить", self.window.status.text())

    def test_profile_export_confirms_the_actual_filename_after_adding_extension(self):
        destination = self.directory / "профиль"
        exported = destination.with_suffix(".toml")
        exported.write_text("existing", encoding="utf-8")
        with patch.object(QFileDialog, "getSaveFileName", return_value=(str(destination), "")), patch(
            "ai_translator.desktop._confirm_replace", return_value=False,
        ) as question:
            self.window.save_profile.click()
        self.assertEqual(question.call_args.args[2], str(exported))
        self.assertEqual(exported.read_text(encoding="utf-8"), "existing")

    def test_confirmation_uses_literal_file_names(self):
        filename = "Книга <b>том 1</b>.txt"

        def dismiss(dialog):
            self.assertEqual(dialog.textFormat(), Qt.TextFormat.PlainText)
            self.assertEqual(dialog.text(), filename)
            return QMessageBox.StandardButton.Cancel.value

        with patch.object(QMessageBox, "exec", dismiss):
            self.assertFalse(_confirm_replace(self.window, "Проверка", filename))

    def test_glossary_editor_validates_and_saves_approved_forms(self):
        original = self.directory / "canon.json"
        original.write_text('{"赫敏":{"ru":"Гермиона","forms":["Гермиону","форма; со знаком"],"aliases":["妙丽"]}}',
                            encoding="utf-8")
        dialog = GlossaryDialog(original, self.window)
        dialog._add_row("哈利", "Гарри", aliases="哈里;哈瑞")
        dialog.search.setText("гермиона")
        self.assertFalse(dialog.table.isRowHidden(0))
        self.assertTrue(dialog.table.isRowHidden(1))
        dialog.search.setText("妙丽")
        self.assertFalse(dialog.table.isRowHidden(0))
        self.assertTrue(dialog.table.isRowHidden(1))
        dialog.search.clear()
        dialog._add_row("哈利", "Другой")
        dialog.save.click()
        self.assertIn("несколько раз", dialog.message.text())
        dialog.delete_term.click()
        target = self.directory / "новый"
        with patch.object(QFileDialog, "getSaveFileName", return_value=(str(target), "")):
            dialog.save.click()
        exported = target.with_suffix(".json")
        self.assertEqual(dialog.saved_path, exported)
        self.assertEqual(load_glossary(exported), {
            **load_glossary(original),
            "哈利": {"ru": "Гарри", "forms": [], "aliases": ["哈里", "哈瑞"], "strict": False},
        })

    def test_export_dialogs_cannot_replace_the_active_cache(self):
        cache = self.directory / "work.json"
        with open_state(cache) as connection:
            connection.execute("CREATE TABLE test_data (value);")
            connection.execute("INSERT INTO test_data VALUES ('saved response');")
            connection.commit()
        before = cache.read_bytes()
        self.window.cache.setText(str(cache))

        def save_to_cache(dialog):
            dialog._add_row("雨", "дождь")
            with patch.object(QFileDialog, "getSaveFileName", return_value=(str(cache), "")), patch(
                "ai_translator.desktop._confirm_replace", return_value=True
            ) as confirm:
                dialog.save.click()
            self.assertIsNone(dialog.saved_path)
            self.assertIn("кеш", dialog.message.text().lower())
            confirm.assert_not_called()
            return dialog.result()

        with patch.object(GlossaryDialog, "exec", save_to_cache):
            self.window._edit_glossary()
        self.assertEqual(cache.read_bytes(), before)
        self.assertEqual(self.window.glossary.text(), "")

        profile_path = self.directory / "work.toml"
        profile_path.write_bytes(before)
        self.window.cache.setText(str(profile_path))
        with patch.object(QFileDialog, "getSaveFileName", return_value=(str(profile_path), "")), patch(
            "ai_translator.desktop._confirm_replace", return_value=True
        ) as confirm:
            self.window._export_profile()
        self.assertIn("кеш", self.window.status.text().lower())
        confirm.assert_not_called()
        self.assertEqual(profile_path.read_bytes(), before)
