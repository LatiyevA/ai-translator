"""Native desktop frontend for the shared CLI translation pipeline."""

import argparse
import csv
import io
import json
import os
import signal
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from .config import STAGES, ModelSettingError, Settings, load_config, parse_config
from .console import configure_output
from .formats import chapter_revision
from .paths import default_output
from .pipeline import SamplePair, default_state_path, load_glossary, parse_glossary, read_request_budget, state_for_reading
from .publication import validate_job_paths, write_outputs
from .windows_job import taskkill_command

try:
    from PySide6.QtCore import QProcess, QProcessEnvironment, QSettings, QTimer, Qt, QUrl
    from PySide6.QtGui import QDesktopServices
    from PySide6.QtWidgets import (
        QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QGridLayout, QHBoxLayout,
        QHeaderView, QTableWidget, QTableWidgetItem,
        QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar,
        QPushButton, QScrollArea, QSpinBox, QSplitter, QTabWidget, QVBoxLayout, QWidget,
    )
except ModuleNotFoundError as exc:
    if exc.name not in {"PySide6", "shiboken6"}:
        raise
    QApplication = None
    QMainWindow = object
    QDialog = object
    QWidget = object
    QT_ERROR = ("Установите зависимости десктопного приложения. В репозитории выполните uv sync --extra desktop. "
                "При установке из wheel добавьте к имени пакета [desktop].")
except ImportError as exc:
    QApplication = None
    QMainWindow = object
    QDialog = object
    QWidget = object
    QT_ERROR = f"Не удалось загрузить системные библиотеки Qt. {exc}"

OPERATIONS = {"translate": "Перевести", "sample": "Перевести пробу", "glossary": "Собрать глоссарий"}
PROVIDERS = {
    "codex": "ChatGPT через Codex",
    "claude-code": "Claude Code",
    "openai-api": "OpenAI API",
    "antigravity": "Google через Antigravity",
}
FORMATS = "Книги (*.txt *.epub *.fb2)"
STAGE_LABELS = ("Глоссарий", "Перевод", "Редактура", "Сверка")
EXTRA_MODELS = {
    "codex": ("gpt-6.2-astra", "gpt-6.1-sol", "gpt-6.1-terra", "gpt-6-luna"),
    "claude-code": ("opus",),
    "openai-api": ("gpt-6.2-astra", "gpt-6.1-sol"),
    "antigravity": ("gemini-4-argon",),
}
EFFORT_OPTIONS = {
    "codex": ("low", "medium", "high", "xhigh", "max", "ultra"),
    "claude-code": ("low", "medium", "high", "xhigh", "max", "ultracode"),
    "openai-api": ("none", "minimal", "low", "medium", "high", "xhigh", "max"),
    "antigravity": ("low", "medium", "high", "xhigh", "max"),
}
AUTO_EFFORT = "Авто"


def _set_combo_value(combo, value: str) -> None:
    index = combo.findText(value)
    combo.setCurrentIndex(index)
    if index < 0:
        combo.setEditText(value)


def config_text(provider: str, values: dict) -> str:
    rows = [f"provider = {json.dumps(provider)}"]
    for key in ("fandom", "style", "batch_chars", "timeout"):
        rows.append(f"{key} = {json.dumps(values[key], ensure_ascii=False)}")
    for stage, (model, effort) in zip(STAGES, values["models"]):
        rows.extend(("", f"[{stage}]", f"model = {json.dumps(model, ensure_ascii=False)}",
                     f"reasoning = {json.dumps(effort, ensure_ascii=False)}"))
    content = "\n".join(rows).replace("\x7f", "\\u007f") + "\n"
    parse_config(content)
    return content


def _confirm_replace(parent, title: str, paths: str) -> bool:
    dialog = QMessageBox(parent)
    dialog.setIcon(QMessageBox.Icon.Question)
    dialog.setWindowTitle(title)
    dialog.setTextFormat(Qt.TextFormat.PlainText)
    dialog.setText(paths)
    dialog.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
    dialog.setDefaultButton(QMessageBox.StandardButton.Cancel)
    try:
        return dialog.exec() == QMessageBox.StandardButton.Yes.value
    finally:
        dialog.deleteLater()


class SamplePreview(QWidget):
    EMPTY = "Запустите «Перевести пробу»."

    def __init__(self):
        super().__init__()
        self.pairs: list[SamplePair] = []
        layout = QVBoxLayout(self)
        self.filename = QLabel(self.EMPTY)
        self.filename.setTextFormat(Qt.TextFormat.PlainText)
        self.filename.setWordWrap(True)
        layout.addWidget(self.filename)

        self.version_row = QWidget()
        version_layout = QHBoxLayout(self.version_row)
        version_layout.setContentsMargins(0, 0, 0, 0)
        version_layout.addWidget(QLabel("Версия перевода"))
        self.version = QComboBox()
        self.version.setAccessibleName("Версия перевода")
        self.version.addItem("Итог в файле", "translation")
        self.version.addItem("Черновик", "draft")
        self.version.addItem("Редактура", "edited")
        version_layout.addWidget(self.version, 1)
        layout.addWidget(self.version_row)

        self.review_choice = QLabel()
        self.review_choice.setTextFormat(Qt.TextFormat.PlainText)
        self.review_choice.setWordWrap(True)
        layout.addWidget(self.review_choice)

        columns = QSplitter(Qt.Orientation.Horizontal)
        columns.setChildrenCollapsible(False)
        self.original = self._pane(columns, "Оригинал")
        self.translation = self._pane(columns, "Перевод")
        columns.setSizes([1, 1])
        layout.addWidget(columns, 1)

        navigation = QHBoxLayout()
        self.previous = QPushButton("Назад")
        self.next = QPushButton("Вперёд")
        self.position = QSpinBox()
        self.position.setAccessibleName("Номер фрагмента пробы")
        self.position.setSpecialValueText("Нет фрагментов")
        self.position.setRange(0, 0)
        navigation.addWidget(self.previous)
        navigation.addWidget(self.position, 1)
        navigation.addWidget(self.next)
        layout.addLayout(navigation)
        self.previous.clicked.connect(self.position.stepDown)
        self.next.clicked.connect(self.position.stepUp)
        self.position.valueChanged.connect(self._show_pair)
        self.version.currentIndexChanged.connect(self._show_version)
        self._show_pair(0)

    @staticmethod
    def _pane(columns, title):
        pane = QWidget()
        layout = QVBoxLayout(pane)
        layout.setContentsMargins(0, 0, 0, 0)
        label = QLabel(title)
        text = QPlainTextEdit(readOnly=True)
        text.setAccessibleName(title)
        layout.addWidget(label)
        layout.addWidget(text, 1)
        columns.addWidget(pane)
        return text

    def set_pairs(self, path: Path | None, pairs: list[SamplePair]) -> None:
        self.filename.setText(path.name if path is not None else self.EMPTY)
        self.filename.setToolTip(str(path) if path is not None else "")
        self.pairs = pairs
        self.position.setSuffix(f" из {len(pairs)}" if pairs else "")
        self.position.setSpecialValueText("" if pairs else "Нет фрагментов")
        self.position.setRange(1 if pairs else 0, len(pairs))
        self.position.setValue(self.position.minimum())
        self.version.setCurrentIndex(0)
        self._show_pair(self.position.value())

    def _show_pair(self, index):
        pair = self._current_pair()
        self.original.setPlainText(pair.original if pair else "")
        self.version_row.setVisible(pair is not None and pair.draft is not None)
        decision = {"draft": "Сверка выбрала черновик.", "edited": "Сверка выбрала редактуру.",
                    "repaired": "Сверка исправила текст для итоговой версии."}
        label = decision.get(pair.review_choice, "") if pair else ""
        self.review_choice.setText(label)
        self.review_choice.setVisible(bool(label))
        self.position.setEnabled(bool(self.pairs))
        self.previous.setEnabled(index > 1)
        self.next.setEnabled(index < len(self.pairs))
        self._show_version()

    def _current_pair(self) -> SamplePair | None:
        index = self.position.value()
        return self.pairs[index - 1] if 1 <= index <= len(self.pairs) else None

    def _show_version(self) -> None:
        pair = self._current_pair()
        text = getattr(pair, self.version.currentData()) if pair else ""
        self.translation.setPlainText(pair.translation if text is None else text)


