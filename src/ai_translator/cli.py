import argparse
import json
import os
import sqlite3
import sys
import zipfile
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path
from tempfile import NamedTemporaryFile

from lxml import etree

from .config import DEFAULT_MODELS, load_config
from .formats import read_book
from .pipeline import Requests, batches, extract, load_glossary, open_state, translate


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Литературный перевод книг с китайского на русский.")
    commands = parser.add_subparsers(dest="command", required=True)
    for command, help_text in (
        ("translate", "Перевести TXT, EPUB или FB2."),
        ("sample", "Перевести начало книги и сохранить двуязычный TXT для оценки."),
        ("glossary", "Подготовить редактируемый JSON-глоссарий."),
        ("inspect", "Показать число блоков и пакетов без обращения к модели."),
    ):
        sub = commands.add_parser(command, help=help_text)
        sub.add_argument("input", type=Path, help="Исходный TXT, EPUB или FB2.")
        sub.add_argument("--config", type=Path, help="Файл параметров TOML.")
        sub.add_argument("--encoding", help="Явная кодировка входного TXT.")
        sub.add_argument("--glossary", type=Path, help="Входной JSON с утверждёнными соответствиями.")
        if command != "glossary":
            sub.add_argument("--no-auto-glossary", action="store_true", help="Пропустить сбор терминов.")
            sub.add_argument("--edit", action="store_true", help="Включить литературную редактуру.")
        if command == "inspect":
            continue
        sub.add_argument("-o", "--output", type=Path, help="Путь к выходному файлу.")
        sub.add_argument("--state", type=Path, help="Путь к SQLite-кешу ответов.")
        sub.add_argument("--provider", choices=DEFAULT_MODELS, help="Переопределить провайдера.")
        sub.add_argument("--max-calls", type=int, help="Максимум новых запросов за запуск.")
        sub.add_argument("--force", action="store_true", help="Заменить существующий выходной файл.")
        if command in ("translate", "sample"):
            sub.add_argument("--save-glossary", type=Path, help="Сохранить итоговый глоссарий в JSON.")
        if command == "sample":
            sub.add_argument("--batches", type=int, default=1, help="Сколько первых пакетов перевести.")
    return parser


def _same_path(first: Path, second: Path) -> bool:
    return first.resolve() == second.resolve() or (
        first.exists() and second.exists() and first.samefile(second)
    )


def _write_outputs(outputs: list[tuple[Path, Callable[[Path], None]]], force: bool) -> None:
    with ExitStack() as cleanup:
        staged = []
        for path, writer in outputs:
            with NamedTemporaryFile(dir=path.parent, prefix=".ai-translator-", delete=False) as stream:
                temporary = Path(stream.name)
            cleanup.callback(temporary.unlink, missing_ok=True)
            writer(temporary)
            staged.append((temporary, path))

        linked = []
        try:
            for temporary, path in staged:
                if force:
                    temporary.replace(path)
                else:
                    os.link(temporary, path)
                    linked.append((temporary, path))
        except OSError:
            for temporary, path in linked:
                if path.exists() and path.samefile(temporary):
                    path.unlink()
            raise


def _validate_paths(reads: list[Path], outputs: list[Path], force: bool) -> None:
    for index, path in enumerate(outputs):
        if any(_same_path(path, other) for other in reads + outputs[:index]):
            raise ValueError(f"Пути исходника, кеша и выходных файлов должны различаться: {path}")
        if not path.parent.is_dir() or path.is_dir():
            raise ValueError(f"Нужен путь к файлу в существующей папке: {path}")
        if not force and (path.exists() or path.is_symlink()):
            raise ValueError(f"Файл уже существует: {path}. Для замены укажите --force.")


