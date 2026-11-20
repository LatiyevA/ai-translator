import codecs
import hashlib
import os
import posixpath
import re
import shutil
import struct
import zipfile
from bisect import bisect_left, bisect_right
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from html.entities import name2codepoint
from io import BytesIO
from itertools import accumulate
from pathlib import Path
from urllib.parse import unquote, urlsplit

from lxml import etree

HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U000323af]")
ANCHOR = re.compile(r"⟦A[0-9]{1,18}⟧")
XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"
XML_ID = "{http://www.w3.org/XML/1998/namespace}id"
XLINK_HREF = "{http://www.w3.org/1999/xlink}href"
EPUB_TYPE = "{http://www.idpf.org/2007/ops}type"
DC = "{http://purl.org/dc/elements/1.1/}"
SVG = "{http://www.w3.org/2000/svg}"
SVG_TEXT_POSITION = frozenset(("x", "y", "dx", "dy", "rotate"))
TXT_HEADING = re.compile(
    r"第[〇零一二三四五六七八九十百千万两兩壹贰貳叁參肆伍陆陸柒捌玖拾佰仟0-9０-９]+[章节節回卷部篇]"
    r"(?=$|[\s:：、.．—(（\[【-])"
)
FB2_AUXILIARY_BODIES = frozenset(("notes", "footnotes", "endnotes", "comments"))
# Saved chapter numbers are meaningful only for the rules that produced them.
CHAPTER_CATALOG_VERSION = 3
FORMAT_CATALOG_VERSIONS = {".fb2": 7, ".epub": 5}
BLOCKS = frozenset("""
    address article aside blockquote body caption dd div dl dt epigraph figcaption
    figure footer h1 h2 h3 h4 h5 h6 header li main nav ol p poem pre section
    stanza subtitle table tbody td text-author tfoot th thead title tr ul v
""".split())
XHTML_OPAQUE = frozenset("""
    audio canvas iframe math rt rp script style svg video
""".split())
XHTML_VOID = frozenset("area br col embed hr img input source track wbr".split())
XHTML_HEADINGS = tuple(f"h{level}" for level in range(1, 7))
XML_EMPTY = frozenset(("image", "empty-line"))
MAX_EPUB_EXPANDED = 512 * 1024 * 1024
MAX_EPUB_ENTRIES = 20_000
MAX_EPUB_DIRECTORY = 16 * 1024 * 1024
MAX_EPUB_MARKUP = 32 * 1024 * 1024
MAX_EPUB_TOTAL_MARKUP = 128 * 1024 * 1024
MAX_TXT_BYTES = 32 * 1024 * 1024
MAX_FB2_BYTES = 128 * 1024 * 1024
XHTML_ENTITIES = "\n".join(
    f'<!ENTITY {name} "&#{number};">' for name, number in name2codepoint.items()
)


@dataclass
class Unit:
    source: str
    replace: Callable[[str], None]
    link_labels: tuple[tuple[str | None, str | None], ...] = ()
    line_breaks: frozenset[str] = frozenset()
    markup_markers: frozenset[str] = frozenset()
    positioned_labels: tuple[tuple[str | None, str | None], ...] = ()

    def visible_text(self, text: str, *, show_line_breaks: bool = True) -> str:
        if not self.markup_markers:
            return text

        def replace_marker(match: re.Match) -> str:
            if show_line_breaks and match[0] in self.line_breaks:
                return "\n"
            return "" if match[0] in self.markup_markers else match[0]

        return ANCHOR.sub(replace_marker, text)


@dataclass(frozen=True)
class Chapter:
    title: str
    spans: tuple[tuple[int, int, int], ...]

    @property
    def unit_count(self) -> int:
        return sum(end - start for _, start, end in self.spans)


@dataclass
class Book:
    sections: list[list[Unit]]
    write: Callable[[Path], None]
    narrative_sections: frozenset[int] = frozenset()
    chapters: tuple[Chapter, ...] = ()


def chapter_revision(path: Path, encoding: str | None) -> str:
    stat = path.stat()
    version = FORMAT_CATALOG_VERSIONS.get(path.suffix.lower(), CHAPTER_CATALOG_VERSION)
    identity = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, encoding or "",
                version)
    return hashlib.sha256(repr(identity).encode("utf-8")).hexdigest()