class GlossaryDialog(QDialog):
    def __init__(self, path: Path | None, parent, *, protected: list[Path] | None = None):
        super().__init__(parent)
        self.setWindowTitle("Глоссарий")
        self.resize(970, 450)
        self.path = path
        self.protected = protected or []
        self.saved_path = None
        data = load_glossary(path)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Оригинал", "Варианты оригинала", "Перевод", "Допустимые формы", "Проверять"])
        header = self.table.horizontalHeader()
        for column in range(4):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setAccessibleName("Термины глоссария")
        for key, entry in data.items():
            if isinstance(entry, dict):
                self._add_row(key, entry["ru"], self._format_list(entry["forms"]), entry.get("strict", True),
                              aliases=self._format_list(entry.get("aliases", [])))
            else:
                self._add_row(key, entry)
        self.message = QLabel("")
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        self.message.setWordWrap(True)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Найти термин или перевод")
        self.search.setAccessibleName("Поиск в глоссарии")
        self.search.textChanged.connect(self._filter)
        self.add_term = QPushButton("Добавить строку")
        self.delete_term = QPushButton("Удалить выбранные")
        self.save = QPushButton("Сохранить…")
        cancel = QPushButton("Отмена")
        self.add_term.clicked.connect(self._new_term)
        self.delete_term.clicked.connect(self._delete_rows)
        self.save.clicked.connect(self._save)
        cancel.clicked.connect(self.reject)
        buttons = QHBoxLayout()
        for button in (self.add_term, self.delete_term, self.save, cancel):
            buttons.addWidget(button)
        layout = QVBoxLayout(self)
        guide = QLabel("Варианты оригинала и допустимые формы разделяются точкой с запятой. Флажок включает проверку форм.")
        guide.setWordWrap(True)
        layout.addWidget(guide)
        layout.addWidget(self.search)
        layout.addWidget(self.table)
        layout.addWidget(self.message)
        layout.addLayout(buttons)

    @staticmethod
    def _format_list(items):
        if not items:
            return ""
        value = io.StringIO()
        csv.writer(value, delimiter=";").writerow(items)
        return value.getvalue().removesuffix("\r\n")

    @staticmethod
    def _parse_list(value, label, term):
        rows = list(csv.reader(io.StringIO(value), delimiter=";", strict=True)) if value else [[]]
        if len(rows) != 1:
            raise ValueError(f"Поле «{label}» должно занимать одну строку для термина {term!r}.")
        return rows[0]

    def _add_row(self, term="", translation="", forms="", strict=False, *, aliases=""):
        row = self.table.rowCount()
        self.table.insertRow(row)
        for column, value in enumerate((term, aliases, translation, forms)):
            self.table.setItem(row, column, QTableWidgetItem(value))
        item = QTableWidgetItem()
        item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsSelectable)
        item.setCheckState(Qt.CheckState.Checked if strict else Qt.CheckState.Unchecked)
        self.table.setItem(row, 4, item)
        self.table.setCurrentCell(row, 0)

    def _delete_rows(self):
        for row in sorted({index.row() for index in self.table.selectedIndexes()}, reverse=True):
            self.table.removeRow(row)

    def _new_term(self):
        self.search.clear()
        self._add_row()
        self.table.editItem(self.table.currentItem())

    def _filter(self, query: str):
        query = query.strip().casefold()
        self.table.clearSelection()
        for row in range(self.table.rowCount()):
            visible = any(query in self.table.item(row, column).text().casefold() for column in range(4))
            self.table.setRowHidden(row, not visible)

    def _content(self) -> str:
        data = {}
        for row in range(self.table.rowCount()):
            term, aliases, translation, forms = (self.table.item(row, column).text().strip() for column in range(4))
            strict = self.table.item(row, 4).checkState() == Qt.CheckState.Checked
            if not (term or aliases or translation or forms or strict):
                continue
            if not term or not translation:
                raise ValueError(f"Укажите оригинал и перевод в строке {row + 1}.")
            if term in data:
                raise ValueError(f"Термин {term!r} встречается несколько раз.")
            if forms and not strict:
                raise ValueError(f"Включите проверку форм в строке {row + 1}.")
            if aliases or strict:
                entry = {"ru": translation, "forms": self._parse_list(forms, "Допустимые формы", term)}
                if aliases:
                    entry["aliases"] = self._parse_list(aliases, "Варианты оригинала", term)
                if not strict:
                    entry["strict"] = False
                data[term] = entry
            else:
                data[term] = translation
        return json.dumps(parse_glossary(json.dumps(data, ensure_ascii=False)), ensure_ascii=False, indent=2) + "\n"

    def _save(self):
        try:
            content = self._content()
            filename, _ = QFileDialog.getSaveFileName(
                self, "Сохранить глоссарий", str(self.path or "glossary.json"), "JSON (*.json)",
                options=QFileDialog.Option.DontConfirmOverwrite,
            )
            if not filename:
                return
            path = Path(filename)
            if not path.suffix:
                path = path.with_suffix(".json")
            if path.suffix.lower() != ".json":
                raise ValueError("Выберите обычный файл с расширением .json.")
            validate_job_paths(self.protected, None, [path], force=True)
            force = path.exists() or path.is_symlink()
            if force and not _confirm_replace(self, "Заменить глоссарий?", str(path)):
                return
            write_outputs([(path, lambda target: target.write_text(content, encoding="utf-8"))], force=force)
        except (ValueError, OSError, csv.Error) as exc:
            self.message.setText(f"Не удалось сохранить глоссарий. {exc}")
            return
        self.saved_path = path
        self.accept()


