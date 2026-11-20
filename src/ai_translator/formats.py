import codecs
import os
import posixpath
import re
import shutil
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from html.entities import name2codepoint
from io import BytesIO
from pathlib import Path
from urllib.parse import unquote, urlsplit

from lxml import etree

HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U000323af]")
ANCHOR = re.compile(r"⟦A[0-9]{1,18}⟧")
XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"
DC = "{http://purl.org/dc/elements/1.1/}"
BLOCKS = frozenset("""
    address article aside blockquote body caption dd div dl dt epigraph figcaption
    figure footer h1 h2 h3 h4 h5 h6 header li main nav ol p poem pre section
    stanza subtitle table tbody td text-author tfoot th thead title tr ul v
""".split())
XHTML_OPAQUE = frozenset("""
    audio canvas iframe math rt rp script style svg video
""".split())
XHTML_VOID = frozenset("area br col embed hr img input source track wbr".split())
XML_EMPTY = frozenset(("image", "empty-line"))
XHTML_ENTITIES = "\n".join(
    f'<!ENTITY {name} "&#{number};">' for name, number in name2codepoint.items()
)


@dataclass
class Unit:
    source: str
    replace: Callable[[str], None]
    link_labels: tuple[tuple[str | None, str | None], ...] = ()


@dataclass
class Book:
    sections: list[list[Unit]]
    write: Callable[[Path], None]
    narrative_sections: frozenset[int] = frozenset()


class _XHTMLResolver(etree.Resolver):
    def resolve(self, url, public_id, context):
        if public_id and public_id.startswith("-//W3C//DTD XHTML "):
            return self.resolve_string(XHTML_ENTITIES, context)
        return self.resolve_string("", context)


def _xml(data: bytes, *, xhtml: bool = False, large_fb2: bool = False) -> etree._ElementTree:
    parser = etree.XMLParser(
        resolve_entities="internal" if xhtml else False, no_network=True,
        load_dtd=xhtml, huge_tree=large_fb2,
    )
    if xhtml:
        parser.resolvers.add(_XHTMLResolver())
    tree = etree.parse(BytesIO(data), parser)
    if any(isinstance(node, etree._Entity) for node in tree.iter()):
        raise ValueError("Встроенные XML-сущности в книге не поддерживаются.")
    return tree


def _serialize(tree: etree._ElementTree) -> bytes:
    return etree.tostring(tree, encoding="UTF-8", xml_declaration=True)


def _name(node: etree._Element) -> str:
    return etree.QName(node).localname if isinstance(node.tag, str) else ""


def _language(node: etree._Element) -> None:
    for key in (XML_LANG, "lang"):
        value = node.get(key, "")
        if value.lower().split("-")[0] in ("zh", "cmn", "zho", "chi"):
            node.set(key, "ru")


def _xml_units(roots: Iterable[etree._Element], *, xhtml: bool = False) -> list[Unit]:
    units: list[Unit] = []
    slots: list[tuple[etree._Element, str]] = []
    skipped = XML_EMPTY | XHTML_OPAQUE | XHTML_VOID if xhtml else XML_EMPTY

    def flush() -> None:
        values = [getattr(node, field) or "" for node, field in slots]
        natural_text = "".join(values)
        if HAN.search(natural_text):
            # Distinct boundaries keep the positions of inline tags and links stable.
            reserved = set(ANCHOR.findall(natural_text))
            anchors = []
            index = 1
            while len(anchors) < len(values) - 1:
                marker = f"⟦A{index}⟧"
                if marker not in reserved:
                    anchors.append(marker)
                index += 1
            source = values[0] + "".join(
                anchor + value for anchor, value in zip(anchors, values[1:])
            )
            link_labels = []
            earlier = [None, *anchors]
            later = [*anchors, None]
            for (node, field), value, first, last in zip(slots, values, earlier, later):
                owner = node if field == "text" else node.getparent()
                if value.strip() and owner is not None and (
                    _name(owner) == "a" or any(_name(parent) == "a" for parent in owner.iterancestors())
                ):
                    link_labels.append((first, last))
            indent = source[:len(source) - len(source.lstrip())]
            ending = source[len(source.rstrip()):]

            def replace(
                text: str, targets=tuple(slots), markers=tuple(anchors), indent=indent, ending=ending
            ) -> None:
                text = indent + text + ending
                for (node, field), marker in zip(targets, markers):
                    before, _, text = text.partition(marker)
                    setattr(node, field, before)
                node, field = targets[-1]
                setattr(node, field, text)

            units.append(Unit(source.strip(), replace, tuple(link_labels)))
        slots.clear()

    def visit(node: etree._Element) -> None:
        name = _name(node)
        if not name or name in skipped:
            return
        _language(node)
        is_block = name in BLOCKS
        if is_block:
            flush()
        slots.append((node, "text"))
        for child in node:
            visit(child)
            slots.append((child, "tail"))
        if is_block:
            flush()

    for root in roots:
        visit(root)
        flush()
    return units