def _chapter_catalog(regions: list[tuple[int, int, int]], starts: dict[tuple[int, int], str]) -> tuple[Chapter, ...]:
    if not starts or not regions:
        return ()
    edges = list(accumulate((end - start for _, start, end in regions), initial=0))
    if not edges[-1]:
        return ()
    positions = {section: (edges[index], start, end) for index, (section, start, end) in enumerate(regions)}
    titles = {0: "Начало книги"}
    for (section, index), title in sorted(starts.items()):
        if section in positions:
            offset, start, end = positions[section]
            absolute = offset + index - start
            if start <= index <= end and absolute < edges[-1]:
                titles[absolute] = title
    bookmarks = sorted(titles.items()) + [(edges[-1], "")]
    chapters = []
    for (start, title), (end, _) in zip(bookmarks, bookmarks[1:]):
        spans = []
        for index in range(bisect_right(edges, start) - 1, bisect_left(edges, end)):
            section, base, stop = regions[index]
            if base < stop:
                spans.append((section, base + max(start - edges[index], 0),
                              base + min(end, edges[index + 1]) - edges[index]))
        chapters.append(Chapter(title, tuple(spans)))
    return tuple(chapters)


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


def _element_title(node: etree._Element | None) -> str:
    return " ".join("".join(node.itertext()).split())[:180] if node is not None else ""


def _epub_navigation(node: etree._Element) -> bool:
    return _name(node) == "nav" and bool(node.get(EPUB_TYPE))


def _language(node: etree._Element) -> None:
    for key in (XML_LANG, "lang"):
        value = node.get(key, "")
        if value.lower().split("-")[0] in ("zh", "cmn", "zho", "chi"):
            node.set(key, "ru")


def _xml_units(roots: Iterable[etree._Element], *, xhtml: bool = False,
               positions: dict[etree._Element, int] | None = None, position_offset: int = 0,
               ends: dict[etree._Element, int] | None = None,
               excluded: set[etree._Element] | None = None) -> list[Unit]:
    units: list[Unit] = []
    slots: list[tuple[etree._Element, str]] = []
    deferred: list[etree._Element | Unit] = []
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
            positioned_labels = []
            earlier = [None, *anchors]
            later = [*anchors, None]
            for (node, field), value, first, last in zip(slots, values, earlier, later):
                owner = node if field == "text" else node.getparent()
                if value.strip() and owner is not None:
                    elements = (owner, *owner.iterancestors())
                    if any(_name(element) == "a" for element in elements):
                        link_labels.append((first, last))
                    if any(element.tag == SVG + "tspan" and SVG_TEXT_POSITION.intersection(element.attrib)
                           for element in elements):
                        positioned_labels.append((first, last))
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

            line_breaks = frozenset(
                marker for marker, (node, field) in zip(anchors, slots[1:])
                if xhtml and field == "tail" and _name(node) == "br"
            )
            units.append(Unit(source.strip(), replace, tuple(link_labels), line_breaks, frozenset(anchors),
                              tuple(positioned_labels)))
        slots.clear()
        for content in deferred:
            if isinstance(content, Unit):
                units.append(content)
            else:
                units.extend(_svg_units(content))
        deferred.clear()

    def visit(node: etree._Element) -> None:
        if excluded and node in excluded:
            if _name(node) in BLOCKS:
                flush()
            return
        if positions is not None and node in positions:
            flush()
            positions[node] = position_offset + len(units)
        if xhtml and node.tag == SVG + "svg":
            if positions is not None and any(target in positions for target in node.iter()):
                flush()
                units.extend(_svg_units(node, positions, position_offset + len(units)))
            else:
                # Keep the surrounding XHTML paragraph intact.
                deferred.append(node)
            return
        name = _name(node)
        if not name:
            return
        if xhtml and positions is not None and _epub_navigation(node):
            flush()
            return
        if xhtml and name in {"a", "img"}:
            attributes = ("alt", "title", "aria-label") if name == "img" else ("title", "aria-label")
            for attribute in attributes:
                value = node.get(attribute, "").strip()
                if HAN.search(value):
                    _language(node)
                    deferred.append(Unit(value, lambda text, node=node, attribute=attribute: node.set(attribute, text)))
        if name in skipped:
            return
        _language(node)
        if xhtml and name == "li" and any(
            _epub_navigation(parent) for parent in node.iterancestors()
        ):
            # EPUB navigation permits a label element and a nested list, but no free text.
            flush()
            for child in node:
                visit(child)
                flush()
            return
        is_block = name in BLOCKS or node.tag == SVG + "desc"
        if is_block:
            flush()
        slots.append((node, "text"))
        for child in node:
            visit(child)
            slots.append((child, "tail"))
        if is_block:
            flush()
            if ends is not None and node in ends:
                ends[node] = position_offset + len(units)

    for root in roots:
        visit(root)
        flush()
    return units


