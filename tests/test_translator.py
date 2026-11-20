import base64
import io
import json
import os
import subprocess
import struct
import sys
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from http.client import IncompleteRead
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError, URLError
from unittest import TestCase, main
from unittest.mock import patch

from lxml import etree

from ai_translator import publication
from ai_translator.cli import main as cli
from ai_translator.config import Model, load_config
from ai_translator.formats import ANCHOR, Book, _xml, _xml_units, read_book, read_chapters, read_txt_document, txt_number_gaps
from ai_translator.numerals import equivalent_numeric
from ai_translator.paths import CACHE_DIRECTORY, PUBLICATION_DIRECTORY, default_output, path_too_long
from ai_translator.pipeline import (
    InvalidAnswer, Requests, _blocks, _entries, _split, _translations, batches, default_state_path, open_state,
    parse_glossary,
)
from ai_translator.publication import recover_outputs, write_outputs
from ai_translator.provider import AuthenticationError, Codex, OpenAIAPI, ProviderError, ProviderRefusal, Usage


def write_epub(path: Path) -> None:
    entries = {
        "mimetype": b"application/epub+zip",
        "META-INF/container.xml": b"""<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
            <rootfiles><rootfile full-path="OPS/book.opf" media-type="application/oebps-package+xml"/>
            </rootfiles></container>""",
        "OPS/book.opf": """<package xmlns="http://www.idpf.org/2007/opf" xml:lang="zh-CN"
            version="3.0" unique-identifier="book-id">
            <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
              <dc:identifier id="book-id">urn:uuid:26018a3c-c97c-4b05-aae5-94f6035bfb20</dc:identifier>
              <dc:title>雨夜</dc:title><dc:language>zh-CN</dc:language>
              <meta property="dcterms:modified">2026-11-19T00:00:00Z</meta>
            </metadata>
            <manifest>
              <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
              <item id="second" href="chapter%202.xhtml" media-type="application/xhtml+xml"/>
              <item id="first" href="chapter1.xhtml" media-type="application/xhtml+xml"/>
              <item id="css" href="styles.css" media-type="text/css"/>
            </manifest>
            <spine><itemref idref="first"/><itemref idref="second"/></spine>
            </package>""".encode(),
        "OPS/chapter1.xhtml": """<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="zh">
            <head><title>Chapter 1</title><link href="styles.css" rel="stylesheet"/></head>
            <body><p id="opening">哈利<i>轻轻地</i>推开了门。<a href="chapter%202.xhtml#turn">继续</a></p>
              <p>Already in English.</p></body></html>""".encode(),
        "OPS/chapter 2.xhtml": """<html xmlns="http://www.w3.org/1999/xhtml">
            <head><title>Chapter 2</title></head>
            <body><h1 id="turn">雨夜</h1><p>赫敏放下了书。</p></body></html>""".encode(),
        "OPS/nav.xhtml": """<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
            <head><title>Contents</title></head>
            <body><nav epub:type="toc"><ol><li><a href="chapter1.xhtml#opening">Opening</a></li>
            <li><a href="chapter%202.xhtml#turn">雨夜</a></li></ol></nav></body></html>""".encode(),
        "OPS/styles.css": b"p { text-indent: 1em; }",
    }
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)


def write_fb2(path: Path) -> None:
    path.write_text("""<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0"
        xmlns:l="http://www.w3.org/1999/xlink">
      <description><title-info><book-title>雨夜</book-title><lang>zh</lang></title-info></description>
      <body><section id="chapter"><p>哈利<emphasis>轻轻地</emphasis>推开了门。
        <a l:href="#n1">注释</a></p></section></body>
      <body name="notes"><section id="n1"><p>旧门。</p></section></body>
      <binary id="cover" content-type="image/png">YWJj</binary>
    </FictionBook>""", encoding="utf-8")


class FakeCodex:
    calls = []
    stop_after = None

    def __init__(self, timeout: int):
        self.usage = Usage()

    def check(self) -> None:
        pass

    def complete(self, model: Model, instructions: str, data: str, schema: dict) -> dict:
        payload = json.loads(data)
        if len(self.calls) == self.stop_after:
            raise ProviderError("тестовое прерывание")
        self.calls.append((model, payload))
        if "choices" in schema["properties"]:
            return {"choices": [{"id": identifier, "version": "draft" if identifier == 1 else "edited"}
                                for identifier in payload["changed"]]}
        if "entries" in schema["properties"]:
            result = [{"zh": "哈利", "ru": "Гарри (автоматически)"}]
            result.append({"zh": "赫敏", "ru": "Гермиона"})
            result.append({"zh": "不存在", "ru": "Вымышленный термин"})
            return {"entries": result}

        translations = []
        for block in payload["blocks"]:
            name = payload["glossary"].get("哈利", "Гермиона")
            marks = ANCHOR.findall(block["text"])
            text = name + " вошёл." if "draft" not in payload else name + " вошёл тихо."
            translations.append({"id": block["id"], "text": text + " ".join(
                marker + " Текст " for marker in marks
            )})
        return {"translations": list(reversed(translations))}