def _txt(path: Path, encoding: str | None) -> Book:
    data = path.read_bytes()
    if encoding:
        content = data.decode(encoding)
    elif data.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        content = data.decode("utf-32")
    elif data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        content = data.decode("utf-16")
    else:
        try:
            content = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            content = data.decode("gb18030")
    lines = content.splitlines(keepends=True)
    units = []
    for index, line in enumerate(lines):
        if HAN.search(line):
            core = line.strip()
            indent = line[:len(line) - len(line.lstrip())]
            ending = line[len(line.rstrip()):]

            def replace(text: str, index=index, indent=indent, ending=ending) -> None:
                lines[index] = indent + text + ending

            units.append(Unit(core, replace))

    def write(output: Path) -> None:
        output.write_bytes("".join(lines).encode("utf-8"))

    return Book([units], write)


def _fb2(path: Path) -> Book:
    original = path.read_bytes()
    try:
        tree = _xml(original)
    except etree.XMLSyntaxError as exc:
        if "Text node too long" not in str(exc):
            raise
        tree = _xml(original, large_fb2=True)
        for node in tree.iter():
            if ((len(node.text or "") >= 10_000_000 and _name(node) != "binary")
                    or len(node.tail or "") >= 10_000_000):
                raise exc
    root = tree.getroot()
    if _name(root) != "FictionBook":
        raise ValueError("В файле FB2 отсутствует корневой элемент FictionBook.")
    sections = []
    title = root.find("./{*}description/{*}title-info")
    lang = None
    if title is not None:
        sections.append(_xml_units(
            child for child in title if _name(child) in ("book-title", "annotation", "keywords")
        ))
        lang = title.find("{*}lang")
    _language(root)
    for body in root.findall("{*}body"):
        sections.append(_xml_units([body]))
    if any(sections) and lang is not None:
        lang.text = "ru"

    def write(output: Path) -> None:
        output.write_bytes(_serialize(tree) if any(sections) else original)

    return Book(sections, write)


def _href(base: str, href: str) -> str:
    parts = urlsplit(href)
    relative = unquote(parts.path)
    if parts.scheme or parts.netloc or not relative or relative.startswith("/"):
        raise ValueError(f"В EPUB ожидался относительный путь: {href}")
    name = posixpath.normpath(posixpath.join(posixpath.dirname(base), relative))
    if name == ".." or name.startswith("../"):
        raise ValueError(f"Недопустимый путь внутри EPUB: {href}")
    return name