def _svg_units(root: etree._Element, positions: dict[etree._Element, int] | None = None,
               position_offset: int = 0) -> list[Unit]:
    prefix = SVG if root.tag == SVG + "svg" else ""
    labels = {prefix + name for name in ("title", "desc", "text")}
    _language(root)
    if positions is None:
        return _xml_units(node for node in root.iter(*labels)
                          if not any(parent.tag in labels for parent in node.iterancestors()))
    units: list[Unit] = []

    def visit(node: etree._Element) -> None:
        if node in positions:
            positions[node] = position_offset + len(units)
        if node.tag in labels:
            units.extend(_xml_units([node], positions=positions, position_offset=position_offset + len(units)))
            return
        for child in node:
            visit(child)

    visit(root)
    return units


def _read_limited(path: Path, limit: int) -> bytes:
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"Исходный {path.suffix.upper().lstrip('.')} превышает предел {limit // (1024 * 1024)} МиБ.")
    return data


def _txt(path: Path, encoding: str | None, *, with_chapters: bool) -> Book:
    data = _read_limited(path, MAX_TXT_BYTES)
    if encoding:
        content = data.decode(encoding)
        if codecs.lookup(encoding).name in {"utf-8", "utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be"}:
            content = content.removeprefix("\ufeff")
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
    starts = {}
    for index, line in enumerate(lines):
        if HAN.search(line):
            core = line.strip()
            if with_chapters and len(core) <= 120 and TXT_HEADING.match(core):
                starts[0, len(units)] = core
            indent = line[:len(line) - len(line.lstrip())]
            ending = line[len(line.rstrip()):]

            def replace(text: str, index=index, indent=indent, ending=ending) -> None:
                lines[index] = indent + text + ending

            units.append(Unit(core, replace))

    def write(output: Path) -> None:
        output.write_bytes("".join(lines).encode("utf-8"))

    return Book([units], write, chapters=_chapter_catalog([(0, 0, len(units))], starts))


def _fb2_note_targets(root: etree._Element) -> set[str]:
    return {
        unquote(href[1:]) for link in root.iter("{*}a") if link.get("type", "").casefold() == "note"
        and (href := link.get(XLINK_HREF) or link.get("href") or "").startswith("#") and len(href) > 1
    }


def _fb2(path: Path, *, with_chapters: bool) -> Book:
    original = _read_limited(path, MAX_FB2_BYTES)
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
    chapters = []
    note_targets = _fb2_note_targets(root) if with_chapters else set()
    for body_index, body in enumerate(root.findall("{*}body")):
        body_name = body.get("name", "").strip().casefold()
        if not with_chapters or body_name in FB2_AUXILIARY_BODIES:
            sections.append(_xml_units([body]))
            continue
        body_title = _element_title(body.find("{*}title"))
        titles = {node: _element_title(node.find("{*}title")) for node in body.iterfind(".//{*}section")}
        # An anonymous secondary body can contain notes. Explicit headings also identify story volumes.
        if body_index and not body_name and not body_title and not any(titles.values()):
            sections.append(_xml_units([body]))
            continue
        headings = {node: title or f"Раздел {index}" for index, (node, title) in enumerate(titles.items(), 1)}
        positions = dict.fromkeys(headings, -1)
        ends = positions.copy()
        notes = set()
        if note_targets:
            notes = {node for node in body.iter() if (node.get("id") or node.get(XML_ID)) in note_targets}
        index = len(sections)
        units = _xml_units([body], positions=positions, ends=ends, excluded=notes)
        sections.append(units)
        subchapters = [
            Chapter(label, ((index, positions[node], ends[node]),))
            for node, label in headings.items() if 0 <= positions[node] < ends[node]
        ]
        if body_title and subchapters:
            chapters.append(Chapter(body_title, ((index, 0, len(units)),)))
        elif (first := min((position for position in positions.values() if position >= 0), default=0)) > 0:
            label = body_title or "Начало книги"
            chapters.append(Chapter(label, ((index, 0, first),)))
        chapters.extend(subchapters)
    if any(sections) and lang is not None:
        lang.text = "ru"

    def write(output: Path) -> None:
        output.write_bytes(_serialize(tree) if any(sections) else original)

    return Book(sections, write, chapters=tuple(chapters))