class TranslatorTests(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.source = self.directory / "Book.txt"
        FakeCodex.calls = []
        FakeCodex.stop_after = None
        fake = patch("ai_translator.pipeline.Codex", FakeCodex)
        fake.start()
        self.addCleanup(fake.stop)

    def invoke(self, *args, expected=0):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = cli(list(map(str, args)))
        self.assertEqual(status, expected, stderr.getvalue() or stdout.getvalue())
        return stdout.getvalue(), stderr.getvalue()

    def test_txt_manual_map_decodes_crlf_and_non_han_boundaries_and_checks_revision(self):
        self.source.write_bytes("序言\r\n\r\nPart One\r\n春雨来了。\r\n\r\nPart Two\r\n她回家了。\r\n".encode("gb18030"))
        self.assertEqual(txt_number_gaps({1: "第一章 早晨", 3: "第三章 下午", 5: "第二卷 晚上"}), [(3, 1, 3)])
        document = read_txt_document(self.source, "gb18030")
        chapter_map = self.directory / "book.chapters.json"
        chapter_map.write_text(document.map_content({3: "Одинаковое название", 5: "Одинаковое название"}),
                               encoding="utf-8")
        options = ("--encoding", "gb18030", "--txt-map", chapter_map)
        raw, _ = self.invoke("inspect", "--list-chapters", "--json-events", *options, self.source)
        entries = json.loads(raw)["chapters"]
        self.assertEqual([(e["title"], e["units"]) for e in entries],
                         [("Начало книги", 1), ("Одинаковое название", 1), ("Одинаковое название", 1)])
        self.assertEqual(read_chapters(self.source, "gb18030", chapter_map),
                         read_book(self.source, "gb18030", with_chapters=True, txt_map=chapter_map).chapters)
        # Boundary inspection does not access the cache, including a path that is a directory.
        raw, _ = self.invoke("inspect", "--for", "sample", "--sample-boundaries", "--state", self.directory,
                             "--chapter", entries[2]["reference"], "--json-events", *options, self.source)
        events = {event["event"]: event for event in map(json.loads, raw.splitlines())}
        self.assertEqual(events["sample_range"]["opening"], "她回家了。")
        self.assertEqual(events["sample_range"]["closing"], "她回家了。")
        self.assertNotIn("plan", events)
        chapter_map.write_text(document.map_content({3: "Оставшаяся глава"}), encoding="utf-8")
        _, error = self.invoke("inspect", "--for", "sample", "--chapter", entries[2]["reference"],
                               *options, self.source, expected=1)
        self.assertIn("Выбор главы устарел", error)
        self.source.write_bytes(self.source.read_bytes() + "她说话了。\r\n".encode("gb18030"))
        _, error = self.invoke("inspect", "--list-chapters", *options, self.source, expected=1)
        self.assertIn("Карта TXT устарела", error)
        self.assertEqual(FakeCodex.calls, [])

    def test_chapter_union_skips_gaps_deduplicates_fb2_children_and_resumes(self):
        source = self.directory / "union.fb2"
        source.write_text("""<FictionBook><body>
          <section><title><p>甲章</p></title><p>她开门。</p>
             <section><title><p>子章</p></title><p>春雨来了。</p></section>
          </section>
          <section><title><p>乙章</p></title><p>她走路。</p></section>
          <section><title><p>丙章</p></title><p>她回家。</p></section>
        </body></FictionBook>""", encoding="utf-8")
        raw, _ = self.invoke("inspect", "--list-chapters", "--json-events", source)
        refs = [entry["reference"] for entry in json.loads(raw)["chapters"]]
        choices = ("--chapter", refs[3], "--chapter", refs[1], "--chapter", refs[0], "--chapter", refs[1])
        output = default_output(source, "sample", chapter=(1, 2, 4))
        raw, _ = self.invoke("inspect", "--for", "sample", "--json-events", "--no-auto-glossary", *choices, source)
        events = {event["event"]: event for event in map(json.loads, raw.splitlines())}
        self.assertEqual(events["inspection"]["units"], 6)
        self.assertEqual(events["plan"]["translate"], 2)
        args = ("sample", "--no-auto-glossary", "--json-events", "--sample-pairs", *choices, source)
        self.invoke(*args, "--max-total-calls", "2", "--max-calls", "1", expected=3)
        self.assertFalse(output.exists())
        raw, _ = self.invoke(*args, "--max-calls", "1")
        events = list(map(json.loads, raw.splitlines()))
        self.assertEqual([e["original"] for e in events if e["event"] == "sample_pair"],
                         ["甲章", "她开门。", "子章", "春雨来了。", "丙章", "她回家。"])
        self.assertEqual(len(FakeCodex.calls), 2)
        first, second = [payload for _, payload in FakeCodex.calls]
        self.assertEqual((first["following"], second["preceding"], second["following"]), ("", "", ""))
        published = output.read_bytes()
        self.invoke(*args, "--force", "--max-calls", "0")
        self.assertEqual(len(FakeCodex.calls), 2)
        self.assertEqual(output.read_bytes(), published)

    def test_chapter_file_selects_sparse_ranges_and_reuses_legacy_sample_cache(self):
        self.source.write_text("第一章 山路\n她走路。\n第二章 石门\n她开门。\n第三章 春雨\n她回家。\n",
                               encoding="utf-8")
        raw, _ = self.invoke("inspect", "--list-chapters", "--json-events", self.source)
        catalog = json.loads(raw)
        selection = self.directory / "выбор глав.json"
        selection.write_text(json.dumps({"version": 1, "revision": catalog["revision"], "chapters": [3, 1, 3]}),
                             encoding="utf-8")
        raw, _ = self.invoke("inspect", "--for", "sample", "--sample-boundaries", "--json-events",
                             "--state", self.directory, "--chapter-file", selection, self.source)
        bounds = {event["event"]: event for event in map(json.loads, raw.splitlines())}
        self.assertEqual(bounds["inspection"]["chapters"], [1, 3])
        self.assertEqual(bounds["sample_range"]["opening"], "第一章 山路")
        self.assertEqual(bounds["sample_range"]["closing"], "她回家。")
        self.assertNotIn("plan", bounds)
        self.assertEqual(FakeCodex.calls, [])

        args = ("sample", "--no-auto-glossary", "--sample-pairs", "--json-events", self.source)
        refs = [entry["reference"] for entry in catalog["chapters"]]
        self.invoke(*args, "--chapter", refs[0], "--chapter", refs[2])
        output = default_output(self.source, "sample", chapter=(1, 3))
        published = output.read_bytes()
        raw, _ = self.invoke(*args, "--chapter-file", selection, "--max-calls", "0", "--force")
        events = list(map(json.loads, raw.splitlines()))
        self.assertEqual([event["original"] for event in events if event["event"] == "sample_pair"],
                         ["第一章 山路", "她走路。", "第三章 春雨", "她回家。"])
        self.assertEqual(next(event["calls"] for event in events if event["event"] == "requests"), 0)
        self.assertEqual(output.read_bytes(), published)
        self.assertEqual(len(FakeCodex.calls), 2)

        self.source.write_text("序言\n" + self.source.read_text(encoding="utf-8"), encoding="utf-8")
        raw, _ = self.invoke(*args, "--chapter-file", selection, "--force", expected=1)
        self.assertIn("Выбор главы устарел", json.loads(raw)["message"])
        self.assertEqual(output.read_bytes(), published)
        self.assertEqual(len(FakeCodex.calls), 2)

    def test_chapter_file_validates_schema_and_incompatible_options(self):
        self.source.write_text("第一章 山路\n她走路。\n", encoding="utf-8")
        raw, _ = self.invoke("inspect", "--list-chapters", "--json-events", self.source)
        revision = json.loads(raw)["revision"]
        selection = self.directory / "selection.json"
        base_args = ("inspect", "--for", "sample", "--chapter-file", selection, self.source)
        valid = {"version": 1, "revision": revision, "chapters": [1]}
        cases = [
            ("{", "Не удалось прочитать JSON"),
            ("[]", "Версия файла"),
            (json.dumps({**valid, "version": True}), "Версия файла"),
            (json.dumps({**valid, "version": 2}), "Версия файла"),
            (json.dumps({**valid, "revision": revision[:16]}), "полная ревизия"),
            (json.dumps({**valid, "revision": 1}), "полная ревизия"),
            (json.dumps({**valid, "chapters": []}), "положительных целых"),
            (json.dumps({**valid, "chapters": [True]}), "положительных целых"),
            (json.dumps({**valid, "chapters": ["1"]}), "положительных целых"),
            (json.dumps({**valid, "chapters": [1.0]}), "положительных целых"),
            (json.dumps({**valid, "chapters": [0]}), "положительных целых"),
            (json.dumps({**valid, "chapters": [2]}), "вне диапазона"),
        ]
        for data, message in cases:
            with self.subTest(data=data):
                selection.write_text(data, encoding="utf-8")
                _, error = self.invoke(*base_args, expected=1)
                self.assertIn(message, error)
        selection.write_text(json.dumps(valid), encoding="utf-8")
        for extra in (("--batches", 1), ("--start-batch", 1), ("--list-chapters",),
                      ("--chapter-revision", revision), ("--for", "translate")):
            with self.subTest(extra=extra):
                _, error = self.invoke(*base_args, *extra, expected=1)
                self.assertIn("--chapter", error)
        with self.assertRaises(SystemExit) as error, redirect_stderr(io.StringIO()) as stderr:
            cli(list(map(str, (*base_args, "--chapter", 1))))
        self.assertEqual(error.exception.code, 2)
        self.assertIn("--chapter-file", stderr.getvalue())
        self.assertFalse(default_state_path(self.source).exists())
        self.assertEqual(FakeCodex.calls, [])

    def test_proofreading_publishes_addresses_and_replays_corrections_with_no_calls(self):
        source = self.directory / "review.fb2"
        source.write_text("""<FictionBook>
          <description><title-info><book-title>雨夜</book-title></title-info></description>
          <body><section><p>她开门。</p></section></body>
          <body name="notes"><section><p>这是脚注。</p></section></body>
        </FictionBook>""", encoding="utf-8")
        output = default_output(source, "translate")
        complete = FakeCodex.complete
        calls = []

        def reject_once(backend, model, instructions, data, schema):
            calls.append(instructions)
            return {"translations": []} if len(calls) == 1 else complete(backend, model, instructions, data, schema)

        args = ("translate", "--proofreading", "--json-events", "--edit", "--review-edits",
                "--no-auto-glossary", source)
        with patch.object(FakeCodex, "complete", reject_once):
            raw, _ = self.invoke(*args, "--max-calls", "1", expected=3)
            self.assertNotIn('"event": "proofreading', raw)
            self.assertFalse(output.exists())
            raw, _ = self.invoke(*args)
        events = list(map(json.loads, raw.splitlines()))
        names = [row["event"] for row in events]
        self.assertLess(names.index("output"), names.index("proofreading"))
        report = [event for event in events if event["event"].startswith("proofreading")]
        self.assertEqual((report[0]["parts"], report[0]["checked"], report[0]["review_mode"]), (3, 3, "changes"))
        self.assertEqual([(row["section"], row["block"], row["part"], row["status"]) for row in report[1:]],
                         [(1, 1, 1, "rejected_then_accepted"), (1, 1, 1, "draft"),
                          (2, 1, 1, "edited"), (3, 1, 1, "edited")])
        published = output.read_bytes()
        replay, _ = self.invoke(*args, "--force", "--max-calls", "0")
        replay = list(map(json.loads, replay.splitlines()))
        self.assertEqual([event for event in replay if event["event"].startswith("proofreading")], report)
        self.assertEqual(next(e for e in replay if e["event"] == "requests")["calls"], 0)
        self.assertEqual(output.read_bytes(), published)
        without_report = self.directory / "without-report.fb2"
        self.invoke("translate", "--edit", "--review-edits", "--no-auto-glossary", "--max-calls", "0",
                    "-o", without_report, source)
        self.assertEqual(without_report.read_bytes(), published)
        with patch("ai_translator.cli.write_outputs", side_effect=OSError("test publication failed")):
            failed, _ = self.invoke(*args, "--force", "--max-calls", "0", expected=1)
        self.assertNotIn('"event": "proofreading', failed)
        self.assertEqual(output.read_bytes(), published)
        raw, _ = self.invoke("translate", "--proofreading", "--json-events", "--no-auto-glossary",
                             "--max-calls", "0", "--force", source)
        summary = next(e for e in map(json.loads, raw.splitlines()) if e["event"] == "proofreading")
        self.assertEqual((summary["parts"], summary["checked"], summary["review_mode"]), (3, 0, "off"))

    def test_entry_points_emit_utf8_even_when_the_pipe_encoding_is_cp1252(self):
        self.source.write_text("雨夜。\n", encoding="utf-8")
        env = {**os.environ, "PYTHONIOENCODING": "cp1252"}
        for module in ("cli", "desktop"):
            with self.subTest(module=module):
                run = subprocess.run([sys.executable, "-m", f"ai_translator.{module}", "--help"],
                                     env=env, capture_output=True, encoding="utf-8", timeout=12, check=False)
                self.assertEqual(run.returncode, 0, run.stderr)
                self.assertIn("перевод", run.stdout.lower())
        run = subprocess.run([sys.executable, "-m", "ai_translator.cli", "inspect", "--json-events",
                              "--for", "sample", "--no-auto-glossary", str(self.source)],
                             env=env, capture_output=True, encoding="utf-8", timeout=12, check=False)
        self.assertEqual(run.returncode, 0, run.stderr)
        events = {event["event"]: event for event in map(json.loads, run.stdout.splitlines())}
        self.assertEqual(events["sample_range"]["opening"], "雨夜。")
        self.assertIn("Блоков с китайским текстом", events["inspection"]["message"])

    def test_glossary_gb18030_layout_and_edit(self):
        self.source.write_bytes("  哈利来了。\r\n\r\n赫敏问好。\r\n".encode("gb18030"))
        glossary = self.directory / "canon.json"
        glossary.write_text('{"哈利":"Гарри Поттер"}', encoding="utf-8")
        generated = self.directory / "generated.json"
        config = self.directory / "style.toml"
        config.write_text('[edit]\nmodel="selected-editor"\nreasoning="high"\n', encoding="utf-8")
        self.invoke("translate", self.source, "--glossary", glossary, "--save-glossary", generated,
                    "--config", config, "--edit")
        output = self.directory / "Book.ru.txt"
        self.assertIn("  Гарри Поттер вошёл тихо.\r\n\r\n", output.read_bytes().decode())
        self.assertEqual(json.loads(generated.read_text()), {"哈利": "Гарри Поттер", "赫敏": "Гермиона"})
        self.assertEqual(FakeCodex.calls[-1][0], Model("selected-editor", "high"))

    def test_resume_and_reuse_draft_when_enabling_edit(self):
        self.source.write_text(("哈利来了。\n" * 60) + ("赫敏问好。\n" * 60), encoding="utf-8")
        config = self.directory / "small.toml"
        config.write_text("batch_chars=400\n")
        args = ("translate", self.source, "--config", config, "--no-auto-glossary")
        count = len(list(batches(read_book(self.source), 400)))
        self.assertGreater(count, 1)
        FakeCodex.stop_after = 1
        self.invoke(*args, expected=1)
        output = self.directory / "Book.ru.txt"
        self.assertFalse(output.exists())
        FakeCodex.stop_after = None
        _, log = self.invoke(*args)
        self.assertIn("Ответов из кеша: 1", log)
        _, log = self.invoke(*args, "--edit", "--force")
        self.assertIn(f"Ответов из кеша: {count}", log)
        self.assertEqual(len(FakeCodex.calls), count * 2)
        with patch("ai_translator.pipeline.Codex", side_effect=AssertionError("Кеш должен работать офлайн")):
            self.invoke(*args, "--edit", "--force")

    def test_epub_preserves_spine_and_assets(self):
        source = self.directory / "book.epub"
        write_epub(source)
        sections = read_book(source).sections
        self.assertIn("哈利", sections[1][0].source)
        self.assertIn("赫敏", sections[2][1].source)
        self.invoke("translate", source, "--no-auto-glossary")
        with zipfile.ZipFile(self.directory / "book.ru.epub") as archive:
            self.assertIsNone(archive.testzip())
            self.assertEqual(archive.infolist()[0].filename, "mimetype")
            self.assertEqual(archive.infolist()[0].compress_type, zipfile.ZIP_STORED)
            self.assertEqual(archive.read("OPS/styles.css"), b"p { text-indent: 1em; }")
            chapter = etree.fromstring(archive.read("OPS/chapter1.xhtml"))
            self.assertEqual(chapter.get("{http://www.w3.org/XML/1998/namespace}lang"), "ru")
            self.assertEqual(chapter.find(".//{*}p").get("id"), "opening")
            self.assertEqual(chapter.find(".//{*}a").get("href"), "chapter%202.xhtml#turn")
            self.assertIn("Текст", chapter.find(".//{*}i").text)
            self.assertIn("Already in English.", archive.read("OPS/chapter1.xhtml").decode())
            nav = etree.fromstring(archive.read("OPS/nav.xhtml"))
            self.assertEqual(nav.findall(".//{*}a")[1].get("href"), "chapter%202.xhtml#turn")
            translated_item = nav.findall(".//{*}li")[1]
            self.assertFalse((translated_item.text or "").strip())
            self.assertFalse((translated_item[0].tail or "").strip())
            self.assertIn("Гермиона", translated_item[0].text)

    def test_epub_chapter_sample_follows_toc_fragments_across_spine_documents(self):
        original = self.directory / "base.epub"
        source = self.directory / "book.epub"
        write_epub(original)
        xhtml = 'xmlns="http://www.w3.org/1999/xhtml"'
        with zipfile.ZipFile(original) as src, zipfile.ZipFile(source, "w") as dest:
            for info in src.infolist():
                data = src.read(info)
                if info.filename == "OPS/chapter1.xhtml":
                    data = (f'<html {xhtml}><body><h1 id="first">第一章</h1><p>哈利来了。</p>'
                            '<h1 id="second">第二章</h1><p>赫敏来了。</p></body></html>').encode()
                elif info.filename == "OPS/chapter 2.xhtml":
                    data = (f'<html {xhtml}><head><title>文件说明</title></head>'
                            '<body><p>门关上了。</p><h1 id="third">第三章</h1><p>雪下了。</p></body></html>').encode()
                elif info.filename == "OPS/nav.xhtml":
                    data = (f'<html {xhtml} xmlns:epub="http://www.idpf.org/2007/ops"><body><nav epub:type="toc">'
                            '<ol><li><a href="chapter1.xhtml#first">山路</a></li>'
                            '<li><a href="chapter1.xhtml#second">石门</a></li>'
                            '<li><a href="chapter%202.xhtml#third">初雪</a></li></ol></nav></body></html>').encode()
                dest.writestr(info, data)

        listing, _ = self.invoke("inspect", source, "--list-chapters", "--json-events")
        entries = json.loads(listing)["chapters"]
        self.assertEqual([(entry["number"], entry["title"], entry["units"]) for entry in entries],
                         [(1, "山路", 2), (2, "石门", 3), (3, "初雪", 2)])
        self.assertEqual(FakeCodex.calls, [])
        self.assertFalse(default_state_path(source).exists())

        args = (source, "--chapter", entries[1]["reference"], "--no-auto-glossary", "--json-events")
        plan, _ = self.invoke("inspect", "--for", "sample", *args)
        events = {item["event"]: item for item in map(json.loads, plan.splitlines())}
        self.assertEqual(events["plan"]["translate"], 1)
        self.assertEqual(events["sample_range"]["opening"], "第二章")
        self.assertEqual(events["sample_range"]["closing"], "门关上了。")
        self.assertIn("石门", events["sample_range"]["message"])
        self.assertFalse(default_state_path(source).exists())

        output, _ = self.invoke("sample", *args, "--sample-pairs")
        pairs = [item["original"] for item in map(json.loads, output.splitlines()) if item["event"] == "sample_pair"]
        self.assertEqual(pairs, ["第二章", "赫敏来了。", "门关上了。"])
        self.assertEqual(len(FakeCodex.calls), 1)
        payload = FakeCodex.calls[0][1]
        self.assertEqual([block["text"] for block in payload["blocks"]], pairs)
        self.assertEqual(payload["preceding"], "")
        self.assertEqual(payload["following"], "")
        first = default_output(source, "sample", chapter=2).read_bytes()
        replay = self.directory / "replay.txt"
        with patch("ai_translator.pipeline.Codex", side_effect=AssertionError("Unexpected request")):
            self.invoke("sample", *args, "--max-calls", "0", "-o", replay)
        self.assertEqual(replay.read_bytes(), first)
        multi, _ = self.invoke("sample", source, "--chapter", entries[1]["reference"], "--chapter", entries[0]["reference"],
                               "--no-auto-glossary", "--json-events", "--sample-pairs")
        originals = [item["original"] for item in map(json.loads, multi.splitlines()) if item["event"] == "sample_pair"]
        self.assertEqual(originals, ["第一章", "哈利来了。", *pairs])
        self.assertNotIn("第三章", originals)
        self.assertTrue(default_output(source, "sample", chapter=(1, 2)).is_file())

    def test_epub_chapter_anchor_at_end_of_xhtml_owns_the_continuation(self):
        base = self.directory / "base.epub"
        write_epub(base)
        for nav in (True, False):
            with self.subTest(nav=nav):
                source = self.directory / f"continued-{nav}.epub"
                with zipfile.ZipFile(base) as src, zipfile.ZipFile(source, "w") as dest:
                    for info in src.infolist():
                        data = src.read(info)
                        if info.filename == "OPS/chapter1.xhtml":
                            data = ('<html><body><h1 id="a">甲章</h1><p>雨停了。</p>'
                                    '<h1 id="b">Chapter B</h1></body></html>').encode()
                        elif info.filename == "OPS/chapter 2.xhtml":
                            data = ('<html><body><p>乙的正文。</p>'
                                    '<h1 id="c">丙章</h1><p>雪下了。</p></body></html>').encode()
                        elif info.filename == "OPS/nav.xhtml":
                            data = ('<html xmlns:epub="http://www.idpf.org/2007/ops"><body><nav epub:type="toc">'
                                    '<ol><li><a href="chapter1.xhtml#a">A</a></li>'
                                    '<li><a href="chapter1.xhtml#b">B</a></li>'
                                    '<li><a href="chapter%202.xhtml#c">C</a></li></ol></nav></body></html>').encode()
                        elif info.filename == "OPS/book.opf" and not nav:
                            data = data.replace(b' properties="nav"', b"")
                            data = data.replace(b'</manifest>', b'<item id="ncx" href="toc.ncx" '
                                                b'media-type="application/x-dtbncx+xml"/></manifest>')
                            data = data.replace(b'<spine>', b'<spine toc="ncx">')
                        dest.writestr(info, data)
                    if not nav:
                        points = "".join(f'<navPoint><navLabel><text>{label}</text></navLabel>'
                                         f'<content src="{href}"/></navPoint>'
                                         for label, href in (("A", "chapter1.xhtml#a"), ("B", "chapter1.xhtml#b"),
                                                             ("C", "chapter%202.xhtml#c")))
                        dest.writestr("OPS/toc.ncx", f"<ncx><navMap>{points}</navMap></ncx>")
                listing, _ = self.invoke("inspect", source, "--list-chapters", "--json-events")
                self.assertEqual([(entry["title"], entry["units"]) for entry in json.loads(listing)["chapters"]],
                                 [("A", 2), ("B", 1), ("C", 2)])
                output, _ = self.invoke("sample", source, "--chapter", "2", "--no-auto-glossary",
                                        "--json-events", "--sample-pairs")
                self.assertEqual([event["original"] for event in map(json.loads, output.splitlines())
                                  if event["event"] == "sample_pair"], ["乙的正文。"])

    def test_epub_chapter_sample_resolves_distinct_svg_groups(self):
        base = self.directory / "base.epub"
        source = self.directory / "panels.epub"
        write_epub(base)
        with zipfile.ZipFile(base) as src, zipfile.ZipFile(source, "w") as dest:
            for info in src.infolist():
                data = src.read(info)
                if info.filename == "OPS/book.opf":
                    data = data.replace(b'href="chapter1.xhtml" media-type="application/xhtml+xml"',
                                        b'href="panels.svg" media-type="image/svg+xml"')
                    data = data.replace(b'<itemref idref="second"/>', b"")
                elif info.filename == "OPS/nav.xhtml":
                    data = data.replace(b"chapter1.xhtml#opening", b"panels.svg#a")
                    data = data.replace(b"chapter%202.xhtml#turn", b"panels.svg#b")
                dest.writestr(info, data)
            dest.writestr("OPS/panels.svg", '<svg xmlns="http://www.w3.org/2000/svg">'
                          '<g id="a"><text x="10">东门。</text></g><g id="b"><text x="50">西门。</text></g></svg>')
        listing, _ = self.invoke("inspect", source, "--list-chapters", "--json-events")
        self.assertEqual([entry["units"] for entry in json.loads(listing)["chapters"]], [1, 1])
        output, _ = self.invoke("sample", source, "--chapter", "2", "--no-auto-glossary",
                                "--json-events", "--sample-pairs")
        self.assertEqual([e["original"] for e in map(json.loads, output.splitlines())
                          if e["event"] == "sample_pair"], ["西门。"])

    def test_epub_spine_navigation_document_can_contain_chapters(self):
        base = self.directory / "base.epub"
        book = self.directory / "inline-nav.epub"
        write_epub(base)
        with zipfile.ZipFile(base) as src, zipfile.ZipFile(book, "w") as dest:
            for entry in src.infolist():
                data = src.read(entry)
                if entry.filename == "OPS/book.opf":
                    data = data.replace(b'<itemref idref="first"/><itemref idref="second"/>', b'<itemref idref="nav"/>')
                    data = b"\n".join(line for line in data.splitlines()
                                      if b'<item id="first"' not in line and b'<item id="second"' not in line)
                elif entry.filename in {"OPS/chapter1.xhtml", "OPS/chapter 2.xhtml"}:
                    continue
                elif entry.filename == "OPS/nav.xhtml":
                    data = ('<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">'
                            '<head><title>Contents</title></head><body>'
                            '<nav epub:type="toc"><h1>目录</h1><ol>'
                            '<li><a href="#a">山路</a></li><li><a href="#b">石门</a></li></ol></nav>'
                            '<h2 id="a">第一章</h2><p>雨停了。</p>'
                            '<h2 id="b">第二章</h2><p>门关上了。</p>'
                            '<nav epub:type="landmarks"><ol>'
                            '<li><a epub:type="bodymatter" href="#a">正文</a></li></ol></nav></body></html>').encode()
                dest.writestr(entry, data)
        listing, _ = self.invoke("inspect", book, "--list-chapters", "--json-events")
        chapters = json.loads(listing)["chapters"]
        self.assertEqual([(entry["title"], entry["units"]) for entry in chapters], [("山路", 2), ("石门", 2)])
        self.assertEqual(FakeCodex.calls, [])
        output, _ = self.invoke("sample", book, "--chapter", chapters[1]["reference"], "--no-auto-glossary",
                                "--json-events", "--sample-pairs")
        pairs = [event["original"] for event in map(json.loads, output.splitlines())
                 if event["event"] == "sample_pair"]
        self.assertEqual(pairs, ["第二章", "门关上了。"])
        self.assertEqual([block["text"] for block in FakeCodex.calls[0][1]["blocks"]], pairs)
        self.invoke("translate", book, "--no-auto-glossary")
        with zipfile.ZipFile(default_output(book, "translate")) as archive:
            translated = etree.fromstring(archive.read("OPS/nav.xhtml"))
            links = translated.findall(".//{*}a")
            self.assertEqual([link.get("href") for link in links], ["#a", "#b", "#a"])
            self.assertEqual([link.text for link in links], ["Гермиона вошёл."] * 3)
            self.assertEqual([part.text for part in translated.findall(".//{*}p")], ["Гермиона вошёл."] * 2)

    def test_epub_image_link_keeps_chapter_name_and_boundary(self):
        base = self.directory / "base.epub"
        book = self.directory / "image-nav.epub"
        write_epub(base)
        with zipfile.ZipFile(base) as src, zipfile.ZipFile(book, "w") as dest:
            for entry in src.infolist():
                data = src.read(entry)
                if entry.filename == "OPS/chapter1.xhtml":
                    data = ('<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Chapters</title></head><body>'
                            '<p id="a">雨停了。</p><p id="b">门关上了。</p><p id="c">雪下了。</p></body></html>').encode()
                elif entry.filename == "OPS/nav.xhtml":
                    data = ('<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">'
                            '<head><title>Contents</title></head><body><nav epub:type="toc"><ol>'
                            '<li><a href="chapter1.xhtml#a">山路</a></li>'
                            '<li><a href="chapter1.xhtml#b" aria-label="第二章">'
                            '<img xml:lang="zh" alt="第二章" src="mark.svg"/></a></li>'
                            '<li><a href="chapter1.xhtml#c">初雪</a></li>'
                            '<li><a href="chapter%202.xhtml#turn"><img alt="" src="mark.svg"/></a></li>'
                            '</ol></nav></body></html>').encode()
                elif entry.filename == "OPS/book.opf":
                    data = data.replace(b"</manifest>", b'<item id="mark" href="mark.svg" media-type="image/svg+xml"/>'
                                        b"</manifest>")
                dest.writestr(entry, data)
            dest.writestr("OPS/mark.svg", '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"/>')
        listing, _ = self.invoke("inspect", book, "--list-chapters", "--json-events")
        chapters = json.loads(listing)["chapters"]
        self.assertEqual([(entry["title"], entry["units"]) for entry in chapters],
                         [("山路", 1), ("第二章", 1), ("初雪", 1), ("turn", 2)])
        for chapter, original in zip(chapters[:3], ("雨停了。", "门关上了。", "雪下了。")):
            output, _ = self.invoke("sample", book, "--chapter", chapter["reference"], "--no-auto-glossary",
                                    "--json-events", "--sample-pairs")
            pairs = [event["original"] for event in map(json.loads, output.splitlines())
                     if event["event"] == "sample_pair"]
            self.assertEqual(pairs, [original])
            self.assertEqual([block["text"] for block in FakeCodex.calls[-1][1]["blocks"]], [original])
        self.invoke("translate", book, "--no-auto-glossary")
        with zipfile.ZipFile(default_output(book, "translate")) as archive:
            nav = etree.fromstring(archive.read("OPS/nav.xhtml"))
            link = next(node for node in nav.iter("{*}a") if node.get("href") == "chapter1.xhtml#b")
            self.assertEqual(link.get("aria-label"), "Гермиона вошёл.")
            image = link.find("{*}img")
            self.assertEqual(image.get("alt"), "Гермиона вошёл.")
            self.assertEqual(image.get("{http://www.w3.org/XML/1998/namespace}lang"), "ru")
            self.assertEqual(image.get("src"), "mark.svg")
            self.assertEqual(list(nav.iter("{*}img"))[1].get("alt"), "")

    def test_epub_chapter_resolves_an_anchor_on_the_html_element(self):
        original = self.directory / "base.epub"
        book = self.directory / "html-anchor.epub"
        write_epub(original)
        with zipfile.ZipFile(original) as src, zipfile.ZipFile(book, "w") as dest:
            for entry in src.infolist():
                data = src.read(entry)
                if entry.filename == "OPS/chapter 2.xhtml":
                    data = ('<html id="turn"><head><title>文件说明</title></head>'
                            '<body><p>门关上了。</p></body></html>').encode()
                dest.writestr(entry, data)
        listing, _ = self.invoke("inspect", book, "--list-chapters", "--json-events")
        entries = json.loads(listing)["chapters"]
        self.assertEqual([entry["title"] for entry in entries], ["Opening", "雨夜"])
        output, _ = self.invoke("sample", book, "--chapter", entries[1]["reference"], "--no-auto-glossary",
                                "--json-events", "--sample-pairs")
        self.assertEqual([event["original"] for event in map(json.loads, output.splitlines())
                          if event["event"] == "sample_pair"], ["门关上了。"])

    def test_inline_chapter_catalog_preserves_existing_batch_cache(self):
        base = self.directory / "base.epub"
        source = self.directory / "inline.epub"
        write_epub(base)
        for href in ("chapter1.xhtml#opening", "chapter1.xhtml#b"):
            with zipfile.ZipFile(base) as src, zipfile.ZipFile(source, "w") as dest:
                for info in src.infolist():
                    data = src.read(info)
                    if info.filename == "OPS/chapter1.xhtml":
                        data = ('<html><body><p id="opening">甲<span id="b">乙</span>丙。</p></body></html>').encode()
                    elif info.filename == "OPS/nav.xhtml":
                        data = data.replace(b"chapter1.xhtml#opening", href.encode())
                    dest.writestr(info, data)
            args = (source, "--no-auto-glossary", "--json-events", "--sample-pairs")
            if href.endswith("opening"):
                output, _ = self.invoke("sample", *args)
                first = [item for item in map(json.loads, output.splitlines()) if item["event"] == "sample_pair"]
                self.assertIn("甲乙丙。", [item["original"] for item in first])
            else:
                with patch("ai_translator.pipeline.Codex", side_effect=AssertionError("Unexpected request")):
                    output, _ = self.invoke("sample", *args, "--max-calls", "0", "-o", self.directory / "replay.txt")
                pairs = [item for item in map(json.loads, output.splitlines()) if item["event"] == "sample_pair"]
                self.assertEqual(pairs, first)
                output, _ = self.invoke("sample", *args, "--chapter", "2")
                self.assertEqual([item["original"] for item in map(json.loads, output.splitlines())
                                  if item["event"] == "sample_pair"], ["乙丙。"])

    def test_fb2_chapter_sample_covers_all_its_batches_and_omits_notes(self):
        source = self.directory / "book.fb2"
        source.write_text('<FictionBook><body><section><title><p>山路</p></title><p>哈利来了。</p></section>'
                          '<section><title><p>石门</p></title>' + '<p>赫敏来了。</p>' * 70 +
                          '</section><section><title><p>初雪</p></title><p>雪下了。</p></section></body>'
                          '<body name="notes"><section><p>译注。</p></section></body></FictionBook>', encoding="utf-8")
        listing, _ = self.invoke("inspect", source, "--list-chapters", "--json-events")
        self.assertEqual([chapter["title"] for chapter in json.loads(listing)["chapters"]], ["山路", "石门", "初雪"])
        output, _ = self.invoke("sample", source, "--chapter", "2", "--no-auto-glossary", "--json-events", "--sample-pairs")
        pairs = [item["original"] for item in map(json.loads, output.splitlines()) if item["event"] == "sample_pair"]
        self.assertEqual(pairs, ["石门"] + ["赫敏来了。"] * 70)
        self.assertEqual(len(FakeCodex.calls), 2)

    def test_epub_chapter_catalog_uses_ncx_or_headings_without_navigation_document(self):
        base = self.directory / "base.epub"
        write_epub(base)
        for ncx in (False, True):
            with self.subTest(ncx=ncx):
                book = self.directory / f"fallback-{ncx}.epub"
                with zipfile.ZipFile(base) as source, zipfile.ZipFile(book, "w") as dest:
                    for info in source.infolist():
                        data = source.read(info)
                        if info.filename == "OPS/book.opf":
                            data = data.replace(b' properties="nav"', b"")
                            if ncx:
                                data = data.replace(b'</manifest>', b'<item id="ncx" href="toc.ncx" '
                                                    b'media-type="application/x-dtbncx+xml"/></manifest>')
                                data = data.replace(b'<spine>', b'<spine toc="ncx">')
                        elif info.filename == "OPS/chapter1.xhtml":
                            data = ('<html><body><h2 id="first">第一章</h2><p>哈利来了。</p>'
                                    '<h2 id="second">第二章</h2><p>赫敏来了。</p></body></html>').encode()
                        dest.writestr(info, data)
                    if ncx:
                        dest.writestr("OPS/toc.ncx",
                                      '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/"><navMap>'
                                      '<navPoint id="c1"><navLabel><text>山路</text></navLabel>'
                                      '<content src="chapter1.xhtml#first"/></navPoint>'
                                      '<navPoint id="c2"><navLabel><text>石门</text></navLabel>'
                                      '<content src="chapter1.xhtml#second"/></navPoint></navMap></ncx>')
                output, _ = self.invoke("inspect", book, "--list-chapters", "--json-events")
                entries = json.loads(output)["chapters"]
                self.assertEqual([entry["title"] for entry in entries[:2]],
                                 ["山路", "石门"] if ncx else ["第一章", "第二章"])
                self.assertEqual(entries[0]["units"], 2)
                self.assertEqual(entries[1]["units"], 4 if ncx else 2)

    def test_epub_partial_toc_finds_missing_peer_heading_without_splitting_subheading(self):
        base = self.directory / "base.epub"
        book = self.directory / "partial.epub"
        write_epub(base)
        with zipfile.ZipFile(base) as src, zipfile.ZipFile(book, "w") as dest:
            for entry in src.infolist():
                data = src.read(entry)
                if entry.filename == "OPS/book.opf":
                    data = data.replace(b'<itemref idref="second"/>', b"")
                elif entry.filename == "OPS/chapter1.xhtml":
                    data = ('<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Chapters</title></head><body>'
                            '<h1 id="volume">上卷</h1>'
                            '<section id="a"><h2>甲章</h2><p>雨停了。</p></section>'
                            '<section><h2>石门</h2><p>门关上了。</p><h3>深夜</h3><p>灯灭了。</p></section>'
                            '<h2><span id="c">丙章</span></h2><p>雪下了。</p></body></html>').encode()
                elif entry.filename == "OPS/nav.xhtml":
                    data = ('<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">'
                            '<head><title>Contents</title></head><body><nav epub:type="toc"><ol>'
                            '<li><a href="chapter1.xhtml#volume">上卷</a></li>'
                            '<li><a href="chapter1.xhtml#a">山路</a></li>'
                            '<li><a href="chapter1.xhtml#c">初雪</a></li></ol></nav></body></html>').encode()
                dest.writestr(entry, data)
        listing, _ = self.invoke("inspect", book, "--list-chapters", "--json-events")
        chapters = json.loads(listing)["chapters"]
        self.assertEqual([(entry["title"], entry["units"]) for entry in chapters],
                         [("上卷", 1), ("山路", 2), ("石门", 4), ("初雪", 2)])
        for chapter, expected in zip(chapters[1:], (["甲章", "雨停了。"], ["石门", "门关上了。", "深夜", "灯灭了。"])):
            output, _ = self.invoke("sample", book, "--chapter", chapter["reference"], "--no-auto-glossary",
                                    "--json-events", "--sample-pairs")
            pairs = [event["original"] for event in map(json.loads, output.splitlines())
                     if event["event"] == "sample_pair"]
            self.assertEqual(pairs, expected)
            self.assertEqual([block["text"] for block in FakeCodex.calls[-1][1]["blocks"]], expected)

    def test_epub_fills_a_missing_nav_boundary_from_ncx_and_keeps_nav_labels(self):
        base = self.directory / "base.epub"
        book = self.directory / "dual-toc.epub"
        write_epub(base)
        with zipfile.ZipFile(base) as src, zipfile.ZipFile(book, "w") as dest:
            for entry in src.infolist():
                data = src.read(entry)
                if entry.filename == "OPS/book.opf":
                    data = data.replace(b"<spine>", b'<spine toc="ncx">')
                    data = data.replace(b"</manifest>", b'<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
                                        b"</manifest>")
                elif entry.filename == "OPS/chapter1.xhtml":
                    data = ('<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Chapters</title></head><body>'
                            '<p id="a">雨停了。</p><p id="b">门关上了。</p><p id="c">雪下了。</p></body></html>').encode()
                elif entry.filename == "OPS/nav.xhtml":
                    data = ('<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">'
                            '<head><title>Contents</title></head><body><nav epub:type="toc"><ol>'
                            '<li><a href="chapter1.xhtml#a">山路</a></li>'
                            '<li><a href="chapter1.xhtml#c">初雪</a></li>'
                            '<li><a href="chapter%202.xhtml#turn">雨夜</a></li></ol></nav></body></html>').encode()
                dest.writestr(entry, data)
            dest.writestr("OPS/toc.ncx", '<ncx><navMap>'
                          '<navPoint><navLabel><text>Different title</text></navLabel>'
                          '<content src="chapter1.xhtml#a"/></navPoint>'
                          '<navPoint><navLabel><text>石门</text></navLabel>'
                          '<content src="chapter1.xhtml#b"/></navPoint></navMap></ncx>')

        listing, _ = self.invoke("inspect", book, "--list-chapters", "--json-events")
        chapters = json.loads(listing)["chapters"]
        self.assertEqual([(entry["title"], entry["units"]) for entry in chapters],
                         [("山路", 1), ("石门", 1), ("初雪", 1), ("雨夜", 2)])
        for chapter, expected in zip(chapters[:2], ("雨停了。", "门关上了。")):
            args = (book, "--chapter", chapter["reference"], "--no-auto-glossary", "--json-events", "--sample-pairs")
            output, _ = self.invoke("sample", *args)
            pairs = [event["original"] for event in map(json.loads, output.splitlines())
                     if event["event"] == "sample_pair"]
            self.assertEqual(pairs, [expected])
            self.assertEqual([block["text"] for block in FakeCodex.calls[-1][1]["blocks"]], pairs)

    def test_fb2_parent_section_covers_children_and_finds_chapters_of_other_story_bodies(self):
        book = self.directory / "volumes.fb2"
        book.write_text('<FictionBook xmlns:l="http://www.w3.org/1999/xlink">'
                        '<body><section><title><p>上卷</p></title><epigraph><p>前言。</p></epigraph>'
                        '<section><title><p>山路</p></title><p>雨停了。</p>'
                        '<p id="n0">原注。</p><p>天晴了。</p>'
                        '<p><a l:href="#n0" type="note">0</a><a l:href="#n1" type="note">1</a>'
                        '<a l:href="#n2" type="note">2</a></p></section>'
                        '<section><title><p>石门</p></title><p>门关上了。</p></section>'
                        '</section></body><body name="notes"><section><p>译注。</p></section></body>'
                        '<body><section><p>脚注。</p></section></body>'
                        '<body name="annotations"><section><p id="n1">编者注。</p></section></body>'
                        '<body><section><title><p>下卷</p></title>'
                        '<p>雪下了。</p><p id="n2">脚注。</p><p>灯亮了。</p></section></body></FictionBook>',
                        encoding="utf-8")
        listing, _ = self.invoke("inspect", book, "--list-chapters", "--json-events")
        chapters = json.loads(listing)["chapters"]
        self.assertEqual([(entry["title"], entry["units"]) for entry in chapters],
                         [("上卷", 7), ("山路", 3), ("石门", 2), ("下卷", 3)])
        for chapter, expected in zip(chapters, (["上卷", "前言。", "山路", "雨停了。", "天晴了。", "石门", "门关上了。"],
                                               ["山路", "雨停了。", "天晴了。"], ["石门", "门关上了。"],
                                               ["下卷", "雪下了。", "灯亮了。"])):
            output, _ = self.invoke("sample", book, "--chapter", chapter["reference"], "--no-auto-glossary",
                                    "--json-events", "--sample-pairs")
            pairs = [event["original"] for event in map(json.loads, output.splitlines())
                     if event["event"] == "sample_pair"]
            self.assertEqual(pairs, expected)
        self.invoke("translate", book, "--no-auto-glossary")
        output = etree.parse(default_output(book, "translate"))
        self.assertEqual(output.find(".//p[@id='n0']").text, "Гермиона вошёл.")
        link = output.find(".//a[@type='note']")
        self.assertEqual(link.get("{http://www.w3.org/1999/xlink}href"), "#n0")

    def test_fb2_body_title_selects_entire_volume_and_keeps_its_sections_selectable(self):
        book = self.directory / "volumes.fb2"
        book.write_text('<FictionBook xmlns:l="http://www.w3.org/1999/xlink">'
                        '<body><title><p>上卷</p></title><section><title><p>山路</p></title>'
                        '<p>雨停了。</p></section></body>'
                        '<body><title><p>下卷</p></title><section>'
                        '<p>雪下了。</p><p id="n1">编者注。</p><p>灯亮了。</p>'
                        '<p><a l:href="#n1" type="note">1</a></p></section></body>'
                        '</FictionBook>', encoding="utf-8")
        output, _ = self.invoke("inspect", book, "--list-chapters", "--json-events")
        chapters = json.loads(output)["chapters"]
        self.assertEqual([(entry["title"], entry["units"]) for entry in chapters],
                         [("上卷", 3), ("山路", 2), ("下卷", 3), ("Раздел 1", 2)])
        for entry, expected in zip(chapters[2:], (["下卷", "雪下了。", "灯亮了。"], ["雪下了。", "灯亮了。"])):
            args = (book, "--chapter", entry["reference"], "--no-auto-glossary", "--json-events")
            plan, _ = self.invoke("inspect", "--for", "sample", *args)
            bounds = next(e for e in map(json.loads, plan.splitlines()) if e["event"] == "sample_range")
            self.assertEqual((bounds["opening"], bounds["closing"]), (expected[0], expected[-1]))
            output, _ = self.invoke("sample", *args, "--sample-pairs")
            pairs = [e["original"] for e in map(json.loads, output.splitlines()) if e["event"] == "sample_pair"]
            self.assertEqual(pairs, expected)
            self.assertEqual([block["text"] for block in FakeCodex.calls[-1][1]["blocks"]], expected)

    def test_txt_parenthetical_chapter_heading_does_not_absorb_the_next_chapter(self):
        self.source.write_text("第一章 山路\n雨停了。\n第二章里他才明白\n第二章（下）\n门关上了。\n",
                               encoding="utf-8-sig")
        options = (self.source, "--encoding", "utf-8", "--json-events")
        with patch("ai_translator.formats.CHAPTER_CATALOG_VERSION", 2):
            outdated, _ = self.invoke("inspect", *options, "--list-chapters")
        error, _ = self.invoke("sample", *options, "--chapter", json.loads(outdated)["chapters"][0]["reference"],
                               expected=1)
        self.assertIn("Выбор главы устарел", error)
        self.assertEqual(FakeCodex.calls, [])
        output, _ = self.invoke("inspect", *options, "--list-chapters")
        chapters = json.loads(output)["chapters"]
        self.assertEqual([(entry["title"], entry["units"]) for entry in chapters],
                         [("第一章 山路", 3), ("第二章（下）", 2)])
        output, _ = self.invoke("sample", *options, "--chapter", chapters[0]["reference"],
                                "--no-auto-glossary", "--sample-pairs")
        pairs = [event["original"] for event in map(json.loads, output.splitlines())
                 if event["event"] == "sample_pair"]
        self.assertEqual(pairs, ["第一章 山路", "雨停了。", "第二章里他才明白"])

    def test_txt_chapter_catalog_and_incompatible_batch_options(self):
        self.source.write_text("序言。\n第一章 山路\n哈利来了。\n第二章里他才明白\n第二章：谁来了？\n赫敏来了。\n",
                               encoding="utf-8")
        output, _ = self.invoke("inspect", self.source, "--list-chapters", "--json-events")
        catalog = json.loads(output)
        self.assertEqual([(item["title"], item["units"]) for item in catalog["chapters"]],
                         [("Начало книги", 1), ("第一章 山路", 3), ("第二章：谁来了？", 2)])
        reference = catalog["chapters"][2]["reference"]
        listing, _ = self.invoke("inspect", self.source, "--list-chapters")
        self.assertIn(f"{reference} · 第二章：谁来了？", listing)
        for extra in (("--chapter", "4"), ("--chapter", "2", "--batches", "1"),
                      ("--chapter", "2", "--start-batch", "1")):
            self.invoke("sample", self.source, *extra, expected=1)
        self.source.write_text("第一章 新章\n哈利来了。\n" + self.source.read_text(encoding="utf-8"), encoding="utf-8")
        _, log = self.invoke("sample", self.source, "--chapter", "2", "--chapter-revision", catalog["revision"],
                             expected=1)
        self.assertIn("Загрузите оглавление заново", log)
        _, log = self.invoke("sample", self.source, "--chapter", reference, expected=1)
        self.assertIn("Загрузите оглавление заново", log)
        self.assertEqual(FakeCodex.calls, [])
        self.assertFalse(default_state_path(self.source).exists())

    def test_epub_translates_embedded_svg_without_merging_it_with_prose(self):
        original = self.directory / "base.epub"
        source = self.directory / "inline-svg.epub"
        write_epub(original)
        svg_ns = "{http://www.w3.org/2000/svg}"
        with zipfile.ZipFile(original) as src, zipfile.ZipFile(source, "w") as dst:
            for entry in src.infolist():
                data = src.read(entry)
                if entry.filename == "OPS/chapter1.xhtml":
                    root = etree.fromstring(data)
                    paragraph = etree.SubElement(root.find("{*}body"), "{http://www.w3.org/1999/xhtml}p",
                                                 id="before-map")
                    paragraph.text = "她看了看"
                    illustration = etree.fromstring(
                        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 100">'
                        '<title>旧地图</title><desc>通往山谷的小路。</desc>'
                        '<path d="M0 0 L3 4"/><text x="8" y="9">入口'
                        '<tspan x="20" fill="red">东门</tspan><tspan x="80">西门</tspan></text>'
                        '</svg>'
                    )
                    paragraph.append(illustration)
                    illustration.tail = "便点了头。"
                    data = etree.tostring(root)
                elif entry.filename == "OPS/book.opf":
                    data = data.replace(b'id="first" href=', b'id="first" properties="svg" href=')
                dst.writestr(entry, data)

        book = read_book(source)
        units = book.sections[1]
        self.assertEqual([unit.visible_text(unit.source) for unit in units[-4:]], [
            "她看了看便点了头。", "旧地图", "通往山谷的小路。", "入口东门西门",
        ])
        pieces = [piece for group in batches(book, 2400) for piece in group if piece.unit is units[-1]]
        self.assertEqual(len(pieces), 1)
        first, second, third, fourth = ANCHOR.findall(pieces[0].text)
        with self.assertRaisesRegex(InvalidAnswer, "позиционированной подписи SVG"):
            _translations({"translations": [{
                "id": 1, "text": f"Вход{first}Восточные и западные ворота{second}{third}{fourth}",
            }]}, pieces)

        self.invoke("translate", source, "--no-auto-glossary")
        blocks = [block for _, payload in FakeCodex.calls for block in payload["blocks"]]
        self.assertEqual(next(block["positioned_labels"] for block in blocks if "positioned_labels" in block),
                         [[first, second], [third, fourth]])
        with zipfile.ZipFile(self.directory / "inline-svg.ru.epub") as archive:
            root = etree.fromstring(archive.read("OPS/chapter1.xhtml"))
            paragraph = root.find('.//{*}p[@id="before-map"]')
            illustration = paragraph.find(svg_ns + "svg")
            self.assertIn("Гермиона", paragraph.text)
            self.assertIn("Текст", illustration.tail)
            self.assertEqual(illustration.get("viewBox"), "0 0 200 100")
            self.assertEqual(illustration.find(svg_ns + "path").get("d"), "M0 0 L3 4")
            for label in ("title", "desc", "text"):
                self.assertIn("Гермиона", illustration.find(svg_ns + label).text)
            caption = illustration.find(svg_ns + "text")
            self.assertEqual((caption.get("x"), caption.get("y")), ("8", "9"))
            east, west = caption.findall(svg_ns + "tspan")
            self.assertEqual(east.get("fill"), "red")
            self.assertEqual((east.get("x"), west.get("x")), ("20", "80"))
            self.assertIn("Текст", east.text)
            self.assertIn("Текст", west.text)

    def test_long_labels_are_validated_on_both_sides_of_a_batch_boundary(self):
        label = "穿过东门继续沿河走。" * 85
        for tag, markup, field in (
            ("tspan", f'<text xmlns="http://www.w3.org/2000/svg">地图<tspan x="32">{label}</tspan>河岸。</text>',
             "positioned_labels"),
            ("a", f'<p>地图<a href="#river">{label}</a>河岸。</p>', "link_labels"),
        ):
            with self.subTest(tag=tag):
                root = etree.fromstring(markup.encode())
                units = _xml_units([root])
                groups = list(batches(Book([units], lambda _: None), 400))
                self.assertEqual(len(groups), 3)
                first, last = ANCHOR.findall(units[0].source)
                self.assertEqual([_blocks(group)[0][field] for group in groups],
                                 [((first, None),), ((None, None),), ((None, last),)])
                route = ("Пройдите через восточные ворота и идите вдоль реки. " * 8).strip()
                for group, empty in zip(groups, (f"{route}{first}", "", f"{last}{route}")):
                    with self.assertRaisesRegex(InvalidAnswer, "Пропущен текст"):
                        _translations({"translations": [{"id": 1, "text": empty}]}, group)

                results = []
                for group, text in zip(groups, (f"Карта.{first}{route}", route, f"{route}{last}Край карты.")):
                    results.extend(_translations({"translations": [{"id": 1, "text": text}]}, group))
                units[0].replace(" ".join(results))
                self.assertEqual(root[0].text, " ".join([route] * 3))
                self.assertEqual(root[0].tail, "Край карты.")

    def test_antigravity_doctor_distinguishes_signed_out_from_a_timeout(self):
        args = ("doctor", "--provider", "antigravity", "--json-events")
        with patch("ai_translator.provider.shutil.which", return_value="/fake/agy"), patch(
            "ai_translator.provider._run_cli"
        ) as run:
            for diagnostic in ("Authentication required", "LOGIN REQUIRED", "Not signed in", "Authentication failed"):
                with self.subTest(diagnostic=diagnostic):
                    run.return_value = subprocess.CompletedProcess([], 0, "", f"{diagnostic}. private detail")
                    stdout, _ = self.invoke(*args, expected=4)
                    event = json.loads(stdout)
                    self.assertEqual(event["event"], "authentication")
                    self.assertNotIn("private", event["message"])
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            stdout, _ = self.invoke(*args, expected=1)
            self.assertEqual(json.loads(stdout)["event"], "error")
            self.assertIn("не подтвердил вход", json.loads(stdout)["message"])
            run.return_value = subprocess.CompletedProcess([], 0, "", "Authentication successful")
            stdout, _ = self.invoke(*args)
            self.assertEqual(json.loads(stdout)["event"], "authentication_ok")
            session = json.dumps({"event": "init", "conversation_id": "offline-session", "init": {"cwd": "/test"}})
            run.return_value = subprocess.CompletedProcess([], 0, session, "")
            stdout, _ = self.invoke(*args)
            self.assertEqual(json.loads(stdout)["event"], "authentication_ok")
            run.return_value = subprocess.CompletedProcess([], 0, session, "Authentication expired")
            self.source.write_text("雨停了。\n", encoding="utf-8")
            run.reset_mock()
            self.invoke("sample", self.source, "--provider", "antigravity", "--max-total-calls", 1, expected=4)
            self.assertEqual(run.call_count, 1)
            self.assertFalse(default_output(self.source, "sample").exists())
            with open_state(default_state_path(self.source)) as state:
                self.assertEqual(state.execute("SELECT attempts FROM request_budget").fetchone(), (0,))

            run.side_effect = subprocess.TimeoutExpired("agy", 10)
            stdout, _ = self.invoke(*args, expected=1)
            event = json.loads(stdout)
            self.assertEqual(event["event"], "error")
            self.assertIn("за 10 с", event["message"])

    def test_codex_doctor_separates_technical_failures_from_missing_chatgpt_login(self):
        with patch("ai_translator.pipeline.Codex", Codex), patch(
            "ai_translator.provider.shutil.which", return_value="/fake/codex",
        ), patch(
            "ai_translator.provider._run_cli"
        ) as run, patch.dict(os.environ, {"CODEX_HOME": str(self.directory)}):
            args = ("doctor", "--provider", "codex", "--json-events")
            for code, out, err, expected, kind, message in (
                (2, "", "error: unexpected argument: hidden input", 1, "error", "Версия Codex CLI"),
                (1, "", "Not logged in", 4, "authentication", "codex login"),
                (0, "Logged in using an API key", "", 4, "authentication", "codex login"),
                (0, "", "", 1, "error", "Не удалось подтвердить вход"),
                (0, "", "Logged in using ChatGPT", 0, "authentication_ok", "подтверждён"),
            ):
                with self.subTest(code=code, out=out, err=err):
                    run.return_value = subprocess.CompletedProcess([], code, out, err)
                    stdout, _ = self.invoke(*args, expected=expected)
                    event = json.loads(stdout)
                    self.assertEqual(event["event"], kind)
                    self.assertIn(message, event["message"])
                    self.assertNotIn("hidden input", event["message"])

    def test_rejects_unsupported_epub_spine_before_model_calls(self):
        source = self.directory / "source.epub"
        write_epub(source)
        unsupported = self.directory / "unsupported.epub"
        with zipfile.ZipFile(source) as original, zipfile.ZipFile(unsupported, "w") as changed:
            for entry in original.infolist():
                data = original.read(entry)
                if entry.filename == "OPS/book.opf":
                    data = data.replace(b'application/xhtml+xml', b'image/png')
                changed.writestr(entry, data)
        _, log = self.invoke("translate", unsupported, expected=1)
        self.assertIn("Неподдерживаемый раздел", log)
        self.assertEqual(FakeCodex.calls, [])

    def test_fb2_preserves_notes_and_binary(self):
        source = self.directory / "book.fb2"
        write_fb2(source)
        self.invoke("translate", source, "--no-auto-glossary")
        result = etree.parse(self.directory / "book.ru.fb2")
        self.assertEqual(result.find(".//{*}lang").text, "ru")
        self.assertEqual(result.find(".//{*}binary").text, "YWJj")
        self.assertEqual(result.find(".//{*}a").get("{http://www.w3.org/1999/xlink}href"), "#n1")
        self.assertTrue(result.find(".//{*}emphasis").text)
        self.assertIn("Гермиона", result.find(".//{*}section[@id='n1']/{*}p").text)

    def test_protect_input_and_existing_output(self):
        self.source.write_text("哈利来了。", encoding="utf-8")
        self.invoke("translate", self.source, "-o", self.source, "--force", expected=1)
        output = self.directory / "exists.txt"
        output.write_text("Существующий текст", encoding="utf-8")
        self.invoke("translate", self.source, "-o", output, expected=1)
        self.assertEqual(output.read_text(), "Существующий текст")
        self.assertEqual(FakeCodex.calls, [])

    def test_request_budget_stops_then_resumes(self):
        self.source.write_text("哈利来了。\n", encoding="utf-8")
        args = ("translate", self.source, "--max-calls", "1")
        _, log = self.invoke(*args, expected=3)
        self.assertIn("Достигнут лимит --max-calls", log)
        self.assertEqual(len(FakeCodex.calls), 1)
        _, log = self.invoke(*args)
        self.assertIn("Ответов из кеша: 1", log)
        self.assertEqual(len(FakeCodex.calls), 2)

    def test_total_request_budget_survives_resume_and_counts_failed_attempts(self):
        self.source.write_text("哈利来了。", encoding="utf-8")
        args = ("translate", self.source, "--no-auto-glossary")
        with patch.object(FakeCodex, "complete", side_effect=ProviderError("Сбой связи")) as complete:
            self.invoke(*args, "--max-total-calls", "1", expected=1)
            self.assertEqual(complete.call_count, 1)

        with patch("ai_translator.pipeline.Codex", side_effect=AssertionError("Бюджет исчерпан")):
            _, log = self.invoke(*args, expected=3)
        self.assertIn("Исчерпан общий лимит запросов: 1/1", log)
        output, _ = self.invoke("inspect", self.source, "--json-events")
        events = {item["event"]: item for item in map(json.loads, output.splitlines())}
        self.assertEqual(events["request_budget"]["attempts"], 1)
        self.assertEqual(events["request_budget"]["ceiling"], 1)
        self.invoke(*args, "--max-total-calls", "2")
        self.invoke(*args, "--force", "--max-calls", "0")
        output, _ = self.invoke("inspect", self.source, "--json-events")
        self.assertIn('"attempts": 2, "ceiling": 2', output)
        self.invoke(*args, "--force", "--max-total-calls", "-1")
        output, _ = self.invoke("inspect", self.source, "--json-events")
        self.assertIn('"attempts": 2, "ceiling": null', output)

    def test_total_request_budget_includes_a_worker_killed_during_submission(self):
        self.source.write_text("哈利来了。", encoding="utf-8")
        program = """
import os
import sys
from ai_translator import pipeline
from ai_translator.cli import main
class InterruptedProvider:
    def check(self): pass
    def complete(self,*args): os._exit(92)
pipeline.provider_backend = lambda *args: InterruptedProvider()
raise SystemExit(main(sys.argv[1:]))
"""
        command = ("translate", str(self.source), "--no-auto-glossary")
        result = subprocess.run([sys.executable, "-c", program, *command, "--max-total-calls", "1"],
                                capture_output=True, timeout=10, check=False)
        self.assertEqual(result.returncode, 92, result.stderr)
        _, log = self.invoke(*command, expected=3)
        self.assertIn("Исчерпан общий лимит запросов: 1/1", log)
        self.assertFalse(FakeCodex.calls)

    def test_interrupted_reservation_is_in_final_cli_report(self):
        self.source.write_text("哈利来了。", encoding="utf-8")

        def interrupt_after_commit(requests, field, value):
            if field == "calls" and value == 1:
                raise KeyboardInterrupt
            object.__setattr__(requests, field, value)

        with patch.object(Requests, "__setattr__", interrupt_after_commit):
            output, _ = self.invoke("translate", self.source, "--no-auto-glossary", "--max-total-calls=1",
                                    "--json-events", expected=130)
        events = {event["event"]: event for event in map(json.loads, output.splitlines())}
        self.assertEqual(events["requests"]["calls"], 1)
        self.assertEqual((events["request_budget"]["attempts"], events["request_budget"]["ceiling"]), (1, 1))
        inspection, _ = self.invoke("inspect", self.source, "--no-auto-glossary", "--json-events")
        self.assertIn('"attempts": 1, "ceiling": 1', inspection)
        self.assertFalse(FakeCodex.calls)

    def test_total_request_budget_migrates_existing_cache_without_touching_answers(self):
        self.source.write_text("哈利来了。", encoding="utf-8")
        args = ("translate", self.source, "--no-auto-glossary")
        self.invoke(*args)
        cache = self.directory / ".Book.txt.ai-translator.sqlite3"
        with open_state(cache) as connection:
            with connection:
                connection.execute("DROP TABLE request_budget")
        self.invoke(*args, "--force", "--max-total-calls", "1")
        self.assertEqual(len(FakeCodex.calls), 1)
        with open_state(cache) as connection:
            self.assertEqual(connection.execute("SELECT attempts, ceiling FROM request_budget").fetchone(), (1, 1))
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM responses").fetchone()[0], 1)

    def test_total_request_budget_does_not_charge_failed_authentication(self):
        self.source.write_text("哈利来了。", encoding="utf-8")
        args = ("translate", self.source, "--no-auto-glossary", "--max-total-calls", "1")
        with patch.object(FakeCodex, "check", side_effect=AuthenticationError("Требуется вход")):
            self.invoke(*args, expected=4)
        self.invoke("translate", self.source, "--no-auto-glossary")
        self.assertEqual(len(FakeCodex.calls), 1)

    def test_total_request_budget_pauses_and_resumes_pending_correction(self):
        self.source.write_text("哈利来了。", encoding="utf-8")
        args = ("translate", self.source, "--no-auto-glossary")
        with patch.object(FakeCodex, "complete", return_value={"translations": []}) as provider:
            self.invoke(*args, "--max-total-calls", "1", expected=3)
            self.invoke(*args, expected=3)
            self.assertEqual(provider.call_count, 1)
        self.invoke(*args, "--max-total-calls", "2")
        cache = self.directory / ".Book.txt.ai-translator.sqlite3"
        with open_state(cache) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM corrections").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT attempts, ceiling FROM request_budget").fetchone(), (2, 2))

    def test_inspection_replays_partial_and_complete_book_without_writes_or_provider(self):
        self.source.write_text("哈利来了。\n", encoding="utf-8")
        args = (self.source, "--edit")
        self.invoke("translate", *args, "--max-calls", "2", expected=3)
        cache = self.directory / ".Book.txt.ai-translator.sqlite3"

        def snapshot():
            raw = cache.read_bytes()
            with patch("ai_translator.pipeline.provider_backend", side_effect=AssertionError("no provider during inspect")):
                output, _ = self.invoke("inspect", *args, "--json-events")
            self.assertEqual(cache.read_bytes(), raw)
            events = {item["event"]: item for item in map(json.loads, output.splitlines())}
            return events["cache_replay"]

        partial = snapshot()
        self.assertEqual(partial["reusable_answers"], 2)
        self.assertFalse(partial["complete"])
        self.assertEqual(partial["first_missing_stage"], "edit")
        self.invoke("translate", *args)
        full = snapshot()
        self.assertEqual(full["reusable_answers"], 3)
        self.assertTrue(full["complete"])
        self.assertIsNone(full["first_missing_stage"])

    def test_inspection_does_not_delete_invalid_cached_replies(self):
        self.source.write_text("哈利来了。", encoding="utf-8")
        opts = (self.source, "--no-auto-glossary")
        self.invoke("translate", *opts)
        cache = self.directory / ".Book.txt.ai-translator.sqlite3"
        for damaged in ("broken", b"\xff"):
            with self.subTest(damaged=damaged):
                with open_state(cache) as db:
                    with db:
                        db.execute("UPDATE responses SET body = ?", (damaged,))
                before = cache.read_bytes()
                output, _ = self.invoke("inspect", *opts, "--json-events")
                events = {item["event"]: item for item in map(json.loads, output.splitlines())}
                self.assertEqual(events["cache_replay"]["first_missing_stage"], "translate")
                self.assertEqual(events["cache_replay"]["reusable_answers"], 0)
                self.assertEqual(cache.read_bytes(), before)

    def test_inspection_of_new_book_reports_a_miss_without_creating_state(self):
        self.source.write_text("哈利来了。", encoding="utf-8")
        output, _ = self.invoke("inspect", self.source, "--no-auto-glossary", "--provider", "claude-code",
                                "--json-events")
        events = {item["event"]: item for item in map(json.loads, output.splitlines())}
        self.assertEqual(events["settings"]["provider"], "claude-code")
        self.assertEqual(events["cache_replay"]["reusable_answers"], 0)
        self.assertEqual(events["cache_replay"]["first_missing_stage"], "translate")
        self.assertFalse((self.directory / ".Book.txt.ai-translator.sqlite3").exists())

    def test_inspection_rejects_cache_directory_but_accepts_new_cache_filename(self):
        self.source.write_text("哈利来了。", encoding="utf-8")
        cache = self.directory / "папка кеша"
        cache.mkdir()
        for command in ("inspect", "translate"):
            with self.subTest(command=command):
                output, _ = self.invoke(command, self.source, "--state", cache, "--json-events", expected=1)
                self.assertIn("Для кеша нужен путь к файлу SQLite.", output)
        state = cache / "answers.sqlite3"
        output, _ = self.invoke("inspect", self.source, "--state", state, "--json-events")
        events = {item["event"]: item for item in map(json.loads, output.splitlines())}
        self.assertEqual(events["cache_replay"]["reusable_answers"], 0)
        self.assertFalse(state.exists())
        self.assertEqual(FakeCodex.calls, [])

    def test_cli_validates_model_and_reasoning_syntax_without_calling_provider(self):
        self.source.write_text("哈利来了。", encoding="utf-8")
        config = self.directory / "profile.toml"
        for field in ('model = "--model"', 'reasoning = "low, high"'):
            with self.subTest(field=field):
                config.write_text(f'[translate]\n{field}\n', encoding="utf-8")
                _, log = self.invoke("translate", self.source, "--config", config, expected=1)
                self.assertIn(f"[translate] {field.split()[0]}", log)
        config.write_text('[translate]\nmodel = "custom/model:fast"\nreasoning = "custom_effort"\n',
                          encoding="utf-8")
        stdout, _ = self.invoke("inspect", self.source, "--config", config)
        self.assertIn("translate=custom/model:fast/custom_effort", stdout)
        self.assertFalse(default_state_path(self.source).exists())
        self.assertEqual(FakeCodex.calls, [])

    def test_edit_review_uses_cached_versions_and_selects_without_rewriting(self):
        from ai_translator.pipeline import _review_choices

        for response in ({"choices": [{"id": 1, "version": "new"}]},
                         {"choices": [{"id": 1, "version": "draft"}, {"id": 1, "version": "edited"}]}):
            with self.assertRaises(InvalidAnswer):
                _review_choices(response, {1} if len(response["choices"]) == 1 else {1, 2})

        self.source.write_text("哈利来了。\n赫敏也来了。\n", encoding="utf-8")
        config = self.directory / "style.toml"
        config.write_text('[review]\nmodel = "review-model"\nreasoning = "high"\n', encoding="utf-8")
        args = (self.source, "--config", config, "--no-auto-glossary", "--edit", "--review-edits")
        self.invoke("translate", *args, "--max-total-calls", "2", expected=3)
        self.assertEqual(len(FakeCodex.calls), 2)
        self.assertFalse((self.directory / "Book.ru.txt").exists())
        output, _ = self.invoke("inspect", *args, "--json-events")
        events = {event["event"]: event for event in map(json.loads, output.splitlines())}
        self.assertEqual(events["cache_replay"]["first_missing_stage"], "review")
        self.assertEqual(events["plan"]["review"], 1)
        self.invoke("translate", *args, "--max-total-calls", "3")
        self.assertEqual(len(FakeCodex.calls), 3)
        self.assertEqual(FakeCodex.calls[-1][0], Model("review-model", "high"))
        self.assertEqual((self.directory / "Book.ru.txt").read_text(), "Гермиона вошёл.\nГермиона вошёл тихо.\n")
        output, _ = self.invoke("inspect", *args, "--json-events")
        self.assertIn('"reusable_answers": 3, "complete": true', output)

    def test_review_skips_unchanged_edit(self):
        self.source.write_text("哈利来了。", encoding="utf-8")
        result = {"translations": [{"id": 1, "text": "Гарри вернулся."}]}
        with patch.object(FakeCodex, "complete", return_value=result) as complete:
            self.invoke("translate", self.source, "--no-auto-glossary", "--edit", "--review-edits",
                        "--max-calls", "2")
            self.assertEqual(complete.call_count, 2)

    def test_reviewed_text_is_the_context_for_editing_the_next_batch(self):
        self.source.write_text("哈利来了。\n" * 65, encoding="utf-8")
        saved = FakeCodex.complete

        def choose_draft(provider, model, instructions, data, schema):
            response = saved(provider, model, instructions, data, schema)
            if "choices" in response:
                return {"choices": [{"id": entry["id"], "version": "draft"} for entry in response["choices"]]}
            return response

        with patch.object(FakeCodex, "complete", choose_draft):
            self.invoke("translate", self.source, "--no-auto-glossary", "--edit", "--review-edits")
        edit_contexts = [payload["preceding"] for _, payload in FakeCodex.calls
                         if "draft" in payload and "changed" not in payload]
        self.assertEqual(len(edit_contexts), 2)
        self.assertIn("Гермиона вошёл.", edit_contexts[1])
        self.assertNotIn("тихо", edit_contexts[1])

    def test_later_review_correction_resumes_without_replacing_existing_outputs(self):
        self.source.write_text("哈利来了。\n" * 65, encoding="utf-8")
        book = self.directory / "Book.ru.txt"
        glossary = self.directory / "Book.glossary.json"
        book.write_bytes(b"Old book\n")
        glossary.write_bytes(b"{}\n")
        saved = FakeCodex.complete
        reviews = []

        def complete(provider, model, instructions, data, schema):
            response = saved(provider, model, instructions, data, schema)
            if "choices" in response:
                reviews.append(instructions)
                if len(reviews) == 2:
                    return {"choices": []}
            return response

        args = ("translate", self.source, "--no-auto-glossary", "--edit", "--review-edits",
                "--force", "--save-glossary", glossary)
        with patch.object(FakeCodex, "complete", complete):
            self.invoke(*args, "--max-total-calls", "6", expected=3)
            self.assertEqual(len(FakeCodex.calls), 6)
            self.assertEqual(book.read_bytes(), b"Old book\n")
            self.assertEqual(glossary.read_bytes(), b"{}\n")
            self.invoke(*args, "--max-total-calls", "7")
        self.assertEqual(len(FakeCodex.calls), 7)
        self.assertEqual(len(reviews), 3)
        self.assertIn("Предыдущий ответ отклонён", reviews[-1])
        self.assertEqual(len(book.read_text(encoding="utf-8").splitlines()), 65)

    def test_changing_review_settings_reuses_draft_and_edit(self):
        self.source.write_text("哈利来了。\n", encoding="utf-8")
        config = self.directory / "review.toml"
        args = ("translate", self.source, "--config", config, "--no-auto-glossary", "--edit", "--review-edits")
        config.write_text('[review]\nmodel = "first-reviewer"\nreasoning = "low"\n', encoding="utf-8")
        self.invoke(*args, "--max-calls", "3")
        config.write_text('[review]\nmodel = "second-reviewer"\nreasoning = "high"\n', encoding="utf-8")
        self.invoke(*args, "--force", "--max-calls", "1")
        self.assertEqual(len(FakeCodex.calls), 4)
        self.assertEqual(FakeCodex.calls[-1][0], Model("second-reviewer", "high"))
        self.assertIn("changed", FakeCodex.calls[-1][1])

    def test_inspection_updates_provisional_plan_after_cached_glossary_rebatch(self):
        self.source.write_text("张三" + "雨" * 397 + "张三" + "夜" * 399, encoding="utf-8")
        config = self.directory / "plan.toml"
        config.write_text("batch_chars = 400\n", encoding="utf-8")
        options = (self.source, "--config", config, "--edit", "--review-edits")

        def estimate():
            output, _ = self.invoke("inspect", *options, "--json-events")
            events = {event["event"]: event for event in map(json.loads, output.splitlines())}
            self.assertIn("Пакетов до автоглоссария: 2", events["inspection"]["message"])
            return events["plan"]

        cold = estimate()
        self.assertTrue(cold["provisional"])
        self.assertEqual((cold["glossary"], cold["translate"]), (2, 2))
        with patch.object(FakeCodex, "complete", return_value={"entries": [{"zh": "张三", "ru": "Чжан Сань"}]}):
            self.invoke("translate", *options, "--max-calls", "2", expected=3)
        warm = estimate()
        self.assertFalse(warm["provisional"])
        self.assertEqual((warm["glossary"], warm["translate"], warm["edit"], warm["review"]), (2, 3, 3, 3))

    def test_budget_one_resumes_the_correction(self):
        self.source.write_text("哈利来了。\n", encoding="utf-8")
        real_complete = FakeCodex.complete
        instructions_seen = []

        def complete(backend, model, instructions, data, schema):
            instructions_seen.append(instructions)
            if len(instructions_seen) == 1:
                return {"translations": []}
            return real_complete(backend, model, instructions, data, schema)

        args = ("translate", self.source, "--no-auto-glossary", "--max-calls", "1")
        with patch.object(FakeCodex, "complete", complete):
            self.invoke(*args, expected=3)
            self.invoke(*args)
        self.assertEqual(len(instructions_seen), 2)
        self.assertIn("Предыдущий ответ отклонён", instructions_seen[1])

    def test_sample_limits_calls_and_reuses_draft_in_full_run(self):
        self.source.write_text(("哈利来了。\n" * 64) + "赫敏到了。\n", encoding="utf-8")
        args = (self.source, "--no-auto-glossary")
        self.invoke("sample", *args, "--max-calls", "1")
        comparison = (self.directory / "Book.sample.ru.txt").read_text()
        self.assertIn("哈利来了。\nГермиона вошёл.", comparison)
        self.assertNotIn("赫敏到了。", comparison)
        self.assertFalse((self.directory / "Book.ru.txt").exists())
        self.assertEqual(len(FakeCodex.calls), 1)
        _, log = self.invoke("translate", *args)
        self.assertIn("Ответов из кеша: 1", log)
        self.assertEqual(len(FakeCodex.calls), 2)

    def test_sample_json_pairs_are_opt_in_and_follow_publication(self):
        self.source.write_text("哈利来了。\n赫敏到了。\n", encoding="utf-8")
        args = ("sample", self.source, "--no-auto-glossary", "--json-events")
        output, _ = self.invoke(*args)
        self.assertNotIn("sample_pair", output)
        saved = self.directory / "with-pairs.txt"
        output, _ = self.invoke(*args, "--sample-pairs", "--max-calls", "0", "-o", saved)
        events = list(map(json.loads, output.splitlines()))
        pairs = [(event["original"], event["translation"]) for event in events if event["event"] == "sample_pair"]
        self.assertEqual(pairs, [("哈利来了。", "Гермиона вошёл."), ("赫敏到了。", "Гермиона вошёл.")])
        self.assertEqual(events[-1]["event"], "output")
        self.assertEqual(saved.read_text(encoding="utf-8"), "哈利来了。\nГермиона вошёл.\n\n赫敏到了。\nГермиона вошёл.\n")
        self.assertEqual(len(FakeCodex.calls), 1)
        with patch("ai_translator.cli.write_outputs", side_effect=FileExistsError):
            output, _ = self.invoke(*args, "--sample-pairs", "--max-calls", "0", "--force", "-o", saved, expected=1)
        self.assertTrue({record["event"] for record in map(json.loads, output.splitlines())}.isdisjoint({"sample_pair", "output"}))

    def test_sample_versions_follow_review_and_replay_without_new_calls(self):
        self.source.write_text("哈利来了。\n赫敏到了。\n", encoding="utf-8")
        args = ("sample", self.source, "--no-auto-glossary", "--edit", "--review-edits",
                "--json-events", "--sample-pairs", "--sample-versions")
        output, _ = self.invoke(*args, "--max-calls=2", expected=3)
        self.assertNotIn('"event": "sample_pair"', output)
        saved = self.directory / "Book.sample.ru.txt"
        self.assertFalse(saved.exists())

        output, _ = self.invoke(*args, "--max-calls=1")
        pairs = [event for event in map(json.loads, output.splitlines()) if event["event"] == "sample_pair"]
        self.assertEqual([pair["review_choice"] for pair in pairs], ["draft", "edited"])
        self.assertEqual([pair["translation"] for pair in pairs], [pairs[0]["draft"], pairs[1]["edited"]])
        self.assertNotEqual(pairs[0]["draft"], pairs[0]["edited"])
        self.assertEqual(saved.read_text(encoding="utf-8"), "\n\n".join(
            f"{pair['original']}\n{pair['translation']}" for pair in pairs
        ) + "\n")

        output, _ = self.invoke(*args, "--max-calls=0", "--force")
        replayed = [event for event in map(json.loads, output.splitlines()) if event["event"] == "sample_pair"]
        self.assertEqual(replayed, pairs)
        self.assertEqual(len(FakeCodex.calls), 3)

    def test_sample_versions_distinguish_unchanged_edit_from_reviewer_choice(self):
        self.source.write_text("哈利来了。", encoding="utf-8")
        options = ("sample", self.source, "--no-auto-glossary", "--json-events", "--sample-pairs")
        output, _ = self.invoke(*options, "--sample-versions", expected=1)
        self.assertIn("--sample-versions применяется", output)
        self.assertEqual(FakeCodex.calls, [])

        response = {"translations": [{"id": 1, "text": "Гарри вернулся."}]}
        with patch.object(FakeCodex, "complete", return_value=response) as complete:
            output, _ = self.invoke(*options, "--sample-versions", "--edit", "--review-edits", "--max-calls=2")
        pair = next(event for event in map(json.loads, output.splitlines()) if event["event"] == "sample_pair")
        self.assertIsNone(pair["review_choice"])
        self.assertEqual(pair["draft"], pair["edited"])
        self.assertEqual(pair["translation"], pair["edited"])
        self.assertEqual(complete.call_count, 2)

        output, _ = self.invoke(*options, "--edit", "--review-edits", "--force", "--max-calls=0")
        pair = next(event for event in map(json.loads, output.splitlines()) if event["event"] == "sample_pair")
        self.assertEqual(set(pair), {"event", "message", "original", "translation"})

    def test_repair_review_can_correct_identical_versions_without_repeating_translation_or_edit(self):
        self.source.write_text("哈利已经回来了。", encoding="utf-8")
        base = (self.source, "--no-auto-glossary", "--edit", "--json-events")
        unchanged = {"translations": [{"id": 1, "text": "Гарри вернулся."}]}
        repaired = {"choices": [{"id": 1, "version": "repaired", "text": "Гарри уже вернулся."}]}
        saved = self.directory / "repaired.txt"
        with patch.object(FakeCodex, "complete", side_effect=[unchanged, unchanged, repaired]) as complete:
            self.invoke("sample", *base, "--review-edits", "--max-calls=2")
            output, _ = self.invoke("inspect", *base, "--for=sample", "--repair-review")
            events = {event["event"]: event for event in map(json.loads, output.splitlines())}
            self.assertEqual(events["plan"]["review"], 1)
            self.assertEqual(events["cache_replay"]["reusable_answers"], 2)
            self.assertEqual(events["cache_replay"]["first_missing_stage"], "review")
            options = (*base, "--repair-review", "--sample-pairs", "--sample-versions", "-o", saved)
            self.invoke("sample", *options, "--max-calls=0", expected=3)
            self.assertFalse(saved.exists())
            output, _ = self.invoke("sample", *options, "--max-calls=1")
            pair = next(event for event in map(json.loads, output.splitlines()) if event["event"] == "sample_pair")
            self.assertEqual(pair["review_choice"], "repaired")
            self.assertEqual(pair["draft"], pair["edited"])
            self.assertEqual(pair["translation"], "Гарри уже вернулся.")
            replay, _ = self.invoke("sample", *options, "--max-calls=0", "--force")
            replayed = next(event for event in map(json.loads, replay.splitlines()) if event["event"] == "sample_pair")
            self.assertEqual(replayed, pair)
        self.assertEqual(complete.call_count, 3)
        self.assertEqual(saved.read_text(encoding="utf-8"), "哈利已经回来了。\nГарри уже вернулся.\n")

    def test_repair_review_rejects_extra_properties_and_nonempty_text_on_a_choice(self):
        from ai_translator.pipeline import _repair_choices

        valid = {"id": 1, "version": "draft", "text": ""}
        variants = ({"choices": [valid], "note": ""},
                    {"choices": [{**valid, "note": ""}]},
                    {"choices": [{**valid, "text": " "}]})
        for response in variants:
            with self.subTest(response=response), self.assertRaises(InvalidAnswer):
                _repair_choices(response, ["Черновик."], ["Редактура."],
                                lambda _: self.fail("Формально неверное решение прошло проверку."))

    def test_repair_review_validates_required_terms_before_caching_and_publication(self):
        self.source.write_text("哈利已经回来了。", encoding="utf-8")
        glossary = self.directory / "names.json"
        glossary.write_text('{"哈利":{"ru":"Гарри","forms":[]}}', encoding="utf-8")
        base = (self.source, "--no-auto-glossary", "--edit", "--repair-review", "--glossary", glossary)
        unchanged = {"translations": [{"id": 1, "text": "Гарри вернулся."}]}
        invalid = {"choices": [{"id": 1, "version": "repaired", "text": "Он уже вернулся."}]}
        repaired = {"choices": [{"id": 1, "version": "repaired", "text": "Гарри уже вернулся."}]}
        with patch.object(FakeCodex, "complete", side_effect=[unchanged, unchanged, invalid, repaired]) as complete:
            _, log = self.invoke("translate", *base, "--max-calls=3", expected=3)
            self.assertIn("пропущена форма", log)
            published = self.directory / "Book.ru.txt"
            self.assertFalse(published.exists())
            self.invoke("translate", *base, "--max-calls=1")
            self.assertIn("Предыдущий ответ отклонён", complete.call_args.args[1])
        self.assertEqual(complete.call_count, 4)
        self.assertEqual(published.read_text(encoding="utf-8"), "Гарри уже вернулся.")

    def test_repair_review_cannot_remove_markup_from_published_ebook(self):
        source = self.directory / "source.fb2"
        source.write_text(
            '<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0">'
            '<body><section><p>哈利<emphasis>已经</emphasis>回来了。</p></section></body></FictionBook>',
            encoding="utf-8",
        )
        normal = FakeCodex.complete
        review_attempts = 0

        def respond(provider, model, instructions, data, schema):
            nonlocal review_attempts
            if "choices" not in schema["properties"]:
                return normal(provider, model, instructions, data, schema)
            review_attempts += 1
            edited = json.loads(data)["edited"][0]["text"]
            return {"choices": [{
                "version": "repaired", "id": 1,
                "text": "Гарри уже вернулся." if review_attempts == 1 else edited.replace("тихо", "неслышно"),
            }]}

        options = (source, "--no-auto-glossary", "--edit", "--repair-review")
        output = self.directory / "source.ru.fb2"
        with patch.object(FakeCodex, "complete", respond):
            _, log = self.invoke("translate", *options, "--max-calls=3", expected=3)
            self.assertIn("Нарушены метки разметки", log)
            self.assertFalse(output.exists())
            self.invoke("translate", *options, "--max-calls=1")
        result = etree.parse(output)
        self.assertEqual(len(result.xpath("//*[local-name()='emphasis']")), 1)
        self.assertIn("неслышно", "".join(result.getroot().itertext()))

    def test_mixed_review_choices_use_block_ids_and_carry_repaired_context_forward(self):
        self.source.write_text("哈利已经回来了。\n" * 65, encoding="utf-8")
        normal = FakeCodex.complete
        subsequent_context = []

        def respond(provider, model, instructions, data, schema):
            payload = json.loads(data)
            if "choices" in schema["properties"]:
                choices = [{"id": block["id"], "version": "edited", "text": ""}
                           for block in payload["blocks"]]
                if len(choices) == 64:
                    choices[61]["version"] = "draft"
                    choices[63].update(version="repaired", text="Гермиона вернулась к окну.")
                return {"choices": list(reversed(choices))}
            if "draft" in payload and len(payload["blocks"]) == 1:
                subsequent_context.append(payload["preceding"])
            return normal(provider, model, instructions, data, schema)

        with patch.object(FakeCodex, "complete", respond):
            self.invoke("translate", self.source, "--no-auto-glossary", "--edit", "--repair-review",
                        "--max-calls=6")
        lines = (self.directory / "Book.ru.txt").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 65)
        self.assertEqual(lines[-4:], ["Гермиона вошёл.", "Гермиона вошёл тихо.",
                                      "Гермиона вернулась к окну.", "Гермиона вошёл тихо."])
        self.assertEqual(len(subsequent_context), 1)
        self.assertTrue(subsequent_context[0].endswith("\n".join(lines[-4:-1])))

    def test_epub_line_break_is_visible_in_bilingual_sample(self):
        epub = self.directory / "source.epub"
        source = self.directory / "with-break.epub"
        write_epub(epub)
        with zipfile.ZipFile(epub) as original, zipfile.ZipFile(source, "w") as changed:
            for entry in original.infolist():
                data = original.read(entry)
                if entry.filename == "OPS/chapter1.xhtml":
                    data = ('<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Chapter 1</title></head>'
                            '<body><p id="opening">雨夜<br/><i>黎明</i></p>'
                            '<p>把⟦A1⟧放<i>这里</i>。</p></body></html>').encode()
                changed.writestr(entry, data)
        glossary = self.directory / "canon.json"
        glossary.write_text('{"雨夜黎明":{"ru":"Рассвет дождливой ночи"}}', encoding="utf-8")
        output, _ = self.invoke("sample", source, "--glossary", glossary, "--no-auto-glossary",
                                "--json-events", "--sample-pairs", "--sample-versions", "--edit")
        self.assertNotIn("required_terms", FakeCodex.calls[0][1])
        self.assertEqual(FakeCodex.calls[0][1]["glossary"], {})
        pairs = [event for event in map(json.loads, output.splitlines()) if event["event"] == "sample_pair"]
        pair = next(event for event in pairs if event["original"] == "雨夜\n黎明")
        for field in ("translation", "draft", "edited"):
            self.assertIn("\n", pair[field])
            self.assertNotRegex(pair[field], ANCHOR)
        literal = next(event for event in pairs if event["original"] == "把⟦A1⟧放这里。")
        for field in ("translation", "draft", "edited"):
            self.assertEqual(ANCHOR.findall(literal[field]), ["⟦A1⟧"])
        exported = (self.directory / "with-break.sample.ru.txt").read_text(encoding="utf-8")
        self.assertIn(f"雨夜\n黎明\n{pair['translation']}", exported)

    def test_txt_sample_keeps_literal_text_resembling_an_anchor(self):
        original = "雨⟦A1⟧夜。"
        self.source.write_text(original, encoding="utf-8")
        output, _ = self.invoke("sample", self.source, "--no-auto-glossary", "--json-events", "--sample-pairs")
        pair = next(event for event in map(json.loads, output.splitlines()) if event["event"] == "sample_pair")
        self.assertEqual(pair["original"], original)
        self.assertIn("⟦A1⟧", pair["translation"])
        result = (self.directory / "Book.sample.ru.txt").read_text(encoding="utf-8")
        self.assertTrue(result.startswith(original + "\n"))

    def test_long_block_does_not_break_markup_marker(self):
        text = "雨" * 399 + "⟦A1⟧" + "门" * 400
        parts = list(_split(text, 400))
        self.assertEqual("".join(parts), text)
        self.assertEqual(sum(part.count("⟦A1⟧") for part in parts), 1)
        self.assertTrue(all(0 < len(part) <= 400 for part in parts))

    def test_long_block_does_not_split_approved_name(self):
        for name in ("张三", "张⟦A1⟧三"):
            with self.subTest(name=name):
                text = "雨" * 399 + name + "门" * 400
                parts = list(_split(text, 400, ("张三",), markup_markers=frozenset(ANCHOR.findall(name))))
                self.assertEqual("".join(parts), text)
                self.assertTrue(parts[1].startswith(name))

    def test_long_block_only_copies_bounded_slices(self):
        copied = []

        class MeasuredString(str):
            def __getitem__(self, index):
                result = super().__getitem__(index)
                if isinstance(index, slice):
                    copied.append(len(result))
                return result

        text = MeasuredString("雨" * 1_000_000)
        parts = list(_split(text, 400))
        self.assertEqual("".join(parts), text)
        self.assertEqual(len(parts), 2_500)
        self.assertLessEqual(max(copied), 824)

    def test_fb2_style_and_link_text_are_protected(self):
        source = self.directory / "styled.fb2"
        source.write_text(
            '<FictionBook><body><section><p>他<style name="light">轻轻地</style>'
            '<a href="#note">按铃</a>。</p></section></body></FictionBook>', encoding="utf-8"
        )
        book = read_book(source)
        group = next(batches(book, 400))
        self.assertIn("轻轻地", group[0].text)
        self.assertIn("按铃", group[0].text)
        answer = group[0].text.replace("他", "Он").replace("轻轻地", "тихо").replace("按铃", "")
        with self.assertRaisesRegex(InvalidAnswer, "Пропущен текст ссылки"):
            _translations({"translations": [{"id": 1, "text": answer}]}, group)

    def test_xhtml_break_never_receives_model_generated_text(self):
        source = self.directory / "source.epub"
        write_epub(source)
        updated = self.directory / "with-break.epub"
        with zipfile.ZipFile(source) as original, zipfile.ZipFile(updated, "w") as changed:
            for entry in original.infolist():
                data = original.read(entry)
                if entry.filename == "OPS/chapter1.xhtml":
                    data = data.replace(b"</i>", b"</i><br/>")
                changed.writestr(entry, data)
        self.invoke("translate", updated, "--no-auto-glossary")
        with zipfile.ZipFile(self.directory / "with-break.ru.epub") as result:
            chapter = etree.fromstring(result.read("OPS/chapter1.xhtml"))
            self.assertIsNone(chapter.find(".//{*}br").text)

    def test_rejects_omitted_clauses_and_copied_chinese(self):
        source = "他终于到了城里，找了很久也没有找到朋友，只好独自乘船回家。"
        self.source.write_text(source, encoding="utf-8")
        group = next(batches(read_book(self.source), 400))
        for answer in ("Он приехал.", source + "!", "Он приехал. " + source[3:]):
            with self.subTest(answer=answer), self.assertRaises(InvalidAnswer):
                _translations({"translations": [{"id": 1, "text": answer}]}, group)

    def test_wrong_language_prose_and_punctuated_copies_are_rejected(self):
        self.source.write_text("她推开房门，雨水沿着衣袖滴在地板上。", encoding="utf-8")
        group = next(batches(read_book(self.source), 400))
        for answer in ("She pushed the door open and rain dripped from her sleeve.",
                       "Она тут. She pushed the door open and rain dripped from her sleeve.",
                       "Она推开房门，雨水沿着衣袖，滴在地板上。"):
            with self.subTest(answer=answer), self.assertRaises(InvalidAnswer):
                _translations({"translations": [{"id": 1, "text": answer}]}, group)
        answer = "Она распахнула дверь. С рукава на пол капала вода."
        self.assertEqual(_translations({"translations": [{"id": 1, "text": answer}]}, group), [answer])
        self.source.write_text("你为什么还站在这里？", encoding="utf-8")
        group = next(batches(read_book(self.source), 400))
        with self.assertRaisesRegex(InvalidAnswer, "русский текст"):
            _translations({"translations": [{"id": 1, "text": "Why are you still standing here?"}]}, group)

    def test_numeric_date_and_explicit_unchanged_name_pass_validation(self):
        self.source.write_text("二〇二六年十一月二十日下午三点\n小明\n", encoding="utf-8")
        group = next(batches(read_book(self.source), 400))
        output = ["20.11.2026, 15:00", "小明"]
        response = {"translations": [{"id": index, "text": text} for index, text in enumerate(output, 1)]}
        self.assertEqual(_translations(response, group, {"小明": "小明"}), output)
        response["translations"][0]["text"] = "01.01.2025, 09:00"
        with self.assertRaises(InvalidAnswer):
            _translations(response, group, {"小明": "小明"})

    def test_numeric_validation_preserves_integer_date_and_time_values(self):
        for source, correct, wrong in (
            ("一千零二", "1 002", "1020"),
            ("十五", "15", "1,5"),
            ("一百二十", "120", "1,20"),
            ("三月四日", "04.03", "03.04"),
            ("三月十五日", "15.03", "15,3"),
            ("三点五分", "03:05", "3,5"),
            ("二〇二六年", "2026", "-2026"),
            ("二〇二六年十一月二十日", "2026-11-20", "2026-11-21"),
            ("下午三点半", "15:30", "03:30"),
        ):
            with self.subTest(source=source):
                self.assertTrue(equivalent_numeric(source, correct))
                self.assertFalse(equivalent_numeric(source, wrong))

    def test_automatic_glossary_rejects_han_only_but_preserves_approved_entries(self):
        response = {"entries": [{"zh": "小明", "ru": "小明"}]}
        with self.assertRaises(InvalidAnswer):
            _entries(response, ["小明推开了门。"], {})
        self.assertEqual(_entries(response, ["小明推开了门。"], {"小明": "小明"}), {})
        response["entries"][0]["ru"] = "Xiaoming"
        self.assertEqual(_entries(response, ["小明推开了门。"], {}), {"小明": "Xiaoming"})

    def test_automatic_glossary_requests_correction_for_conflicting_forms(self):
        entries = {"entries": [{"zh": "小明", "ru": "Сяомин"}, {"zh": "小明", "ru": "Сяо Мин"}]}
        with self.assertRaisesRegex(InvalidAnswer, "разные переводы"):
            _entries(entries, ["小明来了。"], {})

    def test_automatic_glossary_preserves_first_typographically_equivalent_form(self):
        entries = {"entries": [{"zh": "赫敏", "ru": "Гермиона Грейнджер"},
                               {"zh": "赫敏", "ru": "Гермиона\u00a0Греи\u0306нджер"}]}
        self.assertEqual(_entries(entries, ["赫敏到了。"], {}), {"赫敏": "Гермиона Грейнджер"})

    def test_optional_glossary_phrase_cannot_override_required_name(self):
        source = "妙丽小姐来了。妙丽老师昨天也来了。"
        name = {"ru": "Гермиона", "forms": []}
        strict = {"妙丽": name}
        entries = {"entries": [{"zh": "妙丽小姐", "ru": "мисс Мэри"},
                               {"zh": "妙丽老师", "ru": "преподаватель Гермиона"}]}
        self.assertEqual(_entries(entries, [source], strict), {"妙丽老师": "преподаватель Гермиона"})

        self.source.write_text("妙丽小姐来了。", encoding="utf-8")
        group = next(batches(read_book(self.source), 400))
        advisory = {"妙丽": "Гермиона", "妙丽小姐": "мисс Мэри"}
        response = {"translations": [{"id": 1, "text": "Мисс Мэри пришла."}]}
        with self.assertRaisesRegex(InvalidAnswer, "Гермиона"):
            _translations(response, group, glossary=advisory, strict=strict)
        official_full_name = {**strict, "妙丽小姐": {"ru": "Мисс Мэри", "forms": []}}
        self.assertEqual(_translations(response, group, glossary=advisory, strict=official_full_name),
                         ["Мисс Мэри пришла."])

    def test_glossary_rejects_a_spelling_owned_by_two_entries(self):
        for content in ('{"赫敏":{"ru":"Гермиона","aliases":["妙丽"]},"妙丽":"Другое имя"}',
                        '{"赫敏":{"ru":"Гермиона","aliases":[" 妙丽","妙丽"]}}',
                        '{"赫敏":{"ru":"Гермиона","aliases":["赫敏"]}}'):
            with self.subTest(content=content), self.assertRaisesRegex(ValueError, "Написание.*повторяется"):
                parse_glossary(content)

    def test_xhtml_named_entities_round_trip_without_external_loading(self):
        source = self.directory / "source.epub"
        write_epub(source)
        updated = self.directory / "entities.epub"
        doctype = b'<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" "https://invalid.example/xhtml.dtd">'
        with zipfile.ZipFile(source) as original, zipfile.ZipFile(updated, "w") as changed:
            for entry in original.infolist():
                data = original.read(entry)
                if entry.filename == "OPS/chapter1.xhtml":
                    data = doctype + data.replace(b'id="opening">', b'id="opening" title="A&nbsp;B">')
                    data = data.replace(b"Already in English.", b"Already&nbsp;<b>in English</b>.")
                changed.writestr(entry, data)
        self.invoke("translate", updated, "--no-auto-glossary")
        with zipfile.ZipFile(self.directory / "entities.ru.epub") as result:
            chapter = etree.fromstring(result.read("OPS/chapter1.xhtml"))
            first, second = chapter.findall(".//{*}p")
            self.assertEqual(first.get("title"), "A\xa0B")
            self.assertEqual(second.text, "Already\xa0")
            self.assertEqual(second.find("{*}b").text, "in English")

    def test_xhtml_unknown_doctype_does_not_read_local_dtd(self):
        dtd = self.directory / "local.dtd"
        dtd.write_text('<!ENTITY local "This must never be translated">', encoding="utf-8")
        xml = f'<!DOCTYPE html SYSTEM "{dtd.as_uri()}"><html>&local;</html>'.encode()
        with self.assertRaisesRegex(etree.XMLSyntaxError, "Entity 'local' not defined"):
            _xml(xml, xhtml=True)

    def test_epub_context_crosses_spine_but_does_not_reach_navigation(self):
        source = self.directory / "source.epub"
        write_epub(source)
        updated = self.directory / "chapters.epub"
        with zipfile.ZipFile(source) as original, zipfile.ZipFile(updated, "w") as changed:
            for entry in original.infolist():
                data = original.read(entry)
                if entry.filename in ("OPS/chapter1.xhtml", "OPS/chapter 2.xhtml"):
                    first_chapter = entry.filename == "OPS/chapter1.xhtml"
                    paragraph = "<p>哈利到了。</p>" if first_chapter else "<p>赫敏进来了。</p>"
                    repetitions = 63 if first_chapter else 64
                    data = ("<html><body>" + paragraph * repetitions + "</body></html>").encode()
                elif entry.filename == "OPS/book.opf":
                    data = data.replace(b'<item id="nav"', b'<item id="notes" href="notes.xhtml" '
                                        b'media-type="application/xhtml+xml"/><item id="nav"')
                    data = data.replace(b'<itemref idref="second"/>', b'<itemref idref="notes" linear="no"/>'
                                        b'<itemref idref="second"/>')
                changed.writestr(entry, data)
            changed.writestr("OPS/notes.xhtml", ("<html><body>" + "<p>这是脚注。</p>" * 64 + "</body></html>").encode())
        canon = self.directory / "canon.json"
        canon.write_text('{"哈利":"Гарри"}', encoding="utf-8")
        self.invoke("translate", updated, "--no-auto-glossary", "--glossary", canon)
        first, notes, second, nav = [payload for _, payload in FakeCodex.calls]
        self.assertEqual(first["preceding"], "")
        self.assertEqual(notes["preceding"], "")
        self.assertIn("Гарри вошёл.", second["preceding"])
        self.assertNotIn("Гермиона", second["preceding"])
        self.assertEqual(nav["preceding"], "")

    def test_large_fb2_binary_is_kept_intact(self):
        source = self.directory / "illustrated.fb2"
        image = b"test image fixture" * 450_000
        encoded = base64.b64encode(image)
        self.assertGreater(len(encoded), 10_000_000)
        source.write_bytes(
            "<FictionBook><body><section><p>哈利来了。</p></section></body><binary id='cover'>".encode()
            + encoded + b"</binary></FictionBook>"
        )
        self.invoke("translate", source, "--no-auto-glossary")
        result = etree.parse(self.directory / "illustrated.ru.fb2", etree.XMLParser(huge_tree=True))
        self.assertEqual(base64.b64decode(result.find(".//{*}binary").text), image)

    def test_serialization_error_keeps_glossary_unpublished(self):
        self.source.write_text("哈利来了。\n", encoding="utf-8")
        book = read_book(self.source)
        glossary = self.directory / "out.json"
        args = ("translate", self.source, "--save-glossary", glossary)
        with patch.object(book, "write", side_effect=OSError("Ошибка записи")), patch(
            "ai_translator.cli.read_book", return_value=book
        ):
            self.invoke(*args, expected=1)
        self.assertFalse(glossary.exists())
        _, log = self.invoke(*args)
        self.assertIn("Ответов из кеша: 2", log)

    def test_retries_response_with_lost_inline_markers(self):
        source = self.directory / "book.fb2"
        write_fb2(source)
        valid_response = FakeCodex.complete
        attempts = []

        def complete(backend, model, instructions, data, schema):
            response = valid_response(backend, model, instructions, data, schema)
            attempts.append(instructions)
            if len(attempts) == 1:
                for item in response["translations"]:
                    item["text"] = ANCHOR.sub("", item["text"])
            return response

        with patch.object(FakeCodex, "complete", complete):
            self.invoke("translate", source, "--no-auto-glossary")
        self.assertEqual(len(attempts), 2)
        self.assertIn("Нарушены метки разметки", attempts[-1])

    def test_openai_api_uses_its_optional_key_and_schema(self):
        settings = load_config(None, "openai-api")
        schema = {"type": "object"}
        response = {"status": "completed", "output": [{"type": "message", "status": "completed", "content": [
            {"type": "output_text", "text": '{"result":"по-русски"}'},
        ]}]}
        stub = io.BytesIO(json.dumps(response).encode())
        with patch.dict("os.environ", {"OPENAI_API_KEY": "example"}), patch(
            "ai_translator.provider._api_open", return_value=stub
        ) as urlopen:
            backend = OpenAIAPI(90)
            backend.check()
            result = backend.complete(settings.models["translate"], "Instruction", "{}", schema)
            request = urlopen.call_args.args[0]
            data = json.loads(request.data)
            self.assertEqual(request.get_header("Authorization"), "Bearer example")
            self.assertEqual(data["text"]["format"]["schema"], schema)
            self.assertEqual(data["reasoning"], {"effort": "low"})
            self.assertEqual(result, {"result": "по-русски"})

    def test_openai_api_reports_failures_without_exposing_key(self):
        settings = load_config(None, "openai-api")
        backend = OpenAIAPI(1)
        model = settings.models["translate"]
        secret = "private-test-key"
        with patch.dict("os.environ", {"OPENAI_API_KEY": secret}), patch(
            "ai_translator.provider._api_open"
        ) as urlopen:
            for status in (401, 403, 429, 503):
                body = io.BytesIO(json.dumps({"error": {"message": f"Rejected {secret}"}}).encode())
                urlopen.side_effect = HTTPError("https://example.invalid", status, "", {}, body)
                with self.subTest(status=status), self.assertRaises(
                    AuthenticationError if status in (401, 403) else ProviderError
                ) as raised:
                    backend.complete(model, "Test", "{}", {})
                self.assertNotIn(secret, str(raised.exception))
                self.assertIn(str(status), str(raised.exception))
            urlopen.side_effect = URLError("timeout")
            with self.assertRaisesRegex(ProviderError, "недоступен"):
                backend.complete(model, "Test", "{}", {})

    def test_txt_bom_layout_and_final_line_without_newline(self):
        original = "Keep this ASCII.\r\n  哈利来了。\r\n\r\nLast line."
        for encoding, bom in (
            ("utf-8", b""), ("utf-8", b"\xef\xbb\xbf"),
            ("utf-16-le", b"\xff\xfe"), ("utf-16-be", b"\xfe\xff"),
            ("utf-32-le", b"\xff\xfe\x00\x00"), ("utf-32-be", b"\x00\x00\xfe\xff"),
            ("gb18030", b""),
        ):
            with self.subTest(encoding=encoding, bom=bom):
                self.source.write_bytes(bom + original.encode(encoding))
                book = read_book(self.source)
                book.sections[0][0].replace("Гарри пришёл.")
                output = self.directory / "out.txt"
                book.write(output)
                self.assertEqual(output.read_bytes(), original.replace("哈利来了。", "Гарри пришёл.").encode())

    def test_claude_code_uses_local_login_and_structured_print_mode(self):
        self.source.write_text("哈利来了。", encoding="utf-8")
        config = self.directory / "claude.toml"
        config.write_text('[translate]\nmodel = "sonnet"\nreasoning = "high"\n', encoding="utf-8")
        auth = {"loggedIn": True, "authMethod": "claude.ai"}
        response = {"type": "result", "subtype": "success", "is_error": False,
                    "structured_output": {"translations": [{"id": 1, "text": "Гарри пришёл."}]},
                    "usage": {"input_tokens": 10, "cache_read_input_tokens": 3, "output_tokens": 5}}
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "ignored"}), patch(
            "ai_translator.provider.shutil.which", return_value="/fake/claude"
        ), patch("ai_translator.provider._run_cli", side_effect=[
            subprocess.CompletedProcess([], 0, json.dumps(auth), ""),
            subprocess.CompletedProcess([], 0, json.dumps(response), ""),
        ]) as run:
            _, log = self.invoke("translate", self.source, "--provider", "claude-code", "--config", config,
                                 "--no-auto-glossary")
            call = run.call_args
            command = call.args[0]
            self.assertEqual(command[command.index("--model") + 1], "sonnet")
            self.assertEqual(command[command.index("--effort") + 1], "high")
            self.assertEqual(command[command.index("--tools") + 1], "")
            self.assertIn("--json-schema", command)
            self.assertNotIn("ANTHROPIC_API_KEY", call.kwargs["env"])
            self.assertIn("哈利来了。", call.kwargs["input"])
            self.assertIn("13 на входе", log)
        self.assertEqual((self.directory / "Book.ru.txt").read_text(), "Гарри пришёл.")

    def test_claude_rejects_console_authentication(self):
        self.source.write_text("哈利来了。", encoding="utf-8")
        with patch("ai_translator.provider.shutil.which", return_value="/fake/claude"), patch(
            "ai_translator.provider._run_cli",
            return_value=subprocess.CompletedProcess([], 0, '{"loggedIn":true,"authMethod":"api_key"}', ""),
        ) as run:
            _, log = self.invoke("translate", self.source, "--provider", "claude-code",
                                 "--no-auto-glossary", expected=4)
            self.assertIn("claude auth login", log)
            self.assertEqual(run.call_count, 1)

    def test_claude_doctor_distinguishes_sign_out_from_a_cli_failure(self):
        with patch("ai_translator.provider.shutil.which", return_value="/fake/claude"), patch(
            "ai_translator.provider._run_cli"
        ) as run:
            args = ("doctor", "--provider", "claude-code", "--json-events")
            for code, stdout, stderr, expected, kind in (
                (1, '{"loggedIn":false,"authMethod":"none"}', "", 4, "authentication"),
                (1, "", "internal error: private detail", 1, "error"),
                (0, '{"loggedIn":true}', "", 1, "error"),
                (0, '{"loggedIn":true,"authMethod":"api_key"}', "", 4, "authentication"),
                (0, '{"loggedIn":true,"authMethod":"claude.ai"}', "", 0, "authentication_ok"),
            ):
                with self.subTest(code=code, stdout=stdout):
                    run.return_value = subprocess.CompletedProcess([], code, stdout, stderr)
                    output, _ = self.invoke(*args, expected=expected)
                    self.assertEqual(json.loads(output)["event"], kind)
                    self.assertNotIn("private detail", output)

    def test_force_rollback_keeps_book_and_glossary_in_sync(self):
        self.source.write_text("哈利来了。", encoding="utf-8")
        book = self.directory / "Book.ru.txt"
        glossary = self.directory / "names.json"
        book.write_text("Старая книга", encoding="utf-8")
        glossary.write_text('{"哈利":"Старая форма"}', encoding="utf-8")
        replace = Path.replace

        def fail_second_publish(source, destination):
            if destination == glossary:
                raise OSError("Сбой записи глоссария")
            return replace(source, destination)

        with patch.object(Path, "replace", fail_second_publish):
            self.invoke("translate", self.source, "--save-glossary", glossary, "--force", expected=1)
        self.assertEqual(book.read_text(), "Старая книга")
        self.assertEqual(glossary.read_text(), '{"哈利":"Старая форма"}')
        self.assertEqual(list(self.directory.glob(".ai-translator-original-*")), [])

    def test_output_cannot_overwrite_a_recovery_marker(self):
        self.source.write_text("哈利来了。", encoding="utf-8")
        for marker in (".Book.ru.txt.ai-translator-publish.json", ".Book.ru.txt.AI-TRANSLATOR-PUBLISH.JSON"):
            with self.subTest(marker=marker):
                glossary = self.directory / marker
                _, log = self.invoke("translate", self.source, "--save-glossary", glossary, expected=1)
                self.assertIn("зарезервировано", log)
                self.assertFalse(FakeCodex.calls)
                self.assertFalse(glossary.exists())

    def test_approved_book_overwrite_does_not_replace_a_late_glossary(self):
        self.source.write_text("哈利来了。", encoding="utf-8")
        output = self.directory / "Book.ru.txt"
        output.write_text("Предыдущий перевод", encoding="utf-8")
        glossary = self.directory / "new.json"
        complete = FakeCodex.complete

        def create_glossary_during_translation(backend, *args):
            glossary.write_text("Чужой файл", encoding="utf-8")
            return complete(backend, *args)

        with patch.object(FakeCodex, "complete", create_glossary_during_translation):
            self.invoke("translate", self.source, "--no-auto-glossary", "--save-glossary", glossary,
                        "--replace-existing", output, expected=1)
        self.assertEqual(output.read_text(), "Предыдущий перевод")
        self.assertEqual(glossary.read_text(), "Чужой файл")

    def test_state_lock_refuses_second_client_before_model_calls(self):
        self.source.write_text("哈利来了。", encoding="utf-8")
        state = self.directory / "book.sqlite3"
        with open_state(state):
            _, log = self.invoke("translate", self.source, "--state", state, expected=1)
        self.assertIn("Кеш уже используется", log)
        self.assertFalse(FakeCodex.calls)
        self.invoke("translate", self.source, "--state", state)

    def test_state_lock_refuses_symlink_alias(self):
        state = self.directory / "book.sqlite3"
        alias = self.directory / "alias.sqlite3"
        with open_state(state):
            try:
                alias.symlink_to(state)
            except OSError as exc:
                self.skipTest(str(exc))
            with self.assertRaisesRegex(ValueError, "Кеш уже используется"):
                with open_state(alias):
                    pass

    def _make_long_output_path(self, character):
        count = 251 if os.name == "nt" else 83
        path = self.directory / (character * count + ".txt")
        try:
            path.touch()
        except OSError as exc:
            if path_too_long(path):
                self.skipTest(str(exc))
            raise
        if not path_too_long(path.with_name(f".{path.name}.ai-translator-publish.lock")):
            self.skipTest("Файловая система допускает длинное служебное имя.")
        return path

    def test_long_title_translates_and_resumes_with_the_same_cache(self):
        source = self._make_long_output_path("雨")
        source.write_text("哈利来了。", encoding="utf-8")
        directory = source.parent / CACHE_DIRECTORY
        state = default_state_path(source)
        output = default_output(source, "translate")
        self.assertEqual(state.parent, directory / source.name)
        self.assertTrue(output.name.endswith(".ru.txt"))
        self.assertFalse(path_too_long(output))

        self.invoke("inspect", source, "--no-auto-glossary")
        self.assertFalse(directory.exists())
        self.invoke("translate", source, "--no-auto-glossary", "--max-total-calls", "1")
        self.assertTrue(output.is_file())
        self.assertTrue(state.is_file())
        explicit = self._make_long_output_path("雪")
        explicit.unlink()
        _, log = self.invoke("translate", source, "--no-auto-glossary", "--max-calls", "0", "-o", explicit)
        self.assertEqual(explicit.read_bytes(), output.read_bytes())
        self.assertIn("Ответов из кеша: 1", log)
        self.assertEqual(len(FakeCodex.calls), 1)
        self.assertTrue((self.directory / PUBLICATION_DIRECTORY / explicit.name / "publish.lock").is_file())

    def test_long_legacy_cache_migrates_sqlite_answers_and_budget_without_new_requests(self):
        source = self.directory / ("雨" * (225 if os.name == "nt" else 74) + ".txt")
        legacy = source.with_name(f".{source.name}.ai-translator.sqlite3")
        if path_too_long(legacy) or not path_too_long(legacy.with_name(legacy.name + "-journal")):
            self.skipTest("Лимит файловой системы не подходит для этой фикстуры.")
        for original in (source, self.source):
            original.write_text("哈利来了。", encoding="utf-8")
        self.invoke("translate", self.source, "--no-auto-glossary", "--max-total-calls", "1")
        original_cache = default_state_path(self.source).read_bytes()
        legacy.touch()
        bounded = default_state_path(source)
        self.assertEqual(bounded.parent, source.parent / CACHE_DIRECTORY / source.name)
        output, _ = self.invoke("inspect", source, "--no-auto-glossary", "--json-events")
        self.assertIn('"reusable_answers": 0', output)
        legacy.write_bytes(original_cache)
        output, _ = self.invoke("inspect", source, "--no-auto-glossary", "--json-events")
        self.assertIn('"reusable_answers": 1, "complete": true', output)
        self.assertFalse(bounded.parent.exists())

        _, log = self.invoke("translate", source, "--no-auto-glossary", "--max-calls", "0")
        self.assertIn("Ответов из кеша: 1", log)
        self.assertEqual(len(FakeCodex.calls), 1)
        self.assertEqual(legacy.read_bytes(), original_cache)
        with open_state(bounded) as connection:
            self.assertEqual(connection.execute("SELECT attempts, ceiling FROM request_budget").fetchone(), (1, 1))
        legacy.write_bytes(b"stale broken cache")
        with patch("ai_translator.pipeline.path_too_long", return_value=False):
            self.assertEqual(default_state_path(source), bounded)
        output, _ = self.invoke("inspect", source, "--no-auto-glossary", "--json-events")
        self.assertIn('"reusable_answers": 1, "complete": true', output)

    def _interrupt_publication(self, book, glossary, after_replacement):
        for path in (book, glossary):
            path.write_text("old", encoding="utf-8")
        worker = """
import os
import sys
from pathlib import Path
from ai_translator.publication import write_outputs
book, glossary, interrupted = map(Path, sys.argv[1:])
replace = Path.replace
def crash(source, destination):
    result = replace(source, destination)
    if destination == interrupted:
        os._exit(92)
    return result
Path.replace = crash
write_outputs([(path, lambda out: out.write_text("new")) for path in (book, glossary)], True)
"""
        crashed = subprocess.run([sys.executable, "-c", worker, str(book), str(glossary), str(after_replacement)],
                                 check=False, timeout=10)
        self.assertEqual(crashed.returncode, 92)

    def test_publication_recovers_process_death_between_replacements(self):
        book = self.directory / "book.ru.txt"
        glossary = self.directory / "names.json"
        self._interrupt_publication(book, glossary, book)
        self.assertEqual(book.read_text(), "new")
        with self.assertRaisesRegex(ValueError, "со всеми выходными файлами"):
            recover_outputs([glossary])

        journal = next(self.directory.glob(".ai-translator-transaction-*.json"))
        original_record = journal.read_text(encoding="utf-8")
        committed = json.loads(original_record)
        committed["committed"] = True
        journal.write_text(json.dumps(committed), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "не соответствуют завершённой публикации"):
            recover_outputs([book, glossary])
        self.assertEqual(book.read_text(encoding="utf-8"), "new")
        self.assertEqual(glossary.read_text(encoding="utf-8"), "old")
        self.assertTrue(Path(committed["entries"][0]["backup"]).is_file())

        other_job = self.directory / ".ai-translator-stage-other-job"
        other_job.write_text("other job", encoding="utf-8")
        record = json.loads(original_record)
        record["entries"][1]["staged"] = str(other_job)
        journal.write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Повреждён журнал"):
            recover_outputs([book, glossary])
        self.assertEqual(other_job.read_text(encoding="utf-8"), "other job")
        self.assertEqual(book.read_text(encoding="utf-8"), "new")
        self.assertEqual(glossary.read_text(encoding="utf-8"), "old")
        legacy = json.loads(original_record)
        legacy.pop("version")
        for entry in legacy["entries"]:
            entry.pop("backup_digest")
        journal.write_text(json.dumps(legacy), encoding="utf-8")

        recover_outputs([book, glossary])
        self.assertEqual(book.read_text(), "old")
        self.assertEqual(glossary.read_text(), "old")
        self.assertEqual(list(self.directory.glob(".*.ai-translator-publish.json")), [])

    def test_publication_recovers_long_output_names_after_interruption(self):
        book = self._make_long_output_path("雪")
        glossary = self.directory / "names.json"
        self._interrupt_publication(book, glossary, book)
        marker = self.directory / PUBLICATION_DIRECTORY / book.name / "publish.json"
        self.assertTrue(marker.is_file())
        self.assertEqual(book.read_text(encoding="utf-8"), "new")
        recover_outputs([book, glossary])
        self.assertEqual([path.read_text(encoding="utf-8") for path in (book, glossary)], ["old", "old"])
        self.assertFalse(marker.exists())

    def test_publication_checks_all_backup_files_before_rolling_back(self):
        book = self.directory / "book.ru.txt"
        glossary = self.directory / "names.json"
        self._interrupt_publication(book, glossary, glossary)
        lost_backup = next(self.directory.glob(".ai-translator-original-*-0"))
        other_backup = next(self.directory.glob(".ai-translator-original-*-1"))
        original = lost_backup.read_bytes()
        lost_backup.unlink()
        with self.assertRaisesRegex(ValueError, "требуется резервная копия"):
            recover_outputs([book, glossary])
        self.assertEqual([path.read_text(encoding="utf-8") for path in (book, glossary)], ["new", "new"])
        self.assertEqual(other_backup.read_text(encoding="utf-8"), "old")

        lost_backup.write_bytes(b"bad")
        with self.assertRaisesRegex(ValueError, "Повреждена резервная копия"):
            recover_outputs([book, glossary])
        self.assertEqual([path.read_text(encoding="utf-8") for path in (book, glossary)], ["new", "new"])
        self.assertEqual(other_backup.read_text(encoding="utf-8"), "old")

        lost_backup.write_bytes(original)
        recover_outputs([book, glossary])
        self.assertEqual([path.read_text(encoding="utf-8") for path in (book, glossary)], ["old", "old"])

    def test_publication_recovery_resumes_after_interrupted_rollback(self):
        book = self.directory / "book.ru.txt"
        glossary = self.directory / "names.json"
        self._interrupt_publication(book, glossary, glossary)
        worker = """
import os
import sys
from pathlib import Path
from ai_translator.publication import recover_outputs
book, glossary = map(Path, sys.argv[1:])
replace = Path.replace
def crash(source, destination):
    result = replace(source, destination)
    if destination == glossary:
        os._exit(94)
    return result
Path.replace = crash
recover_outputs([book, glossary])
"""
        crashed = subprocess.run([sys.executable, "-c", worker, str(book), str(glossary)], check=False, timeout=10)
        self.assertEqual(crashed.returncode, 94)
        self.assertEqual([path.read_text(encoding="utf-8") for path in (book, glossary)], ["new", "old"])
        recover_outputs([book, glossary])
        self.assertEqual([path.read_text(encoding="utf-8") for path in (book, glossary)], ["old", "old"])
        self.assertEqual(list(self.directory.glob(".ai-translator-*")), [])

    def test_publication_rollback_restores_a_symlink_without_hashing_its_target(self):
        target = self.directory / "original.txt"
        target.write_text("original", encoding="utf-8")
        book = self.directory / "book.ru.txt"
        glossary = self.directory / "names.json"
        try:
            book.symlink_to(target.name)
        except OSError as exc:
            self.skipTest(str(exc))
        self._interrupt_publication(book, glossary, glossary)
        target.write_text("edited separately", encoding="utf-8")
        recover_outputs([book, glossary])
        self.assertTrue(book.is_symlink())
        self.assertEqual(book.readlink(), Path(target.name))
        self.assertEqual(book.read_text(encoding="utf-8"), "edited separately")
        self.assertEqual(glossary.read_text(encoding="utf-8"), "old")

    def test_invalid_publication_journal_fails_before_provider_work(self):
        self.source.write_text("哈利来了。", encoding="utf-8")
        book = self.directory / "Book.ru.txt"
        glossary = self.directory / "names.json"
        for path in (book, glossary):
            path.write_text("old", encoding="utf-8")
        journal = self.directory / f".ai-translator-transaction-{'0' * 32}.json"
        journal.write_text('{"committed":false}', encoding="utf-8")
        marker = self.directory / f".{book.name}.ai-translator-publish.json"
        marker.write_text(str(journal), encoding="utf-8")
        output, _ = self.invoke("translate", self.source, "--save-glossary", glossary, "--force",
                                "--json-events", expected=1)
        failure = json.loads(output)
        self.assertEqual(failure["event"], "error")
        self.assertIn("Повреждён журнал", failure["message"])
        self.assertFalse(FakeCodex.calls)
        self.assertEqual([path.read_text(encoding="utf-8") for path in (book, glossary)], ["old", "old"])
        self.assertTrue(marker.is_file())

        marker.write_text("relative.json", encoding="utf-8")
        _, log = self.invoke("translate", self.source, "--save-glossary", glossary, "--force", expected=1)
        self.assertIn("Повреждён указатель журнала", log)
        self.assertIn(str(marker), log)
        self.assertFalse(FakeCodex.calls)

    def test_publication_removes_temporary_metadata_on_flush_failure(self):
        book = self.directory / "book.ru.txt"
        glossary = self.directory / "names.json"
        for path in (book, glossary):
            path.write_text("old", encoding="utf-8")
        writers = [(path, lambda dest: dest.write_text("new", encoding="utf-8")) for path in (book, glossary)]
        with patch("ai_translator.publication.os.fsync", side_effect=OSError("fault")):
            with self.assertRaisesRegex(OSError, "fault"):
                write_outputs(writers, True)
        self.assertEqual([path.read_text(encoding="utf-8") for path in (book, glossary)], ["old", "old"])
        self.assertEqual(list(self.directory.glob(".ai-translator-*")), [])

    def test_publication_syncs_data_and_output_directories_before_commit(self):
        book = self.directory / "book.ru.txt"
        other = self.directory / "other"
        other.mkdir()
        glossary = other / "names.json"
        paths = (book, glossary)
        for path in paths:
            path.write_text("old", encoding="utf-8")
        events = []
        sync_file = publication._sync_prepared_file
        sync_directory = publication._sync_directory
        atomic_text = publication._atomic_text
        replace = Path.replace

        def observe_file(path):
            sync_file(path)
            events.append(("data", path.parent))

        def observe_directory(path):
            sync_directory(path)
            events.append(("directory", path))

        def observe_commit(path, content):
            if path.name.startswith(".ai-translator-transaction-") and json.loads(content)["committed"]:
                events.append(("commit", None))
            atomic_text(path, content)

        def observe_replace(path, target):
            result = replace(path, target)
            if target in paths:
                events.append(("replace", target))
            return result

        with patch.object(publication, "_sync_prepared_file", observe_file), patch.object(
            publication, "_sync_directory", observe_directory,
        ), patch.object(publication, "_atomic_text", observe_commit), patch.object(Path, "replace", observe_replace):
            write_outputs([(path, lambda dest: dest.write_text("new", encoding="utf-8")) for path in paths], True)

        first_replace = events.index(("replace", book))
        last_replace = events.index(("replace", glossary))
        commit = events.index(("commit", None))
        for path in paths:
            self.assertIn(("data", path.parent), events[:first_replace])
            self.assertIn(("directory", path.parent), events[last_replace + 1:commit])

    def test_publication_failed_commit_sync_keeps_recovery_metadata(self):
        book = self.directory / "book.ru.txt"
        glossary = self.directory / "names.json"
        paths = (book, glossary)
        for path in paths:
            path.write_text("old", encoding="utf-8")
        sync_directory = publication._sync_directory

        def fail_commit_sync(path):
            for journal in self.directory.glob(".ai-translator-transaction-*.json"):
                if json.loads(journal.read_text(encoding="utf-8"))["committed"]:
                    raise OSError("ошибка синхронизации фиксации")
            sync_directory(path)

        with patch.object(publication, "_sync_directory", fail_commit_sync):
            with self.assertRaisesRegex(OSError, "ошибка синхронизации фиксации"):
                write_outputs([(path, lambda dest: dest.write_text("new", encoding="utf-8")) for path in paths], True)
        self.assertEqual(len(list(self.directory.glob(".ai-translator-original-*"))), 2)
        self.assertEqual(len(list(self.directory.glob(".*.ai-translator-publish.json"))), 2)
        self.assertEqual([path.read_text(encoding="utf-8") for path in paths], ["new", "new"])
        recover_outputs(list(paths))
        self.assertEqual(list(self.directory.glob(".ai-translator-*")), [])
        self.assertEqual([path.read_text(encoding="utf-8") for path in paths], ["new", "new"])

    def test_publication_failed_rollback_sync_keeps_backups_across_retries(self):
        book = self.directory / "book.ru.txt"
        other = self.directory / "other"
        other.mkdir()
        glossary = other / "names.json"
        paths = (book, glossary)
        self._interrupt_publication(book, glossary, glossary)
        sync_directory = publication._sync_directory

        def fail_rollback_sync(path):
            if path == other:
                raise OSError("ошибка синхронизации отката")
            sync_directory(path)

        with patch.object(publication, "_sync_directory", fail_rollback_sync), patch.object(
            publication.os, "link", side_effect=OSError("жёсткие ссылки недоступны"),
        ):
            for _ in range(2):
                with self.assertRaisesRegex(OSError, "ошибка синхронизации отката"):
                    recover_outputs(list(paths))
                self.assertEqual([path.read_text(encoding="utf-8") for path in paths], ["old", "old"])
                self.assertTrue(all(list(path.parent.glob(".ai-translator-original-*")) for path in paths))
        recover_outputs(list(paths))
        self.assertTrue(all(not list(path.parent.glob(".ai-translator-*")) for path in paths))
        self.assertEqual([path.read_text(encoding="utf-8") for path in paths], ["old", "old"])

    def test_publication_recovers_process_death_while_preparing_glossary(self):
        book = self.directory / "book.ru.txt"
        glossary = self.directory / "names.json"
        for path in (book, glossary):
            path.write_text("old", encoding="utf-8")
        worker = """
import os
import sys
from pathlib import Path
from ai_translator.publication import write_outputs
book, glossary = map(Path, sys.argv[1:])
def write_partial(path):
    path.write_text("partial")
    os._exit(93)
write_outputs([(book, lambda path: path.write_text("new")), (glossary, write_partial)], True)
"""
        crashed = subprocess.run([sys.executable, "-c", worker, str(book), str(glossary)],
                                 check=False, timeout=10)
        self.assertEqual(crashed.returncode, 93)
        recover_outputs([book, glossary])
        self.assertEqual([path.read_text() for path in (book, glossary)], ["old", "old"])
        self.assertEqual(list(self.directory.glob(".ai-translator-stage-*")), [])
        self.assertEqual(list(self.directory.glob(".ai-translator-original-*")), [])

    def test_non_numeric_prose_rejects_numeric_and_punctuation_output(self):
        self.source.write_text("她推开门。", encoding="utf-8")
        group = next(batches(read_book(self.source), 400))
        for text in ("123", "—?!"):
            with self.subTest(text=text), self.assertRaises(InvalidAnswer):
                _translations({"translations": [{"id": 1, "text": text}]}, group)

    def test_numeric_timestamp_and_midnight_checked_in_block_validation(self):
        self.source.write_text("三月四日\n凌晨十二点", encoding="utf-8")
        group = next(batches(read_book(self.source), 400))
        result = {"translations": [{"id": 1, "text": "04.03"}, {"id": 2, "text": "00:00"}]}
        self.assertEqual(_translations(result, group), ["04.03", "00:00"])
        for wrong in ("1999-01-01T00:00", "1999-01-01T00:00Z", "1999-01-01T00:00+03:00"):
            result["translations"][0]["text"] = wrong
            with self.subTest(wrong=wrong), self.assertRaisesRegex(InvalidAnswer, "не соответствуют"):
                _translations(result, group)

    def test_destination_without_safe_publication_fails_before_provider_work(self):
        self.source.write_text("哈利来了。", encoding="utf-8")
        with patch("ai_translator.publication._publish_new", side_effect=OSError("unsupported")):
            _, log = self.invoke("translate", self.source, expected=1)
        self.assertIn("не поддерживает безопасное создание", log)
        self.assertFalse(FakeCodex.calls)

    def test_inspect_reports_cost_for_selected_workflow_and_sample_range(self):
        self.source.write_text("她推开门。\n" * 140, encoding="utf-8")
        for flags in (("--start-batch", "2", "--batches", "1"),
                      ("--for", "glossary", "--batches", "1"),
                      ("--list-chapters", "--for", "sample", "--start-batch", "2")):
            with self.subTest(flags=flags):
                output, _ = self.invoke("inspect", self.source, "--json-events", *flags, expected=1)
                event = json.loads(output)
                self.assertEqual(event["event"], "error")
                self.assertIn("--for sample", event["message"])
        self.assertFalse(default_state_path(self.source).exists())
        for mode, flags, expected in (
            ("translate", ["--edit"], (3, 3, 3)),
            ("glossary", ["--edit"], (3, 0, 0)),
            ("sample", ["--start-batch", "2", "--edit", "--no-auto-glossary"], (0, 1, 1)),
        ):
            output, _ = self.invoke("inspect", self.source, "--json-events", "--for", mode, *flags)
            events = {item["event"]: item for item in map(json.loads, output.splitlines())}
            plan = events["plan"]
            self.assertEqual(tuple(plan[key] for key in ("glossary", "translate", "edit")), expected)
            if mode == "sample":
                preview = events["sample_range"]
                self.assertEqual((preview["first_batch"], preview["last_batch"]), (2, 2))
                self.assertEqual(preview["opening"], "她推开门。")
        self.assertFalse(FakeCodex.calls)

    def test_sample_can_translate_later_batch_without_translating_opening(self):
        self.source.write_text("她推开门。\n" * 64 + "赫敏终于回来了。\n", encoding="utf-8")
        self.invoke("sample", self.source, "--start-batch", "2", "--no-auto-glossary", "--max-calls", "1")
        comparison = (self.directory / "Book.sample.ru.txt").read_text()
        self.assertIn("赫敏终于回来了。", comparison)
        self.assertNotIn("她推开门。", comparison)
        self.assertEqual(len(FakeCodex.calls), 1)
        self.assertIn("她推开门。", FakeCodex.calls[0][1]["preceding_source"])
    def test_strict_glossary_rejects_name_substitution_and_accepts_inflections(self):
        self.source.write_text("哈利没有回来。\n我没看见赫敏。", encoding="utf-8")
        group = next(batches(read_book(self.source), 400))
        glossary = {"哈利": {"ru": "Гарри", "forms": []},
                    "赫敏": {"ru": "Гермиона", "forms": ["Гермиону"]}}
        response = {"translations": [{"id": 1, "text": "Пётр вернулся."},
                                     {"id": 2, "text": "Я не видел Гермиону."}]}
        with self.assertRaisesRegex(InvalidAnswer, "Гарри"):
            _translations(response, group, strict=glossary)
        response["translations"][0]["text"] = "Гарри так и не вернулся."
        self.assertEqual(_translations(response, group, strict=glossary),
                         ["Гарри так и не вернулся.", "Я не видел Гермиону."])

    def test_glossary_aliases_preserve_forms_through_autoglossary_edit_and_replay(self):
        self.source.write_text("妙丽回来了。\n我把信交给妙麗。\n", encoding="utf-8")
        canon = self.directory / "canon.json"
        flat = self.directory / "flat.json"
        saved = self.directory / "approved.json"
        entry = {"ru": "Гермиона Грейнджер", "forms": ["Гермионе Грейнджер"]}
        grouped = {"赫敏": {**entry, "aliases": ["妙丽", "妙麗"]}}
        canon.write_text(json.dumps(grouped, ensure_ascii=False), encoding="utf-8")
        flat.write_text(json.dumps({term: entry for term in ("赫敏", "妙丽", "妙麗")}), encoding="utf-8")
        attempts = []

        def complete(provider, model, instructions, data, schema):
            payload = json.loads(data)
            attempts.append(payload)
            if "entries" in schema["properties"]:
                return {"entries": [{"zh": "妙丽", "ru": "Хермин"}]}
            self.assertEqual(payload["required_terms"], {"妙丽": entry, "妙麗": entry})
            answer = "Гермиона Грейнджер вернулась."
            if "draft" in payload and "Предыдущий ответ отклонён" not in instructions:
                answer = "Хермиона вернулась."
            return {"translations": [{"id": 1, "text": answer},
                                     {"id": 2, "text": "Я передал письмо Гермионе\u00a0Греи\u0306нджер."}]}

        args = ("translate", self.source, "--glossary", canon, "--edit", "--save-glossary", saved)
        with patch.object(FakeCodex, "complete", complete):
            self.invoke(*args, "--max-calls", "3", expected=3)
            self.assertFalse(saved.exists())
            self.invoke(*args, "--max-calls", "1")
        self.assertEqual(len(attempts), 4)
        self.assertEqual(attempts[0]["glossary"], {"妙丽": entry, "妙麗": entry})
        self.assertEqual(json.loads(saved.read_text(encoding="utf-8")), grouped)
        self.assertIn("Гермиона Грейнджер вернулась", (self.directory / "Book.ru.txt").read_text(encoding="utf-8"))
        output, _ = self.invoke("inspect", self.source, "--glossary", flat, "--edit", "--json-events")
        replay = next(event for event in map(json.loads, output.splitlines()) if event["event"] == "cache_replay")
        self.assertTrue(replay["complete"])
        self.assertEqual(replay["reusable_answers"], 3)

    def test_advisory_alias_is_kept_whole_at_batch_boundary(self):
        self.source.write_text("雨" * 399 + "妙丽" + "夜" * 30, encoding="utf-8")
        glossary = self.directory / "canon.json"
        glossary.write_text('{"赫敏":{"ru":"Гермиона","aliases":["妙丽"],"strict":false}}', encoding="utf-8")
        config = self.directory / "small.toml"
        config.write_text("batch_chars = 400\n", encoding="utf-8")
        self.invoke("sample", self.source, "--start-batch", "2", "--no-auto-glossary", "--max-calls", "1",
                    "--glossary", glossary, "--config", config)
        _, payload = FakeCodex.calls[0]
        self.assertTrue(payload["blocks"][0]["text"].startswith("妙丽"))
        self.assertEqual(payload["glossary"], {"妙丽": "Гермиона"})
        self.assertNotIn("required_terms", payload)

    def test_epub_size_limit_stops_before_any_model_calls(self):
        source = self.directory / "book.epub"
        write_epub(source)
        with patch("ai_translator.formats.MAX_EPUB_EXPANDED", 32):
            _, log = self.invoke("translate", source, expected=1)
        self.assertIn("Распакованный EPUB", log)
        self.assertFalse(FakeCodex.calls)

    def test_epub_markup_budget_stops_before_model_calls(self):
        source = self.directory / "book.epub"
        write_epub(source)
        with patch("ai_translator.formats.MAX_EPUB_TOTAL_MARKUP", 1024):
            _, log = self.invoke("translate", source, expected=1)
        self.assertIn("Суммарный объём", log)
        self.assertFalse(FakeCodex.calls)

    def test_source_limits_fail_before_model_calls(self):
        self.source.write_text("哈利来了。", encoding="utf-8")
        fb2 = self.directory / "book.fb2"
        epub = self.directory / "book.epub"
        write_fb2(fb2)
        write_epub(epub)
        for source, limit in ((self.source, "MAX_TXT_BYTES"), (fb2, "MAX_FB2_BYTES"), (epub, "MAX_EPUB_ENTRIES")):
            with self.subTest(extension=source.suffix), patch(f"ai_translator.formats.{limit}", 1):
                _, log = self.invoke("translate", source, expected=1)
                self.assertIn("Ошибка", log)
        self.assertFalse(FakeCodex.calls)

    def test_provider_refusal_is_not_retried_or_saved_as_a_correction(self):
        self.source.write_text("哈利来了。", encoding="utf-8")
        cache = self.directory / ".Book.txt.ai-translator.sqlite3"
        with patch.object(FakeCodex, "complete", side_effect=ProviderRefusal("Провайдер отклонил запрос.")) as complete:
            _, log = self.invoke("translate", self.source, "--no-auto-glossary", "--max-calls", "2", expected=1)
            self.assertEqual(complete.call_count, 1)
        self.assertIn("отклонил запрос", log)
        with open_state(cache) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM corrections").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM responses").fetchone()[0], 0)

    def test_openai_transport_failure_spends_one_attempt_without_retry_or_publication(self):
        self.source.write_text("哈利来了。", encoding="utf-8")
        args = ("translate", self.source, "--provider", "openai-api", "--no-auto-glossary", "--json-events")
        with patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}), patch(
            "ai_translator.provider._api_open", side_effect=IncompleteRead(b"partial"),
        ) as request:
            output, _ = self.invoke(*args, "--max-total-calls", "2", expected=1)
        request.assert_called_once()
        events = list(map(json.loads, output.splitlines()))
        self.assertIn("прервано до конца ответа", events[-1]["message"])
        result = self.directory / "Book.ru.txt"
        self.assertFalse(result.exists())
        with open_state(self.directory / ".Book.txt.ai-translator.sqlite3") as db:
            self.assertEqual(db.execute("SELECT attempts FROM request_budget").fetchone(), (1,))
            for table in ("responses", "corrections"):
                self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone(), (0,))

        translation = json.dumps({"translations": [{"id": 1, "text": "Гарри пришёл."}]})
        envelope = {"status": "completed", "output": [{"type": "message", "status": "completed",
                    "content": [{"type": "output_text", "text": translation}]}]}
        with patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}), patch(
            "ai_translator.provider._api_open", return_value=io.BytesIO(json.dumps(envelope).encode()),
        ) as request:
            output, _ = self.invoke(*args)
        request.assert_called_once()
        self.assertIn('"attempts": 2, "ceiling": 2', output)
        self.assertEqual(result.read_text(encoding="utf-8"), "Гарри пришёл.")

    def test_openai_invalid_service_body_does_not_schedule_a_correction(self):
        self.source.write_text("哈利来了。", encoding="utf-8")
        with patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}), patch(
            "ai_translator.provider._api_open", side_effect=lambda *_args, **_kwargs: io.BytesIO(b"[]"),
        ) as request:
            _, log = self.invoke("translate", self.source, "--provider", "openai-api", "--no-auto-glossary",
                                 "--max-calls", "2", expected=1)
        request.assert_called_once()
        self.assertIn("некорректный служебный ответ", log)
        self.assertFalse((self.directory / "Book.ru.txt").exists())
        with open_state(self.directory / ".Book.txt.ai-translator.sqlite3") as db:
            self.assertEqual(db.execute("SELECT attempts FROM request_budget").fetchone(), (1,))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM corrections").fetchone(), (0,))

    def test_epub_rejects_large_directory_before_instantiating_zipfile(self):
        source = self.directory / "book.epub"
        write_epub(source)
        with patch("ai_translator.formats.MAX_EPUB_DIRECTORY", 1), patch(
            "ai_translator.formats.zipfile.ZipFile", side_effect=AssertionError("ZIP parser opened too early"),
        ):
            with self.assertRaisesRegex(ValueError, "Каталог ZIP"):
                read_book(source)

    def test_epub_counts_actual_metadata_entries_before_instantiating_zipfile(self):
        source = self.directory / "book.epub"
        write_epub(source)
        archive = bytearray(source.read_bytes())
        end = archive.rfind(zipfile.stringEndArchive)
        struct.pack_into("<HH", archive, end + 8, 1, 1)
        source.write_bytes(archive)
        with patch("ai_translator.formats.MAX_EPUB_ENTRIES", 3), patch(
            "ai_translator.formats.zipfile.ZipFile", side_effect=AssertionError("ZIP parser opened too early"),
        ):
            with self.assertRaisesRegex(ValueError, "больше 20 000"):
                read_book(source)

    def test_epub_directory_preflight_accepts_zip64(self):
        source = self.directory / "book.epub"
        with patch("zipfile.ZIP64_LIMIT", 128):
            write_epub(source)
        self.assertIn(zipfile.stringEndArchive64, source.read_bytes())
        self.assertTrue(any(read_book(source).sections))

    def test_epub_fallback_svg_and_ncx_preserve_reading_order_and_links(self):
        original = self.directory / "base.epub"
        write_epub(original)
        path = self.directory / "variant.epub"
        opf = "{http://www.idpf.org/2007/opf}"
        foreign = b"opaque resource fixture"
        with zipfile.ZipFile(original) as src, zipfile.ZipFile(path, "w") as dst:
            for info in src.infolist():
                data = src.read(info)
                if info.filename == "OPS/book.opf":
                    package = etree.fromstring(data)
                    manifest = package.find(opf + "manifest")
                    spine = package.find(opf + "spine")
                    etree.SubElement(manifest, opf + "item", id="foreign", href="foreign.bin",
                                     attrib={"media-type": "application/octet-stream", "fallback": "first"})
                    etree.SubElement(manifest, opf + "item", id="svg", href="page.svg",
                                     attrib={"media-type": "image/svg+xml"})
                    etree.SubElement(manifest, opf + "item", id="ncx", href="toc.ncx",
                                     attrib={"media-type": "application/x-dtbncx+xml"})
                    spine.set("toc", "ncx")
                    spine[0].set("idref", "foreign")
                    etree.SubElement(spine, opf + "itemref", idref="svg")
                    data = etree.tostring(package)
                dst.writestr(info, data)
            dst.writestr("OPS/foreign.bin", foreign)
            dst.writestr("OPS/page.svg", '<svg xmlns="http://www.w3.org/2000/svg"><text id="mark" x="8">雨夜</text></svg>')
            dst.writestr("OPS/toc.ncx", '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/"><navMap>'
                         '<navPoint id="p"><navLabel><text>雨夜</text></navLabel>'
                         '<content src="chapter1.xhtml#opening"/></navPoint></navMap></ncx>')
        self.invoke("translate", path, "--no-auto-glossary")
        with zipfile.ZipFile(self.directory / "variant.ru.epub") as result:
            self.assertIsNone(result.testzip())
            self.assertEqual(result.read("OPS/foreign.bin"), foreign)
            package = etree.fromstring(result.read("OPS/book.opf"))
            self.assertEqual([ref.get("idref") for ref in package.find(opf + "spine")],
                             ["foreign", "second", "svg"])
            svg = etree.fromstring(result.read("OPS/page.svg")).find("{*}text")
            self.assertEqual(svg.get("id"), "mark")
            self.assertNotEqual(svg.text, "雨夜")
            ncx = etree.fromstring(result.read("OPS/toc.ncx"))
            self.assertNotEqual(ncx.find(".//{*}text").text, "雨夜")
            self.assertEqual(ncx.find(".//{*}content").get("src"), "chapter1.xhtml#opening")



if __name__ == "__main__":
    main()