def _epub(path: Path) -> Book:
    documents: dict[str, etree._ElementTree] = {}
    sections: list[list[Unit]] = []
    narrative_sections = set()
    def file_version(stream) -> tuple:
        stat = os.fstat(stream.fileno())
        return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns

    with path.open("rb") as stream, zipfile.ZipFile(stream) as archive:
        version = file_version(stream)
        names = archive.namelist()
        if len(set(names)) != len(names):
            raise ValueError("В EPUB обнаружены повторяющиеся имена файлов.")
        if archive.read("mimetype") != b"application/epub+zip":
            raise ValueError("Некорректный MIME-тип EPUB.")
        container = _xml(archive.read("META-INF/container.xml"))
        rootfile = container.find(".//{*}rootfile")
        if rootfile is None or not rootfile.get("full-path"):
            raise ValueError("В EPUB отсутствует путь к файлу пакета.")
        package_name = rootfile.get("full-path")
        package = documents[package_name] = _xml(archive.read(package_name))
        _language(package.getroot())
        manifest = {item.get("id"): item for item in package.findall("./{*}manifest/{*}item")}
        spine = package.findall("./{*}spine/{*}itemref")
        if not manifest or not spine or None in manifest or any(not ref.get("idref") for ref in spine):
            raise ValueError("В EPUB отсутствует manifest или spine.")
        metadata = package.find("./{*}metadata")
        if metadata is not None:
            sections.append(_xml_units(
                child for child in metadata if child.tag in {DC + "title", DC + "description", DC + "subject"}
            ))
            for language in metadata.findall(DC + "language"):
                if (language.text or "").strip().lower().split("-")[0] in ("zh", "cmn", "zho", "chi"):
                    language.text = "ru"

        spine_resources = []
        linear_resources = set()
        for ref in spine:
            identifier = ref.get("idref")
            visited = set()
            while identifier in manifest and identifier not in visited:
                visited.add(identifier)
                item = manifest[identifier]
                if item.get("media-type") in ("application/xhtml+xml", "image/svg+xml"):
                    spine_resources.append(identifier)
                    if ref.get("linear") != "no":
                        linear_resources.add(identifier)
                    break
                identifier = item.get("fallback")
            else:
                raise ValueError(f"Неподдерживаемый раздел в порядке чтения EPUB: {', '.join(sorted(visited)) or identifier}.")

        for identifier in dict.fromkeys(spine_resources + list(manifest)):
            if identifier not in manifest:
                raise ValueError(f"Неразрешённый idref в EPUB: {identifier}")
            item = manifest[identifier]
            media_type = item.get("media-type")
            if media_type not in ("application/xhtml+xml", "application/x-dtbncx+xml") and not (
                media_type == "image/svg+xml" and identifier in spine_resources
            ):
                continue
            name = _href(package_name, item.get("href", ""))
            if name in documents:
                continue
            tree = documents[name] = _xml(archive.read(name), xhtml=media_type == "application/xhtml+xml")
            if identifier in linear_resources and "nav" not in item.get("properties", "").split():
                narrative_sections.add(len(sections))
            root = tree.getroot()
            if media_type == "application/x-dtbncx+xml":
                _language(root)
                sections.append(_xml_units(root.findall(".//{*}text")))
            elif media_type == "image/svg+xml":
                if _name(root) != "svg":
                    raise ValueError(f"Раздел EPUB с типом SVG не содержит SVG: {name}")
                _language(root)
                sections.append(_xml_units(root.findall(".//{*}text")))
            else:
                _language(root)
                sections.append(_xml_units([*root.findall("./{*}head/{*}title"),
                                            *root.findall("./{*}body")], xhtml=True))

    def write(output: Path) -> None:
        with path.open("rb") as stream:
            if file_version(stream) != version:
                raise ValueError("Исходный EPUB изменился. Повторите команду с обновлённой книгой.")
            if not any(sections):
                with output.open("wb") as destination:
                    shutil.copyfileobj(stream, destination)
            else:
                with zipfile.ZipFile(stream) as source, zipfile.ZipFile(output, "w") as target:
                    target.writestr("mimetype", b"application/epub+zip", compress_type=zipfile.ZIP_STORED)
                    for info in source.infolist():
                        name = info.filename
                        if name != "mimetype":
                            data = _serialize(documents[name]) if name in documents else source.read(info)
                            target.writestr(info, data)
                    target.comment = source.comment
            if file_version(stream) != version:
                raise ValueError("Исходный EPUB изменился во время записи результата.")

    return Book(sections, write, frozenset(narrative_sections))


def read_book(path: Path, encoding: str | None = None) -> Book:
    suffix = path.suffix.lower()
    if suffix == ".txt":
        return _txt(path, encoding)
    if encoding:
        raise ValueError("--encoding применяется только к TXT.")
    if suffix == ".fb2":
        return _fb2(path)
    if suffix == ".epub":
        return _epub(path)
    raise ValueError("Поддерживаются файлы TXT, EPUB и FB2.")