def _href(base: str, href: str) -> str:
    parts = urlsplit(href)
    relative = unquote(parts.path)
    if parts.scheme or parts.netloc or not relative or relative.startswith("/"):
        raise ValueError(f"В EPUB ожидался относительный путь: {href}")
    name = posixpath.normpath(posixpath.join(posixpath.dirname(base), relative))
    if name == ".." or name.startswith("../"):
        raise ValueError(f"Недопустимый путь внутри EPUB: {href}")
    return name


def _check_epub_directory(stream) -> None:
    # Use zipfile's bounded end-record reader to share its ZIP64 and comment handling.
    end = zipfile._EndRecData(stream)
    if end is None:
        raise zipfile.BadZipFile("Файл не содержит каталога ZIP.")
    if end[zipfile._ECD_ENTRIES_TOTAL] > MAX_EPUB_ENTRIES:
        raise ValueError("EPUB содержит больше 20 000 ресурсов.")
    directory_size = end[zipfile._ECD_SIZE]
    if directory_size > MAX_EPUB_DIRECTORY:
        raise ValueError("Каталог ZIP внутри EPUB превышает 16 МиБ.")
    directory_end = end[zipfile._ECD_LOCATION]
    if end[zipfile._ECD_SIGNATURE] == zipfile.stringEndArchive64:
        # Older Python releases report the position of the standard EOCD even for ZIP64.
        stream.seek(directory_end)
        if stream.read(4) == zipfile.stringEndArchive:
            directory_end -= zipfile.sizeEndCentDir64 + zipfile.sizeEndCentDir64Locator
    if directory_end < directory_size:
        raise zipfile.BadZipFile("Некорректное смещение ZIP-каталога.")
    stream.seek(directory_end - directory_size)
    remaining = directory_size
    count = 0
    while remaining:
        header = stream.read(zipfile.sizeCentralDir)
        if len(header) != zipfile.sizeCentralDir or not header.startswith(zipfile.stringCentralDir):
            raise zipfile.BadZipFile("Некорректная запись ZIP-каталога.")
        extra = sum(struct.unpack_from("<HHH", header, 28))
        remaining -= len(header) + extra
        if remaining < 0:
            raise zipfile.BadZipFile("Запись ZIP выходит за пределы каталога.")
        count += 1
        if count > MAX_EPUB_ENTRIES:
            raise ValueError("EPUB содержит больше 20 000 ресурсов.")
        stream.seek(extra, os.SEEK_CUR)
    stream.seek(0)


def _checked_zip(stream) -> zipfile.ZipFile:
    _check_epub_directory(stream)
    return zipfile.ZipFile(stream)


def _epub_toc_links(name: str, tree: etree._ElementTree, *, navigation: bool):
    if navigation:
        for nav in tree.iter("{*}nav"):
            if "toc" in nav.get(EPUB_TYPE, "").split():
                for link in nav.iter("{*}a"):
                    parts = link.xpath('.//text() | .//*[local-name() = "img"]/@alt')
                    label = "".join(parts).strip() or link.get("title", "")
                    yield name, link.get("href"), " ".join(label.split())[:180]
    else:
        for point in tree.iter("{*}navPoint"):
            content = point.find("{*}content")
            if content is not None:
                yield name, content.get("src"), _element_title(point.find("./{*}navLabel/{*}text"))