class Window(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Литературный перевод")
        self.resize(1120, 710)
        self.setMinimumSize(730, 590)
        self.setAcceptDrops(True)
        self.preferences = QSettings("AI Translator", "Desktop")
        self.process = None
        self.job_config = None
        self.job_state = None
        self.job_snapshot = None
        self.budget_final_received = False
        self.limit_tighten_only = False
        self.worker_group = None
        self.provider_key = None
        self.cancel_requested = False
        self.close_when_finished = False
        self.last_output = None
        self.last_output_selection = None
        self.pending_output = None
        self.pending_sample = []
        self.output_paths = {}
        self.glossary_paths = {}
        self.output_selection = None
        self.worker_success = ""
        self.worker_error = ""
        self.stdout = bytearray()

        workspace = QWidget()
        outer = QVBoxLayout(workspace)
        outer.setContentsMargins(24, 20, 24, 18)
        outer.setSpacing(12)
        heading = QLabel("Литературный перевод")
        font = heading.font()
        font.setPointSize(font.pointSize() + 10)
        font.setBold(True)
        heading.setFont(font)
        outer.addWidget(heading)
        outer.addWidget(QLabel("Китайский → русский"))

        splitter = QSplitter(Qt.Orientation.Horizontal)
        outer.addWidget(splitter, 1)
        self.options = QTabWidget()
        splitter.addWidget(self.options)
        setup = QWidget()
        self._add_form_tab(setup, "Книга")
        form = QFormLayout(setup)
        form.setContentsMargins(14, 16, 14, 14)
        form.setSpacing(10)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)

        self.source = self._path_field(form, "Исходная книга", FORMATS)
        self.restore_run = QPushButton("Восстановить")
        self.forget_run = QPushButton("Забыть")
        self.restore_run.setAccessibleName("Восстановить параметры последнего запуска")
        self.forget_run.setAccessibleName("Забыть параметры последнего запуска")
        self.restore_run.clicked.connect(self._restore_last_run)
        self.forget_run.clicked.connect(self._forget_last_run)
        last_run_actions = QHBoxLayout()
        last_run_actions.addWidget(self.restore_run)
        last_run_actions.addWidget(self.forget_run)
        form.addRow("Последний запуск", last_run_actions)
        self.operation = QComboBox()
        for key, label in OPERATIONS.items():
            self.operation.addItem(label, key)
        form.addRow("Действие", self.operation)
        self.output = self._path_field(form, "Сохранить в", "", save=True)
        self.glossary = self._path_field(form, "Готовый глоссарий", "JSON (*.json)")
        self.edit_glossary = QPushButton("Редактировать глоссарий…")
        self.edit_glossary.clicked.connect(self._edit_glossary)
        form.addRow(self.edit_glossary)
        self.save_glossary = self._path_field(form, "Сохранить глоссарий", "JSON (*.json)", save=True)
        self.provider = QComboBox()
        for key, label in PROVIDERS.items():
            self.provider.addItem(label, key)
        form.addRow("Провайдер", self.provider)
        self.provider_hint = QLabel()
        self.provider_hint.setTextFormat(Qt.TextFormat.PlainText)
        self.provider_hint.setWordWrap(True)
        form.addRow(self.provider_hint)
        self.auto_glossary = QCheckBox("Собирать термины автоматически")
        self.auto_glossary.setChecked(True)
        form.addRow(self.auto_glossary)
        self.edit = QCheckBox("Включить литературную редактуру")
        form.addRow(self.edit)
        self.review_edits = QCheckBox("Сверять редактуру с оригиналом")
        self.edit.toggled.connect(self._update_form)
        self.review_edits.toggled.connect(self._update_form)
        form.addRow(self.review_edits)
        self.repair_review = QCheckBox("Исправлять общие ошибки при сверке")
        self.repair_review.setToolTip("Проверяет каждый пакет и может предложить исправленный текст. "
                                      "Без кеша обычно нужен запрос на пакет. При ошибке возможен повтор.")
        self.repair_review.toggled.connect(self._update_form)
        form.addRow(self.repair_review)
        self.chapter = QComboBox()
        self.chapter_revision = None
        self.chapter.addItem("По пакетам", None)
        self.chapter.setAccessibleName("Глава для пробы")
        self.chapter.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.chapter.setMinimumContentsLength(12)
        self.load_chapters = QPushButton("Загрузить оглавление")
        self.load_chapters.clicked.connect(lambda: self.start("chapters"))
        chapter_row = QHBoxLayout()
        chapter_row.addWidget(self.chapter, 1)
        chapter_row.addWidget(self.load_chapters)
        form.addRow("Глава для пробы", chapter_row)
        self.batches = self._spin(form, "Пакетов в пробе", 1, 1000, 1)
        self.start_batch = self._spin(form, "Первый пакет пробы", 1, 100000, 1)
        self.budget = self._spin(form, "Лимит запросов на запуск", -1, 100000, 20)
        self.budget.setSpecialValueText("Без лимита")
        self.budget.setToolTip("0 позволяет использовать только кеш. Лимит учитывает исправления.")
        self.cache = self._path_field(form, "Файл кеша", "База SQLite (*.sqlite3 *.sqlite *.db);;Все файлы (*)")
        self.cache.setPlaceholderText("Рядом с исходником")
        self.cache.setToolTip("Выберите существующий кеш или введите путь к новому файлу.")
        self.total_budget_mode = QComboBox()
        self.total_budget_mode.addItems(["Из кеша", "Установить", "Снять"])
        self.total_budget = QSpinBox()
        self.total_budget.setRange(0, 1000000)
        self.total_budget.setValue(100)
        self.total_budget.setAccessibleName("Общий лимит запросов")
        self.total_budget.setEnabled(False)
        self.total_budget_mode.currentIndexChanged.connect(lambda index: self.total_budget.setEnabled(index == 1))
        self.total_budget_mode.currentIndexChanged.connect(lambda _: self._use_stricter_limit(False))
        self.total_budget.valueChanged.connect(lambda _: self._use_stricter_limit(False))
        self.source.textChanged.connect(lambda: self.total_budget_mode.setCurrentIndex(0))
        self.cache.textChanged.connect(lambda: self.total_budget_mode.setCurrentIndex(0))
        budget_row = QHBoxLayout()
        budget_row.addWidget(self.total_budget_mode)
        budget_row.addWidget(self.total_budget)
        form.addRow("Общий лимит запросов", budget_row)
        self.encoding = QLineEdit()
        self.encoding.setPlaceholderText("Автоопределение")
        form.addRow("Кодировка TXT", self.encoding)

        model_page = QWidget()
        self._add_form_tab(model_page, "Модели и стиль")
        model_form = QFormLayout(model_page)
        model_form.setContentsMargins(14, 16, 14, 14)
        model_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        stage_grid = QGridLayout()
        stage_grid.setColumnStretch(0, 3)
        stage_grid.setColumnStretch(1, 2)
        stage_grid.addWidget(QLabel("Модель"), 0, 0)
        stage_grid.addWidget(QLabel("Reasoning"), 0, 1)
        self.models = []
        for row, stage in enumerate(STAGE_LABELS):
            model, effort = QComboBox(), QComboBox()
            for editor in (model, effort):
                editor.setEditable(True)
                editor.lineEdit().setMaxLength(2**31 - 1)
                editor.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
                editor.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            model.setMinimumContentsLength(13)
            effort.setMinimumContentsLength(7)
            model.setAccessibleName(f"{stage} — модель")
            effort.setAccessibleName(f"{stage} — reasoning")
            model.setToolTip("Выберите модель или введите идентификатор, доступный вашему аккаунту.")
            effort.setToolTip("Доступные уровни зависят от модели. «Авто» оставляет выбор провайдеру.")
            stage_grid.addWidget(QLabel(stage), row * 2 + 1, 0, 1, 2)
            stage_grid.addWidget(model, row * 2 + 2, 0)
            stage_grid.addWidget(effort, row * 2 + 2, 1)
            self.models.append((model, effort))
        model_form.addRow(stage_grid)
        self.reset_models = QPushButton("Модели и reasoning по умолчанию")
        self.reset_models.clicked.connect(self._reset_models)
        model_form.addRow(self.reset_models)
        self.fandom = QLineEdit()
        self.fandom.setMaxLength(2**31 - 1)
        self.style = QPlainTextEdit()
        self.imported_style = None
        self.style.textChanged.connect(lambda: setattr(self, "imported_style", None))
        self.style.setPlaceholderText("Например, живой диалог и сдержанный тон")
        self.style.setMaximumHeight(105)
        model_form.addRow("Фэндом", self.fandom)
        model_form.addRow("Стиль", self.style)
        self.batch_size = self._spin(model_form, "Символов в пакете", 400, 12000, 2400)
        self.timeout = QLineEdit()
        self.timeout.setAccessibleName("Тайм-аут запроса, с")
        self.timeout.setInputMethodHints(Qt.InputMethodHint.ImhDigitsOnly)
        model_form.addRow("Тайм-аут запроса, с", self.timeout)
        profile_actions = QHBoxLayout()
        self.load_profile = QPushButton("Загрузить TOML…")
        self.save_profile = QPushButton("Сохранить TOML…")
        self.load_profile.clicked.connect(self._choose_profile)
        self.save_profile.clicked.connect(self._export_profile)
        profile_actions.addWidget(self.load_profile)
        profile_actions.addWidget(self.save_profile)
        model_form.addRow(profile_actions)

        self.results = QTabWidget()
        splitter.addWidget(self.results)
        splitter.setSizes([490, 570])
        self.log = QPlainTextEdit(readOnly=True)
        self.log.setAccessibleName("Журнал перевода")
        self.log.setMaximumBlockCount(1000)
        self.preview = SamplePreview()
        self.preview.setAccessibleName("Двуязычная проба")
        self.results.addTab(self.log, "Журнал")
        self.results.addTab(self.preview, "Проба")

        self.status = QLabel("Выберите книгу TXT, EPUB или FB2.")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        outer.addWidget(self.status)
        self.budget_status = QLabel()
        self.budget_status.setTextFormat(Qt.TextFormat.PlainText)
        self.budget_status.hide()
        self.source.textChanged.connect(self.budget_status.hide)
        self.cache.textChanged.connect(self.budget_status.hide)
        outer.addWidget(self.budget_status)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        outer.addWidget(self.progress)
        buttons = QHBoxLayout()
        self.inspect = QPushButton("Оценить запросы")
        self.run = QPushButton("Перевести")
        self.run.setDefault(True)
        self.stop = QPushButton("Остановить")
        self.stop.setDisabled(True)
        self.open_result = QPushButton("Открыть результат")
        self.open_result.setDisabled(True)
        self.check_login = QPushButton("Проверить вход")
        buttons.addWidget(self.check_login)
        buttons.addWidget(self.inspect)
        buttons.addWidget(self.run)
        buttons.addWidget(self.stop)
        buttons.addStretch()
        buttons.addWidget(self.open_result)
        outer.addLayout(buttons)

        self.setCentralWidget(workspace)
        self.edit.setChecked(self.preferences.value("edit", False, type=bool))
        self.review_edits.setChecked(self.preferences.value("reviewEdits", False, type=bool))
        self.repair_review.setChecked(self.preferences.value("repairReview", False, type=bool))
        self.auto_glossary.setChecked(self.preferences.value("autoGlossary", True, type=bool))
        self.fandom.setText(self.preferences.value("fandom", ""))
        self._set_style(self.preferences.value("style", ""))
        self.budget.setValue(self.preferences.value("budget", 20, type=int))
        self.batch_size.setValue(self.preferences.value("batchSize", 2400, type=int))
        self.timeout.setText(self.preferences.value("timeout", "300", type=str))
        self.provider.setCurrentIndex(max(0, self.provider.findData(self.preferences.value("provider", "codex"))))
        self._provider_changed()
        self.provider.currentIndexChanged.connect(self._provider_changed)
        self.source.textChanged.connect(self._update_form)
        self.operation.currentIndexChanged.connect(self._update_form)
        self.chapter.currentIndexChanged.connect(self._update_form)
        self.start_batch.valueChanged.connect(self._update_form)
        self.batches.valueChanged.connect(self._update_form)
        self.batch_size.valueChanged.connect(self._update_form)
        self.encoding.textChanged.connect(self._update_form)
        self.run.clicked.connect(lambda: self.start(self.operation.currentData()))
        self.inspect.clicked.connect(lambda: self.start("inspect"))
        self.stop.clicked.connect(self.cancel)
        self.open_result.clicked.connect(self._open_result)
        self.check_login.clicked.connect(lambda: self.start("doctor"))
        self._update_last_run()
        self._update_form()

    def _add_form_tab(self, page, label):
        scroll = QScrollArea()
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.setWidget(page)
        self.options.addTab(scroll, label)

    def _path_field(self, form, label, filters, *, save=False):
        field = QLineEdit()
        field.setClearButtonEnabled(True)
        field.setAccessibleName(label)
        button = QPushButton("Обзор…")

        def browse():
            initial = field.text() or field.placeholderText()
            selection = filters
            suffix = ".json" if filters else Path(field.placeholderText()).suffix
            if save and not selection:
                selection = f"{suffix[1:].upper()} (*{suffix})" if suffix else FORMATS
            dialog = QFileDialog.getSaveFileName if save else QFileDialog.getOpenFileName
            filename, _ = dialog(self, label, initial, selection)
            if filename:
                if save and suffix and not Path(filename).suffix:
                    filename += suffix
                field.setText(filename)

        button.clicked.connect(browse)
        row = QHBoxLayout()
        row.addWidget(field, 1)
        row.addWidget(button)
        form.addRow(label, row)
        return field

    @staticmethod
    def _spin(form, label, minimum, maximum, value):
        spin = QSpinBox()
        spin.setRange(minimum, maximum)
        spin.setValue(value)
        form.addRow(label, spin)
        return spin

    def _provider_changed(self):
        if self.provider_key is not None:
            self._store_models()
        self.provider_key = self.provider.currentData()
        self.provider_hint.setText({
            "codex": "Подписка ChatGPT · для входа выполните codex login.",
            "claude-code": "Подписка Claude · для входа выполните claude auth login.",
            "openai-api": "Отдельная оплата API · требуется переменная OPENAI_API_KEY.",
            "antigravity": "Личный аккаунт Google · войдите через agy. Модели и лимиты зависят от тарифа.",
        }[self.provider_key])
        settings = load_config(None, self.provider_key)
        names = [entry.name for entry in settings.models.values()]
        choices = list(dict.fromkeys([*names, *EXTRA_MODELS[self.provider_key]]))
        for stage, (model, effort) in zip(STAGES, self.models):
            prefix = f"{self.provider_key}/{stage}"
            model.clear()
            model.addItems(choices)
            _set_combo_value(model, self.preferences.value(f"{prefix}/model", settings.models[stage].name, type=str))
            effort.clear()
            effort.addItems([AUTO_EFFORT, *EFFORT_OPTIONS[self.provider_key]])
            value = self.preferences.value(f"{prefix}/effort", settings.models[stage].reasoning, type=str)
            _set_combo_value(effort, value or AUTO_EFFORT)

    def _reset_models(self):
        self._set_models(load_config(None, self.provider_key))

    def _set_models(self, settings: Settings):
        for stage, (model, effort) in zip(STAGES, self.models):
            _set_combo_value(model, settings.models[stage].name)
            _set_combo_value(effort, settings.models[stage].reasoning or AUTO_EFFORT)

    def _model_values(self):
        for model, effort in self.models:
            value = effort.currentText().strip()
            yield model.currentText().strip(), "" if value == AUTO_EFFORT else value

    def _protected_inputs(self) -> list[Path]:
        source_text = self.source.text().strip()
        source = Path(source_text) if source_text else None
        cache = self.cache.text().strip()
        protected = [source] if source is not None else []
        if cache:
            state = Path(cache)
        elif source is not None and source.suffix.lower() in {".txt", ".epub", ".fb2"}:
            state = default_state_path(source)
        else:
            return protected
        return list(dict.fromkeys([*protected, state, state_for_reading(state)]))

    def _edit_glossary(self):
        current = self.glossary.text().strip()
        try:
            dialog = GlossaryDialog(Path(current) if current else None, self, protected=self._protected_inputs())
        except (ValueError, OSError) as exc:
            self.status.setText(f"Не удалось открыть глоссарий. {exc}")
            return
        try:
            if dialog.exec() == QDialog.DialogCode.Accepted and dialog.saved_path is not None:
                self.glossary.setText(str(dialog.saved_path))
                self.status.setText(f"Глоссарий сохранён · {dialog.saved_path.name}")
        finally:
            dialog.deleteLater()

    def _store_models(self):
        self.preferences.setValue("provider", self.provider_key)
        for stage, (model, effort) in zip(STAGES, self._model_values()):
            prefix = f"{self.provider_key}/{stage}"
            self.preferences.setValue(f"{prefix}/model", model)
            self.preferences.setValue(f"{prefix}/effort", effort)

    def _store_preferences(self):
        self._store_models()
        for key, value in (
            ("edit", self.edit.isChecked()), ("reviewEdits", self.review_edits.isChecked()),
            ("repairReview", self.repair_review.isChecked()),
            ("autoGlossary", self.auto_glossary.isChecked()),
            ("fandom", self.fandom.text()), ("style", self._style_text()),
            ("budget", self.budget.value()), ("batchSize", self.batch_size.value()),
            ("timeout", self.timeout.text()),
        ):
            self.preferences.setValue(key, value)

    def _run_controls(self):
        return {
            "glossary": self.glossary, "save_glossary": self.save_glossary, "encoding": self.encoding,
            "auto_glossary": self.auto_glossary, "edit": self.edit, "review_edits": self.review_edits,
            "repair_review": self.repair_review,
            "batches": self.batches, "start_batch": self.start_batch, "budget": self.budget, "state": self.cache,
        }

    def _update_last_run(self):
        available = bool(self.preferences.value("lastRun"))
        self.restore_run.setEnabled(available)
        self.forget_run.setEnabled(available)

    def _remember_last_run(self, action: str, source: Path, output: Path, profile: str) -> str | None:
        record = {"version": 1, "action": action, "source": str(source.absolute()),
                  "output": str(output.absolute()), "profile": profile}
        if action == "sample" and (chapter := self.chapter.currentData()) is not None:
            record["chapter"] = chapter
            record["chapter_label"] = self.chapter.currentText()
            record["chapter_revision"] = self.chapter_revision
        if self.total_budget_mode.currentIndex() == 1:
            record["pending_total_limit"] = self.total_budget.value()
        for key, control in self._run_controls().items():
            if isinstance(control, QCheckBox):
                record[key] = control.isChecked()
            elif isinstance(control, QSpinBox):
                record[key] = control.value()
            else:
                value = control.text().strip()
                record[key] = str(Path(value).absolute()) if value and control is not self.encoding else value
        snapshot = json.dumps(record, ensure_ascii=False)
        self.preferences.setValue("lastRun", snapshot)
        self.preferences.sync()
        self._update_last_run()
        return snapshot if self.preferences.status() == self.preferences.Status.NoError else None

    def _use_stricter_limit(self, enabled: bool) -> None:
        self.limit_tighten_only = enabled
        self.total_budget_mode.setItemText(1, "Ужесточить" if enabled else "Установить")

    def _clear_saved_limit(self, snapshot: str | None) -> None:
        if snapshot is None:
            return
        record = json.loads(snapshot)
        if "pending_total_limit" not in record:
            return
        self.preferences.sync()
        if self.preferences.value("lastRun") != snapshot:
            return
        record.pop("limit_unconfirmed", None)
        record.pop("pending_total_limit")
        self.preferences.setValue("lastRun", json.dumps(record, ensure_ascii=False))
        self.preferences.sync()
        if self.preferences.status() != self.preferences.Status.NoError:
            self.log.appendPlainText("Не удалось обновить параметры общего лимита для восстановления.")

    def _forget_last_run(self):
        self.preferences.remove("lastRun")
        self.preferences.sync()
        self._update_last_run()
        if self.preferences.status() != self.preferences.Status.NoError:
            self.status.setText("Не удалось удалить сохранённые параметры с диска.")

    def _restore_last_run(self):
        try:
            snapshot = self.preferences.value("lastRun", "")
            record = json.loads(snapshot)
            if not isinstance(record, dict) or type(record.get("version")) is not int or record["version"] != 1:
                raise ValueError("Версия сохранённых параметров не поддерживается.")
            action, source, output, profile = (record.get(key) for key in ("action", "source", "output", "profile"))
            if (not all(isinstance(value, str) and value for value in (action, source, output, profile))
                    or action not in OPERATIONS):
                raise ValueError("Укажите действие, книгу, выходной файл и профиль.")
            if not all("\x00" not in path and Path(path).is_absolute() for path in (source, output)):
                raise ValueError("Ожидаются абсолютные пути книги и выходного файла.")
            chapter, chapter_label = record.get("chapter"), record.get("chapter_label", "")
            revision = record.get("chapter_revision")
            if chapter is not None and (action != "sample" or type(chapter) is not int or chapter < 1
                                        or not isinstance(chapter_label, str)
                                        or (revision is not None and not isinstance(revision, str))):
                raise ValueError("Некорректная сохранённая глава.")
            pending_limit = record.get("pending_total_limit")
            if "pending_total_limit" in record and (
                type(pending_limit) is not int or not -1 <= pending_limit <= self.total_budget.maximum()
            ):
                raise ValueError("Некорректный сохранённый общий лимит.")
            settings = parse_config(profile)
            changes = []
            defaults = {"state": "", "repair_review": False}
            for key, control in self._run_controls().items():
                value = record.get(key, defaults.get(key))
                if isinstance(control, QCheckBox) and type(value) is bool:
                    changes.append((control.setChecked, value))
                elif (isinstance(control, QSpinBox) and type(value) is int
                      and control.minimum() <= value <= control.maximum()):
                    changes.append((control.setValue, value))
                elif (isinstance(control, QLineEdit) and isinstance(value, str) and "\x00" not in value
                      and (control is self.encoding or not value or Path(value).is_absolute())):
                    changes.append((control.setText, value))
                else:
                    raise ValueError(f"Некорректное сохранённое поле {control.accessibleName() or key}.")
            state = Path(record["state"]) if record.get("state") else default_state_path(Path(source))
            self._apply_profile(settings)
        except (OSError, TypeError, ValueError) as exc:
            self.status.setText(f"Не удалось восстановить запуск. {exc}")
            return
        self.source.setText(source)
        self.operation.setCurrentIndex(self.operation.findData(action))
        if chapter is not None and revision != self.chapter_revision:
            self._set_chapters((), revision)
        chapter_index = self.chapter.findData(chapter)
        if chapter_index < 0 and chapter is not None:
            self.chapter.addItem(chapter_label or f"Глава {chapter}", chapter)
            chapter_index = self.chapter.count() - 1
        self.chapter.setCurrentIndex(max(0, chapter_index))
        self.output.setText(output)
        for set_value, value in changes:
            set_value(value)
        self.total_budget_mode.setCurrentIndex(0)
        ceiling = self._refresh_budget(state)
        if pending_limit is not None:
            if pending_limit < 0 or (ceiling is not None and ceiling <= pending_limit):
                self._clear_saved_limit(snapshot)
            else:
                self.total_budget.setValue(pending_limit)
                self.total_budget_mode.setCurrentIndex(1)
                self._use_stricter_limit(True)
        if chapter is None or self._chapter_is_current(Path(source)):
            self.status.setText(f"Параметры восстановлены · {Path(source).name}")

    def _set_style(self, text: str) -> None:
        self.style.setPlainText(text)
        self.imported_style = text

    def _style_text(self) -> str:
        if self.imported_style is not None:
            return self.imported_style
        return self.style.document().toRawText().replace("\u2029", "\n")

    def _profile_text(self) -> str:
        try:
            timeout = int(self.timeout.text())
        except ValueError:
            raise ValueError("Тайм-аут запроса должен быть положительным целым числом секунд.") from None
        return config_text(self.provider_key, {
            "models": list(self._model_values()),
            "fandom": self.fandom.text(), "style": self._style_text(),
            "batch_chars": self.batch_size.value(), "timeout": timeout,
        })

    def _load_profile(self, path: Path) -> None:
        settings = load_config(path, None)
        self._apply_profile(settings)
        self._store_preferences()

    def _apply_profile(self, settings: Settings) -> None:
        self.provider.setCurrentIndex(self.provider.findData(settings.provider))
        self._set_models(settings)
        self.fandom.setText(settings.fandom)
        self._set_style(settings.style)
        self.batch_size.setValue(settings.batch_chars)
        self.timeout.setText(str(settings.timeout))

    def _choose_profile(self):
        filename, _ = QFileDialog.getOpenFileName(self, "Загрузить настройки", "", "TOML (*.toml)")
        if not filename:
            return
        try:
            self._load_profile(Path(filename))
        except (ValueError, OSError) as exc:
            self.status.setText(f"Не удалось загрузить настройки. {exc}")
            return
        self.status.setText(f"Профиль загружен · {Path(filename).name}")

    def _export_profile(self):
        filename, _ = QFileDialog.getSaveFileName(
            self, "Сохранить настройки", "translation.toml", "TOML (*.toml)",
            options=QFileDialog.Option.DontConfirmOverwrite,
        )
        if not filename:
            return
        path = Path(filename)
        if not path.suffix:
            path = path.with_suffix(".toml")
        if path.suffix.lower() != ".toml":
            self.status.setText("Выберите обычный файл с расширением .toml.")
            return
        try:
            validate_job_paths(self._protected_inputs(), None, [path], force=True)
            force = path.exists() or path.is_symlink()
            if force and not _confirm_replace(self, "Заменить профиль?", str(path)):
                return
            content = self._profile_text()
            write_outputs([(path, lambda target: target.write_text(content, encoding="utf-8"))], force=force)
        except (ValueError, OSError) as exc:
            self.status.setText(f"Не удалось сохранить настройки. {exc}")
            return
        self.status.setText(f"Профиль сохранён · {path.name}")

    def _set_chapters(self, entries, revision: str | None = None):
        selected = self.chapter.currentData() if revision == self.chapter_revision else None
        self.chapter_revision = revision
        blocked = self.chapter.blockSignals(True)
        try:
            self.chapter.clear()
            self.chapter.addItem("По пакетам", None)
            for entry in entries:
                self.chapter.addItem(f"{entry['number']}. {entry['title']}", entry["number"])
            self.chapter.setCurrentIndex(max(0, self.chapter.findData(selected)))
        finally:
            self.chapter.blockSignals(blocked)

    def _chapter_is_current(self, source: Path) -> bool:
        try:
            encoding = (self.encoding.text().strip() or None) if source.suffix.lower() == ".txt" else None
            if self.chapter_revision == chapter_revision(source, encoding):
                return True
        except OSError as exc:
            self.status.setText(f"Не удалось проверить выбранную главу. {exc}")
            return False
        self.status.setText("Выбор главы устарел. Загрузите оглавление заново и выберите главу.")
        return False

    def _result_selection(self, selection):
        source, operation, chapter, _ = selection
        if operation == "sample":
            if Path(source).suffix.lower() == ".txt":
                selection += (self.encoding.text().strip(),)
            if chapter is None:
                selection += (self.start_batch.value(), self.batches.value(), self.batch_size.value())
        return selection

    def _update_form(self):
        operation = self.operation.currentData()
        source = self.source.text().strip()
        if self.output_selection is not None and source != self.output_selection[0]:
            previous_source = self.output_selection[0]
            if previous_source:
                self.glossary_paths[previous_source] = (self.glossary.text(), self.save_glossary.text())
            if previous_source or source in self.glossary_paths:
                glossary, export = self.glossary_paths.get(source, ("", ""))
                self.glossary.setText(glossary)
                self.save_glossary.setText(export)
            self._set_chapters(())
            self.last_output = self.last_output_selection = None
            self.preview.set_pairs(None, [])
            self.cache.clear()
        chapter = self.chapter.currentData() if operation == "sample" else None
        selection = (source, operation, chapter, self.chapter_revision if chapter is not None else None)
        result_selection = self._result_selection(selection)
        if (self.last_output_selection and self.last_output_selection[1] == "sample"
                and result_selection != self.last_output_selection):
            self.preview.set_pairs(None, [])
            self.last_output = self.last_output_selection = None
        if selection != self.output_selection:
            if self.output_selection is not None and self.output.text().strip():
                self.output_paths[self.output_selection] = self.output.text()
            else:
                self.output_paths.pop(self.output_selection, None)
            self.output_selection = selection
            self.output.setText(self.output_paths.get(selection, ""))
        valid = Path(source).suffix.lower() in {".txt", ".epub", ".fb2"}
        self.output.setPlaceholderText(str(default_output(Path(source), operation, chapter=chapter))
                                       if valid else "Рядом с исходником")
        self.encoding.setEnabled(Path(source).suffix.lower() == ".txt")
        self.chapter.setEnabled(operation == "sample" and self.chapter.count() > 1)
        self.load_chapters.setEnabled(valid and operation == "sample" and self.process is None)
        self.batches.setEnabled(operation == "sample" and chapter is None)
        self.start_batch.setEnabled(operation == "sample" and chapter is None)
        self.edit.setEnabled(operation != "glossary")
        self.review_edits.setEnabled(operation != "glossary" and self.edit.isChecked())
        self.repair_review.setEnabled(self.review_edits.isEnabled() and self.review_edits.isChecked())
        package = "каждый пакет" if self.repair_review.isEnabled() and self.repair_review.isChecked() else "пакет с изменениями"
        self.review_edits.setToolTip(f"Без кеша обычно нужен один запрос на {package}. При ошибке возможен повтор.")
        self.auto_glossary.setEnabled(operation != "glossary")
        self.save_glossary.setEnabled(operation != "glossary")
        self.run.setText(OPERATIONS[operation])
        self.run.setEnabled(valid and self.process is None)
        self.inspect.setEnabled(valid and self.process is None)
        self.check_login.setEnabled(self.process is None)
        result_available = self.last_output is not None and result_selection == self.last_output_selection
        self.open_result.setEnabled(result_available and self.process is None)
        self.open_result.setToolTip(str(self.last_output) if result_available else "")

    def dragEnterEvent(self, event):
        urls = event.mimeData().urls()
        if self.process is None and any(Path(url.toLocalFile()).suffix.lower() in {".txt", ".epub", ".fb2"}
                                        for url in urls):
            event.acceptProposedAction()

    def dropEvent(self, event):
        if self.process is not None:
            return
        for url in event.mimeData().urls():
            path = Path(url.toLocalFile())
            if path.is_file() and path.suffix.lower() in {".txt", ".epub", ".fb2"}:
                self.source.setText(str(path))
                event.acceptProposedAction()
                break

    def _arguments(self, action, config, source, output, replacements):
        args = ["-m", "ai_translator.cli", action, "--json-events", f"--config={config}"]
        if self.cache.text().strip():
            args.append(f"--state={self.cache.text().strip()}")
        if self.glossary.text().strip():
            args.append(f"--glossary={self.glossary.text().strip()}")
        if source.suffix.lower() == ".txt" and self.encoding.text().strip():
            args.append(f"--encoding={self.encoding.text().strip()}")
        selection = self.operation.currentData() if action == "inspect" else action
        if action == "inspect":
            args += ["--for", selection]
        if selection != "glossary":
            if not self.auto_glossary.isChecked():
                args.append("--no-auto-glossary")
            if self.edit.isChecked():
                args.append("--edit")
                if self.review_edits.isChecked():
                    args.append("--repair-review" if self.repair_review.isChecked() else "--review-edits")
        if action != "inspect":
            args.append(f"--output={output}")
            if self.budget.value() >= 0:
                args += ["--max-calls", str(self.budget.value())]
            mode = self.total_budget_mode.currentIndex()
            if mode:
                flag = "--tighten-total-calls" if self.limit_tighten_only else "--max-total-calls"
                args += [flag, str(self.total_budget.value() if mode == 1 else -1)]
            for path in replacements:
                args.append(f"--replace-existing={path}")
            if action != "glossary" and self.save_glossary.text().strip():
                args.append(f"--save-glossary={self.save_glossary.text().strip()}")
        if selection == "sample":
            chapter = self.chapter.currentData()
            if chapter is not None:
                args += ["--chapter", str(chapter)]
                if self.chapter_revision is not None:
                    args += ["--chapter-revision", self.chapter_revision]
            else:
                args += ["--batches", str(self.batches.value()), "--start-batch", str(self.start_batch.value())]
        if action == "sample":
            args.append("--sample-pairs")
            if self.edit.isChecked():
                args.append("--sample-versions")
        return args + ["--", str(source)]

    def start(self, action):
        if self.process is not None:
            return
        source = Path(self.source.text().strip())
        if action != "doctor" and not source.is_file():
            self.status.setText("Исходный файл не найден.")
            return
        sampling = action == "sample" or (action == "inspect" and self.operation.currentData() == "sample")
        if sampling and self.chapter.currentData() is not None and not self._chapter_is_current(source):
            return
        if action not in ("doctor", "chapters"):
            try:
                profile = self._profile_text()
            except ValueError as exc:
                self.options.setCurrentIndex(1)
                if isinstance(exc, ModelSettingError):
                    model, effort = self.models[STAGES.index(exc.stage)]
                    (model if exc.field == "model" else effort).setFocus()
                self.status.setText(str(exc))
                return
        try:
            state = None
            if action in OPERATIONS:
                cache = self.cache.text().strip()
                state = Path(cache) if cache else default_state_path(source)
        except (OSError, ValueError) as exc:
            self.status.setText(f"Не удалось открыть кеш. {exc}")
            return
        output = None
        replacements = []
        chapter = self.chapter.currentData() if action == "sample" else None
        if action in OPERATIONS:
            suggested = default_output(source, action, chapter=chapter)
            output = Path(self.output.text().strip()) if self.output.text().strip() else suggested
            if output.suffix.lower() != suggested.suffix:
                self.status.setText(f"Для этого действия укажите выходной файл {suggested.suffix.upper()}.")
                return
            destinations = [output]
            if action != "glossary" and self.save_glossary.text().strip():
                extra_glossary = Path(self.save_glossary.text().strip())
                if extra_glossary.suffix.lower() != ".json":
                    self.status.setText("Выходной глоссарий должен иметь расширение .json.")
                    self.options.setCurrentIndex(0)
                    self.save_glossary.setFocus()
                    return
                destinations.append(extra_glossary)
            reads = [source]
            if glossary := self.glossary.text().strip():
                reads.append(Path(glossary))
            try:
                validate_job_paths(reads, state, destinations, force=True)
                existing = [str(path) for path in destinations if path.exists() or path.is_symlink()]
            except (ValueError, OSError) as exc:
                self.status.setText(str(exc))
                return
            if existing:
                if not _confirm_replace(self, "Заменить файлы?", "\n".join(existing)):
                    return
                replacements = existing

        args = ["-m", "ai_translator.cli", "doctor", "--json-events", "--provider", self.provider_key]
        if action == "chapters":
            args = ["-m", "ai_translator.cli", "inspect", "--list-chapters", "--json-events"]
            if source.suffix.lower() == ".txt" and self.encoding.text().strip():
                args.append(f"--encoding={self.encoding.text().strip()}")
            args += ["--", str(source)]
        elif action != "doctor":
            try:
                self.job_config = TemporaryDirectory(prefix="ai-translator-desktop-")
                config = Path(self.job_config.name, "settings.toml")
                config.write_text(profile, encoding="utf-8")
            except OSError as exc:
                if self.job_config is not None:
                    self.job_config.cleanup()
                    self.job_config = None
                self.status.setText(f"Не удалось подготовить настройки запуска. {exc}")
                return
            args = self._arguments(action, config, source, output, replacements)

        self._store_preferences()
        self.log.clear()
        if action == "inspect":
            self.budget_status.hide()
        if output is not None:
            self.job_snapshot = self._remember_last_run(action, source, output, profile)
            if self.job_snapshot is None:
                self.log.appendPlainText("Не удалось сохранить параметры последнего запуска.")
        if action in OPERATIONS:
            self.last_output = self.last_output_selection = None
        if action == "sample":
            self.preview.set_pairs(None, [])
        self.results.setCurrentIndex(0)
        self.status.setText({"doctor": "Проверка входа…", "chapters": "Чтение оглавления…"}.get(action, "Подготовка книги…"))
        self.progress.setRange(0, 0)
        self.pending_output = None
        self.pending_sample = []
        self.worker_success = ""
        self.worker_error = ""
        self.cancel_requested = False
        self.stdout.clear()
        self.job_state = state
        self.budget_final_received = False

        process = QProcess(self)
        self.process = process
        self.job_selection = self._result_selection(
            (self.source.text().strip(), action, chapter, self.chapter_revision if chapter is not None else None)
        )
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONUTF8", "1")
        if os.name == "posix":
            process.setUnixProcessParameters(QProcess.UnixProcessFlag.CreateNewSession)
            process.started.connect(self._remember_worker_group)
        process.setProcessEnvironment(env)
        process.readyReadStandardOutput.connect(self._read_events)
        process.readyReadStandardError.connect(self._read_stderr)
        process.finished.connect(self._finished)
        process.errorOccurred.connect(self._process_error)
        self.options.setDisabled(True)
        self._update_form()
        self.stop.setEnabled(True)
        process.start(sys.executable, args)

    def _read_stderr(self):
        content = bytes(self.process.readAllStandardError()).decode("utf-8", errors="replace").rstrip()
        if content:
            self.log.appendPlainText(content)

    def _read_events(self):
        self.stdout.extend(bytes(self.process.readAllStandardOutput()))
        while (end := self.stdout.find(b"\n")) != -1:
            chunk = bytes(self.stdout[:end])
            del self.stdout[:end + 1]
            try:
                event = json.loads(chunk)
            except ValueError:
                self.log.appendPlainText(chunk.decode("utf-8", errors="replace"))
                continue
            kind, message = event["event"], event["message"]
            if kind == "sample_pair":
                self.pending_sample.append(SamplePair(event["original"], event["translation"], event.get("draft"),
                                                      event.get("edited"), event.get("review_choice")))
                continue
            if kind == "chapters":
                entries = event["chapters"]
                self._set_chapters(entries, event["revision"])
                self._update_form()
                self.worker_success = (f"Глав с китайским текстом: {len(entries)}. Выберите главу для пробы."
                                       if entries else message)
                continue
            self.log.appendPlainText(message)
            if kind == "progress":
                self.status.setText(message)
                self.progress.setRange(0, event["total"])
                self.progress.setValue(event["current"] - 1)
            elif kind in ("error", "cancelled", "budget", "authentication"):
                self.worker_error = message
            elif kind == "authentication_ok":
                self.worker_success = message
            elif kind == "request_budget":
                self._show_budget(event["attempts"], event["ceiling"])
                if event.get("changed"):
                    self.total_budget_mode.setCurrentIndex(0)
                    self._clear_saved_limit(self.job_snapshot)
                if event.get("phase") == "final":
                    self.budget_final_received = True
            elif kind == "output" and self.pending_output is None:
                self.pending_output = Path(event["path"])

    def _show_budget(self, attempts: int, ceiling: int | None) -> None:
        limit = f" / {ceiling}" if ceiling is not None else ""
        self.budget_status.setText(f"Запросов в базе · {attempts}{limit}")
        self.budget_status.show()

    def _refresh_budget(self, path: Path) -> int | None:
        self.budget_status.hide()
        try:
            path = state_for_reading(path)
            if not path.is_file():
                return None
            with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.2)) as db:
                budget = read_request_budget(db)
        except (sqlite3.Error, OSError) as exc:
            self.log.appendPlainText(f"Не удалось прочитать общий лимит. {exc}")
            return None
        if budget is None:
            return None
        self._show_budget(*budget)
        return budget[1]

    def cancel(self):
        process = self.process
        if process is None or self.cancel_requested:
            return
        self.cancel_requested = True
        self.stop.setDisabled(True)
        self.status.setText("Остановка…")
        taskkill = None
        if os.name == "nt" and process.processId():
            program, *arguments = taskkill_command(process.processId())
            taskkill = QProcess(process)
            taskkill.start(program, arguments)

        def interrupt(sig):
            if process is not self.process or process.state() == QProcess.ProcessState.NotRunning:
                return
            if self.worker_group is not None:
                self._signal_process_group(self.worker_group, sig)
            elif sig == signal.SIGINT:
                process.terminate()
            else:
                process.kill()

        if taskkill is None:
            interrupt(signal.SIGINT)
        else:
            taskkill.errorOccurred.connect(lambda _: interrupt(signal.SIGINT))
            taskkill.finished.connect(lambda code, _: interrupt(signal.SIGINT) if code else None)
        timer = QTimer(process)
        timer.setSingleShot(True)
        timer.timeout.connect(lambda: interrupt(signal.SIGKILL if os.name == "posix" else signal.SIGTERM))
        timer.start(3500)

    def _remember_worker_group(self) -> None:
        self.worker_group = self.process.processId() or None
        if self.worker_group is not None and self.cancel_requested:
            self._signal_process_group(self.worker_group, signal.SIGINT)

    @staticmethod
    def _signal_process_group(group: int, sig: int) -> None:
        try:
            os.killpg(group, sig)
        except ProcessLookupError:
            pass

    def _process_error(self, error):
        if error == QProcess.ProcessError.FailedToStart:
            self.worker_error = self.process.errorString()
            self._finished(1, QProcess.ExitStatus.CrashExit)

    def _finished(self, code, exit_status):
        self._read_events()
        self._read_stderr()
        if self.worker_group is not None:
            # The worker has exited, but the rest of its session may still be running.
            self._signal_process_group(self.worker_group, signal.SIGKILL)
            self.worker_group = None
        self.process.deleteLater()
        self.process = None
        if self.job_state is not None and (
            self.cancel_requested or exit_status == QProcess.ExitStatus.CrashExit or not self.budget_final_received
        ):
            ceiling = self._refresh_budget(self.job_state)
            if self.total_budget_mode.currentIndex() != 1 or (
                ceiling is not None and ceiling <= self.total_budget.value()
            ):
                self.total_budget_mode.setCurrentIndex(0)
                self._clear_saved_limit(self.job_snapshot)
            else:
                self._use_stricter_limit(True)
        self.job_state = None
        self.job_snapshot = None
        if self.job_config is not None:
            self.job_config.cleanup()
            self.job_config = None
        self.options.setEnabled(True)
        self.stop.setDisabled(True)
        self._update_form()
        self.progress.setRange(0, 1)
        success = code == 0 and exit_status == QProcess.ExitStatus.NormalExit
        self.progress.setValue(int(success))
        if success:
            self.status.setText(self.worker_success or "Готово.")
            if self.pending_output:
                self.last_output = self.pending_output
                self.last_output_selection = self.job_selection
                self._update_form()
                if self.job_selection[1] == "sample" and self.job_selection == self._result_selection(self.output_selection):
                    self.preview.set_pairs(self.last_output, self.pending_sample)
                    self.results.setCurrentWidget(self.preview)
        elif self.cancel_requested:
            self.status.setText("Остановлено. Повторный запуск использует готовые пакеты из кеша.")
        else:
            self.status.setText(self.worker_error or f"Переводчик завершился с кодом {code}. См. журнал.")
        if self.close_when_finished:
            self.close()

    def _open_result(self):
        if (self.open_result.isEnabled() and self.last_output
                and not QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.last_output)))):
            self.status.setText(f"Откройте файл вручную · {self.last_output}")

    def closeEvent(self, event):
        if self.process is not None:
            event.ignore()
            if QMessageBox.question(self, "Остановить работу и выйти?", "Готовые пакеты останутся в кеше.",
                                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                                    ) == QMessageBox.StandardButton.Yes:
                self.close_when_finished = True
                self.cancel()
        else:
            self._store_preferences()
            event.accept()


def main(argv: list[str] | None = None) -> int:
    configure_output()
    parser = argparse.ArgumentParser(
        prog="ai-translator-desktop", description="Десктопное приложение для литературного перевода книг.",
    )
    parser.add_argument("book", nargs="?", metavar="книга", help="Открыть книгу TXT, EPUB или FB2.")
    args = parser.parse_args(argv)
    if QApplication is None:
        print(QT_ERROR, file=sys.stderr)
        return 1
    app = QApplication([sys.argv[0]])
    app.setApplicationName("Литературный перевод")
    window = Window()
    if args.book is not None:
        window.source.setText(args.book)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
