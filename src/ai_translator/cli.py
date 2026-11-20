import argparse
import json
import re
import sqlite3
import sys
import zipfile
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryFile

from lxml import etree

from .config import DEFAULT_MODELS, load_config
from .console import configure_output
from .formats import chapter_revision, chapter_selection, read_book, read_chapters
from .paths import default_output
from .pipeline import (
    BudgetExceeded, CacheMiss, Proofreading, Requests, batches, default_state_path, expand_glossary, extract, load_glossary, open_state,
    open_state_readonly, read_request_budget, state_for_reading, translate,
)
from .provider import AuthenticationError, ProviderError
from .publication import check_publication, output_path, recover_outputs, validate_job_paths, write_outputs


def _chapter_reference(value: str) -> tuple[int, str | None]:
    number, separator, revision = value.partition("@")
    try:
        chapter = int(number)
    except ValueError:
        raise argparse.ArgumentTypeError("Укажите номер главы или идентификатор из оглавления.") from None
    if separator and not re.fullmatch(r"[0-9a-f]{16}", revision):
        raise argparse.ArgumentTypeError("Скопируйте идентификатор главы целиком из оглавления.")
    return chapter, revision if separator else None


def _read_chapter_file(path: Path) -> tuple[list[int], str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ValueError(f"Не удалось прочитать JSON выбора глав: {path}. {exc}") from exc
    if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1:
        raise ValueError("Версия файла выбора глав не поддерживается. Ожидается version: 1.")
    numbers, revision = data.get("chapters"), data.get("revision")
    if not isinstance(numbers, list) or not numbers or any(type(n) is not int or n < 1 for n in numbers):
        raise ValueError("Файл выбора глав должен содержать непустой список положительных целых номеров в chapters.")
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{64}", revision):
        raise ValueError("В файле выбора глав требуется полная ревизия оглавления в revision.")
    return numbers, revision


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Литературный перевод книг с китайского на русский.")
    commands = parser.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor", help="Проверить вход у провайдера без перевода.")
    doctor.add_argument("--provider", choices=DEFAULT_MODELS, default="codex")
    doctor.add_argument("--json-events", action="store_true", help="Вывод событий в формате JSON Lines.")
    for command, help_text in (
        ("translate", "Перевести TXT, EPUB или FB2."),
        ("sample", "Перевести начало книги и сохранить двуязычный TXT для оценки."),
        ("glossary", "Подготовить редактируемый JSON-глоссарий."),
        ("inspect", "Показать число блоков и пакетов без обращения к модели."),
    ):
        sub = commands.add_parser(command, help=help_text)
        sub.add_argument("--json-events", action="store_true", help="Вывод событий в формате JSON Lines.")
        sub.add_argument("input", type=Path, help="Исходный TXT, EPUB или FB2.")
        sub.add_argument("--config", type=Path, help="Файл параметров TOML.")
        sub.add_argument("--encoding", help="Явная кодировка входного TXT.")
        sub.add_argument("--txt-map", type=Path, help="JSON-карта ручных границ TXT.")
        sub.add_argument("--glossary", type=Path, help="Входной JSON с утверждёнными соответствиями.")
        sub.add_argument("--state", type=Path, help="Путь к SQLite-кешу ответов.")
        if command != "glossary":
            sub.add_argument("--no-auto-glossary", action="store_true", help="Пропустить сбор терминов.")
            sub.add_argument("--edit", action="store_true", help="Включить литературную редактуру.")
            review = sub.add_mutually_exclusive_group()
            review.add_argument("--review-edits", action="store_true",
                                help="Сверить черновик и редактуру с оригиналом. Требуется --edit.")
            review.add_argument("--repair-review", action="store_true",
                                help="Сверить все пакеты и разрешить исправление общих ошибок. Требуется --edit.")
        sub.add_argument("--provider", choices=DEFAULT_MODELS, help="Переопределить провайдера.")
        if command in ("sample", "inspect"):
            chapters = sub.add_mutually_exclusive_group()
            chapters.add_argument("--chapter", type=_chapter_reference, action="append", metavar="N[@РЕВИЗИЯ]",
                                  help="Выбрать главу по идентификатору из inspect или номеру. Можно повторить.")
            chapters.add_argument("--chapter-file", type=Path, metavar="JSON",
                                  help="Выбрать главы из JSON с полями version (1), chapters и revision.")
            sub.add_argument("--chapter-revision", help=argparse.SUPPRESS)
            sub.add_argument("--batches", type=int, help="Сколько пакетов взять для пробы по пакетам.")
            sub.add_argument("--start-batch", type=int, help="Номер первого пакета пробы по пакетам.")
        if command == "inspect":
            sub.add_argument("--for", dest="operation", choices=("translate", "sample", "glossary"),
                             default="translate", help="Действие для расчёта запросов.")
            sub.add_argument("--list-chapters", action="store_true", help="Показать только список глав без чтения кеша.")
            sub.add_argument("--sample-boundaries", action="store_true",
                             help="Показать только границы пробы без чтения кеша.")
            continue
        if command == "translate":
            sub.add_argument("--proofreading", action="store_true", help="Вывести адресную сводку после сохранения книги.")
        sub.add_argument("-o", "--output", type=Path, help="Путь к выходному файлу.")
        sub.add_argument("--max-calls", type=int, help="Максимум новых запросов за запуск.")
        total = sub.add_mutually_exclusive_group()
        total.add_argument("--max-total-calls", type=int, help="Сохранить общий лимит в SQLite. -1 снимает лимит.")
        total.add_argument("--tighten-total-calls", type=int,
                           help="Ограничить общий расход и сохранить более строгий потолок, если он уже есть.")
        override = sub.add_mutually_exclusive_group()
        override.add_argument("--force", action="store_true", help="Разрешить замену всех выходных файлов.")
        override.add_argument("--replace-existing", type=Path, action="append", default=[],
                              help="Разрешить замену только указанного выходного файла.")
        if command in ("translate", "sample"):
            sub.add_argument("--save-glossary", type=Path, help="Сохранить итоговый глоссарий в JSON.")
        if command == "sample":
            sub.add_argument("--sample-pairs", action="store_true",
                             help="Включить пары оригинала и перевода в поток --json-events.")
            sub.add_argument("--sample-versions", action="store_true",
                             help="Добавить в пары черновик, редактуру и выбор сверки. Требуются --sample-pairs и --edit.")
    return parser


def _run(args: argparse.Namespace) -> None:
    def report(event: str, message: str, *, error_stream: bool = False, **fields) -> None:
        if args.json_events:
            print(json.dumps({"event": event, "message": message, **fields}, ensure_ascii=False), flush=True)
        else:
            print(message, file=sys.stderr if error_stream else sys.stdout, flush=True)

    if args.command == "doctor":
        from .pipeline import provider_backend
        if provider_backend(args.provider, 20).check() is False:
            raise ProviderError("Antigravity не подтвердил вход при проверке без запроса к модели.")
        message = ("Переменная OPENAI_API_KEY задана. Проверка не обращалась к API."
                   if args.provider == "openai-api" else f"Вход через {args.provider} подтверждён.")
        report("authentication_ok", message)
        return

    sample_pairs = getattr(args, "sample_pairs", False)
    sample_versions = getattr(args, "sample_versions", False)
    if sample_pairs and not args.json_events:
        raise ValueError("--sample-pairs применяется вместе с --json-events.")
    if sample_versions and (not sample_pairs or not args.edit):
        raise ValueError("--sample-versions применяется вместе с --sample-pairs и --edit.")
    operation = args.operation if args.command == "inspect" else args.command
    preview = operation == "sample"
    list_chapters = getattr(args, "list_chapters", False)
    batch_range = getattr(args, "start_batch", None) is not None or getattr(args, "batches", None) is not None
    if batch_range and (not preview or list_chapters):
        raise ValueError("--start-batch и --batches применяются к sample и inspect --for sample без --list-chapters.")
    references = getattr(args, "chapter", None) or []
    chapter_file = getattr(args, "chapter_file", None)
    proofreading_enabled = getattr(args, "proofreading", False)
    boundaries_only = getattr(args, "sample_boundaries", False)
    if boundaries_only and (not preview or list_chapters):
        raise ValueError("--sample-boundaries применяется к inspect --for sample без --list-chapters.")
    expected_revision = getattr(args, "chapter_revision", None)
    chapter_flag = "--chapter-file" if chapter_file is not None else "--chapter"
    if references or chapter_file is not None:
        if list_chapters:
            raise ValueError(f"--list-chapters нельзя сочетать с {chapter_flag}.")
        if not preview:
            raise ValueError(f"{chapter_flag} применяется к sample и inspect --for sample.")
        if batch_range:
            raise ValueError(f"{chapter_flag} нельзя сочетать с --start-batch или --batches.")
    if expected_revision is not None and not references:
        raise ValueError("--chapter-revision применяется вместе с --chapter.")
    if chapter_file is not None:
        selected_numbers, expected_revision = _read_chapter_file(chapter_file)
    else:
        selected_numbers = [number for number, _ in references]
    numbers = tuple(sorted(set(selected_numbers)))
    chapter_number = numbers[0] if len(numbers) == 1 else numbers or None
    revisions = {revision for _, revision in references if revision is not None}
    if expected_revision is not None and revisions:
        raise ValueError("Укажите ревизию главы один раз.")
    with_chapters = list_chapters or bool(numbers)
    needs_revision = with_chapters or proofreading_enabled or args.txt_map is not None
    revision = chapter_revision(args.input, args.encoding, args.txt_map) if needs_revision else None
    if (expected_revision is not None and expected_revision != revision
            or revisions and revisions != {revision[:16]}):
        raise ValueError("Выбор главы устарел. Загрузите оглавление заново и выберите главу.")
    if list_chapters:
        chapters = read_chapters(args.input, args.encoding, args.txt_map)
    else:
        settings = load_config(args.config, getattr(args, "provider", None))
        book = read_book(args.input, args.encoding, with_chapters=with_chapters, txt_map=args.txt_map)
    if revision is not None and revision != chapter_revision(args.input, args.encoding, args.txt_map):
        raise ValueError("Книга или карта TXT изменилась при чтении. Повторите выбор главы.")
    if list_chapters:
        catalog = [{"number": number, "reference": f"{number}@{revision[:16]}",
                    "title": chapter.title, "units": chapter.unit_count}
                   for number, chapter in enumerate(chapters, 1)]
        message = ("\n".join(f"{entry['reference']} · {entry['title']} · блоков: {entry['units']}" for entry in catalog)
                   or "Главы по заголовкам не найдены. Можно выбрать пробу по пакетам.")
        report("chapters", message, chapters=catalog, revision=revision)
        return
    review_edits = getattr(args, "review_edits", False)
    repair_review = getattr(args, "repair_review", False)
    if (review_edits or repair_review) and (operation == "glossary" or not args.edit):
        flag = "--repair-review" if repair_review else "--review-edits"
        raise ValueError(f"{flag} применяется к переводу или пробе вместе с --edit.")
    chapter = None
    if numbers:
        if numbers[0] < 1 or numbers[-1] > len(book.chapters):
            raise ValueError(f"{chapter_flag} вне диапазона. Глав с китайским текстом: {len(book.chapters)}.")
        chapter = chapter_selection(book.chapters[number - 1] for number in numbers)
    glossary = load_glossary(args.glossary)
    spellings = expand_glossary(glossary)
    groups = list(batches(book, settings.batch_chars, tuple(spellings), chapter=chapter,
                          separate_gaps=len(numbers) > 1, addresses=proofreading_enabled))
    state = None if boundaries_only else args.state or default_state_path(args.input)
    if args.command == "inspect" and not boundaries_only:
        state = state_for_reading(state)
        if state.exists() and not state.is_file():
            raise ValueError(f"Для кеша нужен путь к файлу SQLite. {state}")
    offset = args.start_batch - 1 if preview and args.start_batch is not None else 0
    limit = (len(groups) if chapter is not None else
             args.batches if preview and args.batches is not None else 1)
    if preview and limit < 1:
        raise ValueError("--batches должен быть положительным.")
    if preview and (offset < 0 or offset >= max(1, len(groups))):
        raise ValueError(f"--start-batch вне диапазона. Пакетов: {len(groups)}.")

    def process_book(requests: Requests, progress, groups_ready=None, proofreading=None):
        work_groups = groups
        terms = glossary
        lookup = spellings
        sample = [] if preview else None
        if operation == "glossary" or not args.no_auto_glossary:
            glossary_groups = work_groups[offset:offset + limit] if preview else work_groups
            terms = extract(requests, glossary_groups, terms, progress, proofreading=proofreading)
            if terms.keys() != glossary.keys():
                lookup = expand_glossary(terms)
                if operation == "translate":
                    work_groups = list(batches(book, settings.batch_chars, tuple(lookup), addresses=proofreading_enabled))
        if groups_ready is not None:
            groups_ready(len(work_groups))
        if operation != "glossary":
            translate(requests, work_groups, lookup, args.edit, progress,
                      sample=sample, sample_versions=sample_versions, limit=limit if preview else None, start=offset,
                      review_edits=review_edits, repair_review=repair_review, proofreading=proofreading)
        return terms, sample

    if args.command == "inspect":
        units = chapter.unit_count if chapter is not None else sum(map(len, book.sections))
        initial_count = len(groups[offset:offset + limit]) if preview else len(groups)
        glossary_calls = initial_count if operation == "glossary" or not args.no_auto_glossary else 0
        group_label = "Пакетов до автоглоссария" if operation == "translate" and glossary_calls else "Пакетов"
        report("inspection", f"Блоков с китайским текстом: {units}. {group_label}: {len(groups)}.",
               units=units, batches=len(groups), operation=operation, selected_batches=initial_count,
               chapters=list(numbers))
        if preview and initial_count:
            start, stop = offset + 1, offset + initial_count
            first, last = groups[offset][0], groups[stop - 1][-1]
            opening = first.unit.visible_text(first.text).strip()[:100]
            ending = last.unit.visible_text(last.text).strip()[-100:]
            label = ""
            if len(numbers) == 1:
                label = f"Глава {chapter_number} · {chapter.title}\n"
            elif numbers:
                label = f"Выбрано глав: {len(numbers)}\n"
            report("sample_range", f"{label}Пакеты {start}–{stop}\nПервый блок · {opening}\nПоследний блок · {ending}",
                   first_batch=start, last_batch=stop, opening=opening, closing=ending)
        if boundaries_only:
            return
        models = {stage: {"model": model.name, "reasoning": model.reasoning}
                  for stage, model in settings.models.items()}
        choices = ", ".join(f"{stage}={model.name}/{model.reasoning or 'auto'}"
                            for stage, model in settings.models.items())
        report("settings", f"{settings.provider} · {choices}", provider=settings.provider, models=models)
        cached_count = cached_hits = 0
        resolved_groups = None

        def remember_groups(amount):
            nonlocal resolved_groups
            resolved_groups = amount

        request_budget = None
        first_missing = ("glossary" if glossary_calls else "translate") if groups else None
        if state.is_file():
            with open_state_readonly(state) as db:
                db.execute("BEGIN")
                table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='responses'").fetchone()
                cached_count = db.execute("SELECT COUNT(*) FROM responses").fetchone()[0] if table else 0
                request_budget = read_request_budget(db)
                if cached_count:
                    probe = Requests(db, settings, inspect_cache=True)
                    first_missing = None
                    try:
                        process_book(probe, lambda *_: None, remember_groups)
                    except CacheMiss as miss:
                        first_missing = miss.stage
                    cached_hits = probe.hits
        translated = 0 if operation == "glossary" else initial_count
        if operation == "translate" and resolved_groups is not None:
            translated = resolved_groups
        edit_calls = translated if args.edit else 0
        review_calls = edit_calls if review_edits or repair_review else 0
        provisional = operation == "translate" and bool(glossary_calls) and resolved_groups is None
        review_detail = ""
        if repair_review:
            review_detail = f", сверка с исправлениями: {review_calls}"
        elif review_edits:
            review_detail = f", сверка: до {review_calls}"
        estimate = "Предварительный план без кеша и повторов" if provisional else "Запросов без кеша и повторов"
        maximum = "до " if review_edits and not provisional else ""
        detail = " Разбиение может измениться после глоссария." if provisional else ""
        report("plan", f"{estimate}: {maximum}{glossary_calls + translated + edit_calls + review_calls} "
               f"(глоссарий: {glossary_calls}, перевод: {translated}, редактура: {edit_calls}{review_detail}).{detail}",
               glossary=glossary_calls, translate=translated, edit=edit_calls, review=review_calls,
               provisional=provisional, conditional_review=review_edits, repair_review=repair_review,
               auto_glossary=operation == "glossary" or not args.no_auto_glossary,
               edit_enabled=bool(edit_calls) or getattr(args, "edit", False) and operation != "glossary")
        report("cache", f"Ответов в базе кеша: {cached_count}.", cached_answers=cached_count)
        if first_missing is None:
            report("cache_replay", f"Кеш покрывает весь выбранный запуск. Будет использовано ответов: {cached_hits}.",
                   reusable_answers=cached_hits, complete=True, first_missing_stage=None)
        else:
            stage_labels = {"glossary": "глоссарий", "translate": "перевод", "edit": "редактура", "review": "сверка"}
            report("cache_replay", f"До первого нового запроса будет использовано ответов из кеша: "
                   f"{cached_hits}. Следующий этап · {stage_labels[first_missing]}.",
                   reusable_answers=cached_hits, complete=False, first_missing_stage=first_missing)
        if request_budget is not None:
            attempts, ceiling = request_budget
            report("request_budget", f"Всего учтённых запросов: {attempts}. Общий лимит: "
                   f"{ceiling if ceiling is not None else 'не задан'}.", attempts=attempts, ceiling=ceiling)
        return

    if args.max_calls is not None and args.max_calls < 0:
        raise ValueError("--max-calls должен быть неотрицательным.")
    if args.max_total_calls is not None and not -1 <= args.max_total_calls <= 2**63 - 1:
        raise ValueError("--max-total-calls должен быть неотрицательным целым числом или -1.")
    if args.tighten_total_calls is not None and not 0 <= args.tighten_total_calls <= 2**63 - 1:
        raise ValueError("--tighten-total-calls должен быть неотрицательным целым числом.")
    collect = args.command == "glossary"
    output = args.output or default_output(args.input, args.command, chapter=chapter_number)
    glossary_output = output if collect else getattr(args, "save_glossary", None)
    if preview and output.suffix.lower() != ".txt":
        raise ValueError("Выходной файл пробы должен иметь формат TXT.")
    if not collect and not preview and output.suffix.lower() != args.input.suffix.lower():
        raise ValueError("Выходной файл должен иметь тот же формат, что и исходный.")
    if glossary_output is not None and glossary_output.suffix.lower() != ".json":
        raise ValueError("Выходной глоссарий должен иметь расширение .json.")

    reads = [path for path in (args.input, args.config, args.glossary, args.txt_map, chapter_file) if path is not None]
    outputs = [output]
    if not collect and glossary_output is not None:
        outputs.append(glossary_output)
    validate_job_paths(reads, state, outputs, True)
    replacements = {output_path(path) for path in args.replace_existing}
    if replacements - {output_path(path) for path in outputs}:
        raise ValueError("--replace-existing должен указывать один из выходных файлов.")
    overwrite = True if args.force else replacements
    recover_outputs(outputs)
    validate_job_paths(reads, state, outputs, overwrite)
    check_publication(outputs, overwrite)

    def progress(stage: str, current: int, total: int) -> None:
        report("progress", f"{stage} {current}/{total}", error_stream=True,
               stage=stage, current=current, total=total)

    with ExitStack() as stack:
        db = stack.enter_context(open_state(state))
        proofreading = spool = None
        if proofreading_enabled:
            spool = stack.enter_context(TemporaryFile(mode="w+t", encoding="utf-8"))
            proofreading = Proofreading(lambda row: spool.write(json.dumps(row, ensure_ascii=False) + "\n"))
        update_limit = args.max_total_calls is not None or args.tighten_total_calls is not None
        if update_limit and args.json_events:
            report("request_budget_update", "Запись общего лимита…")
        requests = Requests(db, settings, args.max_calls, max_total_calls=args.max_total_calls,
                            tighten_total_calls=args.tighten_total_calls, report_rejections=proofreading_enabled)
        initial_attempts = requests.total_calls
        if args.tighten_total_calls is not None:
            budget_message = "Применён наиболее строгий общий лимит."
        elif update_limit:
            budget_message = "Изменение общего лимита сохранено."
        else:
            budget_message = f"Ранее учтённых запросов · {initial_attempts}."
        report("request_budget", budget_message, error_stream=True,
               attempts=initial_attempts, ceiling=requests.total_limit, changed=update_limit, phase="start")
        try:
            glossary, sample = process_book(requests, progress, proofreading=proofreading)
        finally:
            try:
                budget = read_request_budget(db)
            except sqlite3.Error:
                budget = None
            attempts, ceiling = budget if budget is not None else (requests.total_calls, requests.total_limit)
            calls = attempts - initial_attempts if budget is not None else requests.calls
            report("requests", f"Запросов к модели: {calls}. Ответов из кеша: {requests.hits}.",
                   error_stream=True, calls=calls, cache_hits=requests.hits)
            label = f"Запросов в базе · {attempts}" + (f" / {ceiling}" if ceiling is not None else "")
            report("request_budget", label, error_stream=True, attempts=attempts, ceiling=ceiling, phase="final")
            if requests.backend is not None:
                usage = requests.backend.usage
                if usage.input or usage.output:
                    report("usage", f"Токенов по данным провайдера: {usage.input} на входе "
                           f"({usage.cached_input} из кеша провайдера), {usage.output} на выходе.",
                           error_stream=True, input_tokens=usage.input, output_tokens=usage.output,
                           cached_input_tokens=usage.cached_input)
        writers = []
        if sample is not None:
            comparison = "\n\n".join(f"{pair.original}\n{pair.translation}" for pair in sample)
            writers.append((output, lambda path: path.write_text(
                comparison + ("\n" if comparison else ""), encoding="utf-8"
            )))
        elif not collect:
            writers.append((output, book.write))
        if glossary_output is not None:
            glossary_json = json.dumps(glossary, ensure_ascii=False, indent=2) + "\n"
            writers.append((glossary_output, lambda path: path.write_text(glossary_json, encoding="utf-8")))
        write_outputs(writers, overwrite)
        if not groups:
            report("notice", "Китайский текст для обработки не найден.", error_stream=True)
        if sample_pairs:
            if glossary:
                report("sample_glossary", "", glossary=glossary)
            for pair in sample:
                versions = ({"draft": pair.draft, "edited": pair.edited, "review_choice": pair.review_choice}
                            if sample_versions else {})
                report("sample_pair", "", original=pair.original, translation=pair.translation, **versions)
        for path in outputs:
            report("output", str(path), path=str(path.resolve()))
        if proofreading is not None:
            report("proofreading", f"Вычитка: замечаний {proofreading.findings}, "
                   f"сверено частей {proofreading.checked} из {proofreading.parts}.",
                   revision=revision, source=str(args.input.resolve()), path=str(output.resolve()),
                   parts=proofreading.parts, checked=proofreading.checked, findings=proofreading.findings,
                   review_mode="repair" if repair_review else "changes" if review_edits else "off")
            spool.seek(0)
            for line in spool:
                row = json.loads(line)
                address = f"{row['section']}/{row['block']}/{row['part']}"
                reason = " ".join(row["reason"].split())
                report("proofreading_item", f"{address} · {row['stage']} · {reason} · {row['original_excerpt']}",
                       revision=revision, **row)


def main(argv: list[str] | None = None) -> int:
    configure_output()
    args = _parser().parse_args(argv)

    def failure(event: str, message: str) -> None:
        if args.json_events:
            print(json.dumps({"event": event, "message": message}, ensure_ascii=False), flush=True)
        else:
            print(message, file=sys.stderr)

    try:
        _run(args)
    except KeyboardInterrupt:
        failure("cancelled", "Прервано. Повторите команду для продолжения из SQLite-кеша.")
        return 130
    except BudgetExceeded as exc:
        failure("budget", str(exc))
        return 3
    except AuthenticationError as exc:
        failure("authentication", str(exc))
        return 4
    except (ValueError, OSError, LookupError, etree.XMLSyntaxError, sqlite3.Error, zipfile.BadZipFile) as exc:
        failure("error", f"Ошибка: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
