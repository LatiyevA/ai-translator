"""Measure end-to-end inspect time and child peak RSS on Linux with GNU time."""

import argparse
from pathlib import Path
import statistics
import subprocess
import sys
from time import perf_counter
from tempfile import TemporaryDirectory
from zipfile import ZipFile


def fixtures(root, chapters, lines):
    txt, fb2, epub = (root / f"book.{suffix}" for suffix in ("txt", "fb2", "epub"))
    paragraph = "窗外的春雨落下了。"
    txt.write_text("".join(f"第{i}章 回家\n" + (paragraph + "\n") * lines
                           for i in range(1, chapters + 1)), encoding="utf-8")
    sections = [f"<section><title><p>第{i}章 回家</p></title>" + f"<p>{paragraph}</p>" * lines + "</section>"
                for i in range(1, chapters + 1)]
    fb2.write_text("<FictionBook><body>" + "".join(sections) + "</body></FictionBook>", encoding="utf-8")
    with ZipFile(epub, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml",
                         '<container><rootfiles><rootfile full-path="book.opf"/></rootfiles></container>')
        archive.writestr("book.opf", "<package><metadata/><manifest>" + "".join(
            f'<item id="c{i}" href="c{i}.xhtml" media-type="application/xhtml+xml"/>'
            for i in range(chapters)) + "</manifest><spine>" + "".join(
            f'<itemref idref="c{i}"/>' for i in range(chapters)) + "</spine></package>")
        for i, section in enumerate(sections):
            archive.writestr(f"c{i}.xhtml", "<html><body>" + section.replace("<title><p>", "<h1>").replace(
                "</p></title>", "</h1>") + "</body></html>")
    return txt, fb2, epub


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chapters", type=int, default=1000)
    parser.add_argument("--lines", type=int, default=100)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--compare", type=Path, help="Baseline source tree measured on the same files.")
    parser.add_argument("--qt-search", action="store_true", help="Also time filtering an already loaded Qt catalog.")
    args = parser.parse_args()
    with TemporaryDirectory() as directory:
        root = Path(directory)
        # Put the chosen checkout ahead of the installed package.
        launcher = ("import sys; sys.path.insert(0, sys.argv.pop(1)); "
                    "from ai_translator.cli import main; sys.exit(main())")
        for book in fixtures(root, args.chapters, args.lines):
            for label, options in (
                ("catalog", ["--list-chapters"]),
                ("chapter", ["--for", "sample", "--chapter", "1"]),
                ("full", []),
            ):
                repos = {"current": args.repo}
                if args.compare:
                    repos = {"baseline": args.compare, **repos}
                results = {name: [] for name in repos}
                for _ in range(args.runs):
                    for name, repo in repos.items():
                        command = [sys.executable, "-c", launcher, str(repo.resolve() / "src"),
                                   "inspect", "--no-auto-glossary", *options, str(book)]
                        subprocess.run(["/usr/bin/time", "-f", "%e %M", "-o", str(root / "time"), *command],
                                       stdout=subprocess.DEVNULL, check=True)
                        seconds, rss = (root / "time").read_text().split()
                        results[name].append((float(seconds), int(rss)))
                for name, values in results.items():
                    medians = tuple(statistics.median(column) for column in zip(*values))
                    print(f"{book.suffix} {label} {name}: {values} → {medians} (seconds, KiB)", flush=True)
        if args.qt_search:
            qt_search(root, args.repo, args.chapters)


def qt_search(root, repo, count):
    sys.path.insert(0, str(repo.resolve() / "src"))
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication
    from ai_translator.desktop import Window

    app = QApplication([])
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(root))
    window = Window()
    window.show()
    window.operation.setCurrentIndex(window.operation.findData("sample"))
    window._set_chapters([{"number": i, "title": f"第{i}章 回家", "units": 101} for i in range(1, count + 1)], "bench")
    app.processEvents()
    measurements = []
    for query in ["回", "回家", str(count), "无结果", ""] * 12:
        start = perf_counter()
        window.chapter_search.setText(query)
        app.processEvents()
        measurements.append((perf_counter() - start) * 1000)
    print(f"Qt search ({count} entries): {statistics.median(measurements):.2f} ms median, "
          f"{max(measurements):.2f} ms max", flush=True)
    window.close()


if __name__ == "__main__":
    main()