def _epub_chapter_targets(links, reading: dict[str, etree._Element]):
    targets: dict[str, dict[etree._Element, str]] = {}
    identifiers = {}
    for base, href, label in links:
        if not href:
            continue
        try:
            parts = urlsplit(href)
            name = _href(base, href) if parts.path or parts.netloc or parts.scheme else base
        except ValueError:
            continue
        root = reading.get(name)
        if root is None:
            continue
        target = root
        fragment = unquote(parts.fragment)
        if fragment:
            if name not in identifiers:
                identifiers[name] = {
                    identifier: node
                    for node in root.iter()
                    if (identifier := node.get("id") or node.get(XML_ID)
                        or (node.get("name") if _name(node) == "a" else None))
                }
                if _name(root) == "body" and (html := root.getparent()) is not None:
                    if identifier := html.get("id") or html.get(XML_ID):
                        identifiers[name].setdefault(identifier, root)
            target = identifiers[name].get(fragment)
        if target is not None:
            targets.setdefault(name, {}).setdefault(target, label or fragment[:180] or posixpath.basename(name))
    return targets


def _epub_headings(body: etree._Element) -> dict[etree._Element, str]:
    return {
        node: label for node in body.iter(*(f"{{*}}{level}" for level in XHTML_HEADINGS))
        if not any(_epub_navigation(parent) for parent in node.iterancestors())
        and (label := _element_title(node))
    }


def _epub_target_heading(target: etree._Element) -> etree._Element | None:
    for node in (target, *target.iterancestors()):
        if _name(node) in XHTML_HEADINGS:
            return node
    if _name(target) in {"article", "body", "div", "section"} and not (target.text or "").strip():
        first = next((child for child in target if isinstance(child.tag, str)), None)
        if first is not None and _name(first) == "header":
            first = next(first.iter(*(f"{{*}}{level}" for level in XHTML_HEADINGS)), None)
        if first is not None and _name(first) in XHTML_HEADINGS:
            return first
    return None


def _epub_document_chapters(body: etree._Element, title: str, listed: dict[etree._Element, str],
                            *, toc_available: bool, listed_headings: set[etree._Element],
                            chapter_levels: set[str]) -> dict[etree._Element, str]:
    if body.tag == SVG + "svg" or _name(body) == "svg":
        return listed if toc_available else {body: title}
    headings = _epub_headings(body)
    if toc_available:
        # Numbered titles and peers of TOC headings can restore an omitted chapter boundary.
        inferred = {
            node: label for node, label in headings.items()
            if node not in listed_headings and (_name(node) in chapter_levels or TXT_HEADING.match(label))
        }
        return {**inferred, **listed}
    if headings:
        chapter_level = min(map(_name, headings))
        return {node: label for node, label in headings.items() if _name(node) == chapter_level}
    return {body: title}


