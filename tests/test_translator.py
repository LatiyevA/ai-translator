import base64
import io
import json
import subprocess
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, main
from unittest.mock import patch

from lxml import etree

from ai_translator.cli import main as cli
from ai_translator.config import Model, load_config
from ai_translator.formats import ANCHOR, _xml, read_book
from ai_translator.pipeline import InvalidAnswer, _entries, _split, _translations, batches
from ai_translator.provider import OpenAIAPI, ProviderError, Usage


def write_epub(path: Path) -> None:
    entries = {
        "mimetype": b"application/epub+zip",
        "META-INF/container.xml": b"""<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
            <rootfiles><rootfile full-path="OPS/book.opf"/></rootfiles></container>""",
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
        self.directory = Path(temporary.name)
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
        self.assertEqual(status, expected, stderr.getvalue())
        return stdout.getvalue(), stderr.getvalue()

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
        _, log = self.invoke(*args, expected=1)
        self.assertIn("Достигнут лимит --max-calls", log)
        self.assertEqual(len(FakeCodex.calls), 1)
        _, log = self.invoke(*args)
        self.assertIn("Ответов из кеша: 1", log)
        self.assertEqual(len(FakeCodex.calls), 2)

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
            self.invoke(*args, expected=1)
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
                parts = list(_split(text, 400, ("张三",)))
                self.assertEqual("".join(parts), text)
                self.assertTrue(parts[1].startswith(name))

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

    def test_automatic_glossary_rejects_han_only_but_preserves_approved_entries(self):
        response = {"entries": [{"zh": "小明", "ru": "小明"}]}
        with self.assertRaises(InvalidAnswer):
            _entries(response, ["小明推开了门。"], {})
        self.assertEqual(_entries(response, ["小明推开了门。"], {"小明": "小明"}), {})
        response["entries"][0]["ru"] = "Xiaoming"
        self.assertEqual(_entries(response, ["小明推开了门。"], {}), {"小明": "Xiaoming"})

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
        response = {"status": "completed", "output": [{"type": "message", "content": [
            {"type": "output_text", "text": '{"result":"по-русски"}'},
        ]}]}
        stub = io.BytesIO(json.dumps(response).encode())
        with patch.dict("os.environ", {"OPENAI_API_KEY": "example"}), patch(
            "ai_translator.provider.urlopen", return_value=stub
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

    def test_claude_code_uses_local_login_and_structured_print_mode(self):
        self.source.write_text("哈利来了。", encoding="utf-8")
        config = self.directory / "claude.toml"
        config.write_text('[translate]\nmodel = "sonnet"\nreasoning = "high"\n', encoding="utf-8")
        auth = {"loggedIn": True, "authMethod": "claude.ai"}
        response = {"structured_output": {"translations": [{"id": 1, "text": "Гарри пришёл."}]},
                    "usage": {"input_tokens": 10, "cache_read_input_tokens": 3, "output_tokens": 5}}
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "ignored"}), patch(
            "ai_translator.provider.shutil.which", return_value="/fake/claude"
        ), patch("ai_translator.provider.subprocess.run", side_effect=[
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
            "ai_translator.provider.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, '{"loggedIn":true,"authMethod":"api_key"}', ""),
        ) as run:
            _, log = self.invoke("translate", self.source, "--provider", "claude-code",
                                 "--no-auto-glossary", expected=1)
            self.assertIn("claude auth login", log)
            self.assertEqual(run.call_count, 1)



if __name__ == "__main__":
    main()