def _run(args: argparse.Namespace) -> None:
    settings = load_config(args.config, getattr(args, "provider", None))
    book = read_book(args.input, args.encoding)
    glossary = load_glossary(args.glossary)
    groups = list(batches(book, settings.batch_chars, tuple(glossary)))
    if args.command == "inspect":
        units = sum(map(len, book.sections))
        count = len(groups)
        glossary_calls = 0 if args.no_auto_glossary else count
        edit_calls = count if args.edit else 0
        print(f"Блоков с китайским текстом: {units}. Пакетов: {count}.")
        print(f"Запросов без кеша и повторов: {glossary_calls + count + edit_calls} "
              f"(глоссарий: {glossary_calls}, перевод: {count}, редактура: {edit_calls}).")
        return

    if args.max_calls is not None and args.max_calls < 0:
        raise ValueError("--max-calls должен быть неотрицательным.")
    preview = args.command == "sample"
    if preview and args.batches < 1:
        raise ValueError("--batches должен быть положительным.")
    state = args.state or args.input.with_name(f".{args.input.name}.ai-translator.sqlite3")
    collect = args.command == "glossary"
    default_suffix = ".glossary.json" if collect else f".ru{args.input.suffix.lower()}"
    if preview:
        default_suffix = ".sample.ru.txt"
    output = args.output or args.input.with_name(args.input.stem + default_suffix)
    glossary_output = output if collect else getattr(args, "save_glossary", None)
    if preview and output.suffix.lower() != ".txt":
        raise ValueError("Выходной файл пробы должен иметь формат TXT.")
    if not collect and not preview and output.suffix.lower() != args.input.suffix.lower():
        raise ValueError("Выходной файл должен иметь тот же формат, что и исходный.")
    if glossary_output is not None and glossary_output.suffix.lower() != ".json":
        raise ValueError("Выходной глоссарий должен иметь расширение .json.")

    reads = [path for path in (args.input, args.config, args.glossary) if path is not None]
    if any(_same_path(state, path) for path in reads):
        raise ValueError("Кеш должен храниться отдельно от исходных файлов.")
    outputs = [output]
    if not collect and glossary_output is not None:
        outputs.append(glossary_output)
    _validate_paths(reads + [state], outputs, args.force)

    def progress(stage: str, current: int, total: int) -> None:
        print(f"{stage} {current}/{total}", file=sys.stderr, flush=True)

    with open_state(state) as db:
        requests = Requests(db, settings, args.max_calls)
        sample = [] if preview else None
        try:
            if collect or not args.no_auto_glossary:
                glossary_groups = groups[:args.batches] if preview else groups
                expanded = extract(requests, glossary_groups, glossary, progress)
                if expanded.keys() != glossary.keys() and not collect and not preview:
                    groups = list(batches(book, settings.batch_chars, tuple(expanded)))
                glossary = expanded
            if not collect:
                translate(requests, groups, glossary, args.edit, progress,
                          sample=sample, limit=args.batches if preview else None)
        finally:
            print(f"Запросов к модели: {requests.calls}. Ответов из кеша: {requests.hits}.", file=sys.stderr)
            if requests.backend is not None:
                usage = requests.backend.usage
                if usage.input or usage.output:
                    print(f"Токенов по данным провайдера: {usage.input} на входе "
                          f"({usage.cached_input} из кеша провайдера), {usage.output} на выходе.",
                          file=sys.stderr)
        writers = []
        if sample is not None:
            comparison = "\n\n".join(f"{original}\n{translated}" for original, translated in sample)
            writers.append((output, lambda path: path.write_text(
                comparison + ("\n" if comparison else ""), encoding="utf-8"
            )))
        elif not collect:
            writers.append((output, book.write))
        if glossary_output is not None:
            glossary_json = json.dumps(glossary, ensure_ascii=False, indent=2) + "\n"
            writers.append((glossary_output, lambda path: path.write_text(glossary_json, encoding="utf-8")))
        _write_outputs(writers, args.force)
        if not groups:
            print("Китайский текст для обработки не найден.", file=sys.stderr)
        for path in outputs:
            print(path)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        _run(args)
    except KeyboardInterrupt:
        print("Прервано. Повторите команду для продолжения из SQLite-кеша.", file=sys.stderr)
        return 130
    except (ValueError, OSError, LookupError, etree.XMLSyntaxError, sqlite3.Error, zipfile.BadZipFile) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