def _epub(path: Path, *, with_chapters: bool) -> Book:
    documents: dict[str, etree._ElementTree] = {}
    sections: list[list[Unit]] = []
    narrative_sections = set()
    chapter_regions = []
    chapter_starts = {}

    def file_version(stream) -> tuple:
        stat = os.fstat(stream.fileno())
        return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns

    with path.open("rb") as stream, _checked_zip(stream) as archive:
        version = file_version(stream)
        if len(archive.infolist()) > MAX_EPUB_ENTRIES:
            raise ValueError("EPUB содержит больше 20 000 ресурсов.")
        names = archive.namelist()
        if sum(info.file_size for info in archive.infolist()) > MAX_EPUB_EXPANDED:
            raise ValueError("Распакованный EPUB превышает 512 МиБ.")
        if any(info.flag_bits & 1 for info in archive.infolist()):
            raise ValueError("Зашифрованные EPUB не поддерживаются.")

        markup_size = 0

        def read_markup(name: str, limit: int = MAX_EPUB_MARKUP) -> bytes:
            nonlocal markup_size
            size = archive.getinfo(name).file_size
            if size > limit:
                raise ValueError(f"Файл разметки EPUB слишком большой: {name}")
            markup_size += size
            if markup_size > MAX_EPUB_TOTAL_MARKUP:
                raise ValueError("Суммарный объём разбираемой разметки EPUB превышает 128 МиБ.")
            return archive.read(name)

        if len(set(names)) != len(names):
            raise ValueError("В EPUB обнаружены повторяющиеся имена файлов.")
        if read_markup("mimetype", 20) != b"application/epub+zip":
            raise ValueError("Некорректный MIME-тип EPUB.")
        container = _xml(read_markup("META-INF/container.xml"))
        rootfile = container.find(".//{*}rootfile")
        if rootfile is None or not rootfile.get("full-path"):
            raise ValueError("В EPUB отсутствует путь к файлу пакета.")
        package_name = rootfile.get("full-path")
        package = documents[package_name] = _xml(read_markup(package_name))
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

        resources = {}
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
            tree = documents[name] = _xml(read_markup(name), xhtml=media_type == "application/xhtml+xml")
            resources[name] = (identifier, media_type, tree)

        reading = {}
        ncx_id = package.find("./{*}spine").get("toc")
        navigation_links = []
        ncx_links = []
        if with_chapters:
            for name, (identifier, media_type, tree) in resources.items():
                properties = manifest[identifier].get("properties", "").split()
                if "nav" in properties:
                    navigation_links.extend(_epub_toc_links(name, tree, navigation=True))
                elif media_type == "application/x-dtbncx+xml" and identifier == ncx_id:
                    ncx_links.extend(_epub_toc_links(name, tree, navigation=False))
                if identifier in linear_resources:
                    root = tree.getroot()
                    body = root if media_type == "image/svg+xml" else root.find("{*}body")
                    if body is not None:
                        reading[name] = body

        targets = _epub_chapter_targets(ncx_links, reading)
        for name, entries in _epub_chapter_targets(navigation_links, reading).items():
            # NAV supplies the label and anchor for entries found in both tables.
            nav_headings = {_epub_target_heading(node) for node in entries}
            nav_headings.discard(None)
            supplemental = {node: label for node, label in targets.get(name, {}).items()
                            if node not in entries and _epub_target_heading(node) not in nav_headings}
            targets[name] = {**supplemental, **entries}
        listed_headings = {
            heading for entries in targets.values() for target in entries
            if (heading := _epub_target_heading(target)) is not None
        }
        chapter_levels = {_name(heading) for heading in listed_headings}
        for name, (identifier, media_type, tree) in resources.items():
            item = manifest[identifier]
            if identifier in linear_resources and "nav" not in item.get("properties", "").split():
                narrative_sections.add(len(sections))
            root = tree.getroot()
            body = reading.get(name)
            headings = {}
            positions = None
            if body is not None:
                title = (_element_title(root.find("./{*}head/{*}title")) or posixpath.basename(name))
                headings = _epub_document_chapters(body, title, targets.get(name, {}), toc_available=bool(targets),
                                                    listed_headings=listed_headings, chapter_levels=chapter_levels)
                positions = dict.fromkeys([body, *headings], -1)
            index = len(sections)
            if media_type == "application/x-dtbncx+xml":
                _language(root)
                sections.append(_xml_units(root.findall(".//{*}text")))
            elif media_type == "image/svg+xml":
                if _name(root) != "svg":
                    raise ValueError(f"Раздел EPUB с типом SVG не содержит SVG: {name}")
                sections.append(_svg_units(root, positions))
            else:
                _language(root)
                sections.append(_xml_units([*root.findall("./{*}head/{*}title"),
                                            *root.findall("./{*}body")], xhtml=True, positions=positions))
            if positions is not None:
                chapter_regions.append((index, positions[body], len(sections[index])))
                chapter_starts.update({(index, positions[node]): label for node, label in headings.items()})

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
                        if name == "mimetype":
                            continue
                        if name in documents:
                            target.writestr(info, _serialize(documents[name]))
                        else:
                            with source.open(info) as resource, target.open(info, "w") as copy:
                                shutil.copyfileobj(resource, copy, length=1024 * 1024)
                    target.comment = source.comment
            if file_version(stream) != version:
                raise ValueError("Исходный EPUB изменился во время записи результата.")

    return Book(sections, write, frozenset(narrative_sections), _chapter_catalog(chapter_regions, chapter_starts))


def read_book(path: Path, encoding: str | None = None, *, with_chapters: bool = False) -> Book:
    suffix = path.suffix.lower()
    if suffix in (".txt", ".fb2", ".epub") and not path.is_file():
        raise ValueError(f"Нужен обычный файл книги: {path}")
    if suffix == ".txt":
        return _txt(path, encoding, with_chapters=with_chapters)
    if encoding:
        raise ValueError("--encoding применяется только к TXT.")
    if suffix == ".fb2":
        return _fb2(path, with_chapters=with_chapters)
    if suffix == ".epub":
        return _epub(path, with_chapters=with_chapters)
    raise ValueError("Поддерживаются файлы TXT, EPUB и FB2.")
