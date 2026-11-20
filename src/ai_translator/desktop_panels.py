from tempfile import TemporaryFile

from PySide6.QtCore import QAbstractListModel, QModelIndex, Qt
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
    QListView, QListWidget, QListWidgetItem, QPlainTextEdit, QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from .formats import txt_number_gaps


def chapter_matches(entry: dict, query: str) -> bool:
    number = str(entry["number"])
    return query in entry["title"].casefold() or query in number


def _buttons(dialog, layout, label):
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
    buttons.button(QDialogButtonBox.StandardButton.Ok).setText(label)
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    return buttons


class ChapterListModel(QAbstractListModel):
    def __init__(self, parent, checked=None):
        super().__init__(parent)
        self.rows = []
        self.checked = checked

    def set_rows(self, rows):
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def data(self, index, role):
        if index.isValid():
            label, number, tooltip = self.rows[index.row()]
            if role == Qt.ItemDataRole.DisplayRole:
                return label
            if role == Qt.ItemDataRole.UserRole:
                return number
            if role == Qt.ItemDataRole.ToolTipRole:
                return tooltip
            if role == Qt.ItemDataRole.CheckStateRole and self.checked is not None:
                return Qt.CheckState.Checked if number in self.checked else Qt.CheckState.Unchecked
        return None

    def flags(self, index):
        flags = super().flags(index)
        if index.isValid() and self.checked is not None:
            flags |= Qt.ItemFlag.ItemIsUserCheckable
        return flags

    def setData(self, index, value, role):
        if not index.isValid() or role != Qt.ItemDataRole.CheckStateRole or self.checked is None:
            return False
        number = self.rows[index.row()][1]
        if Qt.CheckState(value) == Qt.CheckState.Checked:
            self.checked.add(number)
        else:
            self.checked.discard(number)
        self.dataChanged.emit(index, index, [Qt.ItemDataRole.CheckStateRole])
        return True

    def check_visible(self, checked):
        numbers = (number for _, number, _ in self.rows)
        if checked:
            self.checked.update(numbers)
        else:
            self.checked.difference_update(numbers)
        self.dataChanged.emit(self.index(0, 0), self.index(len(self.rows) - 1, 0), [Qt.ItemDataRole.CheckStateRole])


class ChapterPicker(QDialog):
    def __init__(self, entries, selected, parent):
        super().__init__(parent)
        self.setWindowTitle("Главы для двуязычной пробы")
        self.resize(590, 440)
        self.entries = entries
        self.search = QLineEdit(placeholderText="Название или номер записи")
        self.search.setAccessibleName("Поиск глав")
        self.list = QListView()
        self.list.setAccessibleName("Главы, пробел меняет выбор")
        self.list.setUniformItemSizes(True)
        self.model = ChapterListModel(self.list, checked=set(selected) - {None})
        self.list.setModel(self.model)
        self.count = QLabel()
        layout = QVBoxLayout(self)
        for widget in (self.search, self.list, self.count):
            layout.addWidget(widget)
        actions = QHBoxLayout()
        self.select_visible = QPushButton("Выделить видимые", autoDefault=False)
        self.clear_visible = QPushButton("Снять видимые", autoDefault=False)
        self.select_visible.clicked.connect(lambda: self.model.check_visible(True))
        self.clear_visible.clicked.connect(lambda: self.model.check_visible(False))
        for button in (self.select_visible, self.clear_visible):
            actions.addWidget(button)
        actions.addStretch()
        layout.addLayout(actions)
        _buttons(self, layout, "Применить")
        self.search.textChanged.connect(self._filter)
        self.model.dataChanged.connect(self._show_count)
        self._filter()

    def _filter(self):
        query = self.search.text().strip().casefold()
        rows = [(f"{entry['title']} · запись {entry['number']} · блоков {entry['units']}", entry["number"], None)
                for entry in self.entries if chapter_matches(entry, query)]
        self.model.set_rows(rows)
        self.list.setCurrentIndex(self.model.index(0, 0))
        self.select_visible.setEnabled(bool(rows))
        self.clear_visible.setEnabled(bool(rows))
        self._show_count()

    def _show_count(self):
        self.count.setText(f"Совпадений {len(self.model.rows)} · выбрано {len(self.model.checked)}")

    def selection(self):
        return tuple(entry["number"] for entry in self.entries if entry["number"] in self.model.checked)


class TxtMapEditor(QDialog):
    PAGE_SIZE = 80

    def __init__(self, document, anchors, parent):
        super().__init__(parent)
        self.setWindowTitle("Карта глав TXT")
        self.resize(680, 500)
        self.document = document
        self.anchors = dict(anchors)
        self.line = QSpinBox()
        self.line.setRange(1, max(1, len(document.lines)))
        self.line.setAccessibleName("Номер строки исходника")
        self.line.setPrefix("Строка ")
        self.rows = QListWidget()
        self.rows.setAccessibleName("Строки исходника и ручные границы")
        self.rows.setUniformItemSizes(True)
        self.starts = QListWidget()
        self.starts.setAccessibleName("Ручные границы TXT")
        self.starts.setMaximumHeight(102)
        self.starts.setUniformItemSizes(True)
        self.add = QPushButton("Начать главу здесь…")
        self.remove = QPushButton("Снять границу")
        self.count = QLabel(f"Строк {len(document.lines)} · {document.encoding}")
        self.gaps = QLabel()
        self.gaps.setWordWrap(True)
        row = QHBoxLayout()
        for widget in (self.line, self.add, self.remove):
            row.addWidget(widget)
        layout = QVBoxLayout(self)
        layout.addWidget(self.count)
        layout.addWidget(self.gaps)
        layout.addLayout(row)
        layout.addWidget(self.rows, 1)
        layout.addWidget(self.starts)
        _buttons(self, layout, "Сохранить карту…")
        self.add.clicked.connect(self._add)
        self.remove.clicked.connect(self._remove)
        self.line.valueChanged.connect(self._show_line)
        self.rows.currentItemChanged.connect(self._pick_row)
        self.starts.itemActivated.connect(lambda item: self.line.setValue(item.data(Qt.ItemDataRole.UserRole)))
        self._show_starts()
        self._show_line(1)

    def _show_starts(self):
        self.starts.clear()
        gaps = txt_number_gaps(self.anchors)
        self.gaps.setVisible(bool(gaps))
        if gaps:
            line, before, after = gaps[0]
            self.gaps.setText(f"Скачков нумерации {len(gaps)} · первый: {before} → {after}, строка {line}. "
                              "Проверьте исходник рядом с границей.")
        for line, title in sorted(self.anchors.items()):
            item = QListWidgetItem(f"{line} · {title}", self.starts)
            item.setData(Qt.ItemDataRole.UserRole, line)
        self.starts.setVisible(bool(self.anchors))

    def _show_line(self, line):
        start = ((line - 1) // self.PAGE_SIZE) * self.PAGE_SIZE
        previous = self.rows.blockSignals(True)
        self.rows.clear()
        for number, text in enumerate(self.document.lines[start:start + self.PAGE_SIZE], start + 1):
            boundary = f"◆ {self.anchors[number]}  |  " if number in self.anchors else ""
            excerpt = text.strip()[:200] or "Пустая строка"
            item = QListWidgetItem(f"{number} · {boundary}{excerpt}", self.rows)
            item.setData(Qt.ItemDataRole.UserRole, number)
        self.rows.setCurrentRow(line - start - 1)
        self.rows.blockSignals(previous)
        self.add.setEnabled(bool(self.document.lines))
        self.remove.setEnabled(line in self.anchors)

    def _pick_row(self, item, _previous):
        if item is not None:
            line = item.data(Qt.ItemDataRole.UserRole)
            blocked = self.line.blockSignals(True)
            self.line.setValue(line)
            self.line.blockSignals(blocked)
            self.remove.setEnabled(line in self.anchors)

    def _add(self):
        line = self.line.value()
        default = self.anchors.get(line, self.document.lines[line - 1].strip()[:240] or f"Строка {line}")
        title, accepted = QInputDialog.getText(self, "Граница главы", "Заголовок", text=default)
        title = title.strip()
        if accepted and title and len(title) <= 240 and title.splitlines() == [title] and "\x00" not in title:
            self.anchors[line] = title
            self._show_starts()
            self._show_line(line)
        elif accepted:
            self.count.setText("Введите заголовок в одну строку, длиной до 240 символов.")

    def _remove(self):
        line = self.line.value()
        self.anchors.pop(line, None)
        self._show_starts()
        self._show_line(line)


class ProofreadingRows:
    PAGE_SIZE = 100

    def __init__(self):
        self.file = TemporaryFile(mode="w+t", encoding="utf-8")
        self.offsets = []
        self.count = 0

    def append(self, row):
        if not self.count % self.PAGE_SIZE:
            self.offsets.append(self.file.tell())
        self.file.write(row + "\n")
        self.count += 1

    def page(self, number):
        if not self.offsets:
            return ""
        self.file.seek(self.offsets[number - 1])
        return "".join(self.file.readline() for _ in range(self.PAGE_SIZE)).rstrip("\n")

    def close(self):
        self.file.close()


class ProofreadingView(QWidget):

    def __init__(self):
        super().__init__()
        self.summary = QLabel("Включите адресную сводку перед переводом книги.")
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(Qt.TextFormat.PlainText)
        self.rows = QPlainTextEdit(readOnly=True)
        self.rows.setAccessibleName("Адреса фрагментов исходной книги")
        self.page = QSpinBox()
        self.page.setAccessibleName("Страница вычитки")
        self.page.setPrefix("Страница ")
        layout = QVBoxLayout(self)
        for widget in (self.summary, self.rows, self.page):
            layout.addWidget(widget)
        self.page.valueChanged.connect(self._render)
        self.items = None
        self.set_report(None, None)

    def set_report(self, header, rows):
        if self.items is not None:
            self.destroyed.disconnect(self.items.close)
            self.items.close()
        self.items = rows
        if rows is not None:
            self.destroyed.connect(rows.close)
        if header:
            self.summary.setText(
                f"{header['message']} Без сверки: {header['parts'] - header['checked']}.\n"
                f"{header['source']} · ревизия {header['revision']}\n"
                "Адрес: секция / блок с китайским текстом / часть. "
                "Прежний кеш может не содержать причины повторных запросов."
            )
        else:
            self.summary.setText("Включите адресную сводку перед переводом книги.")
        pages = len(rows.offsets) if rows and rows.offsets else 1
        self.page.setRange(1, pages)
        self.page.setSuffix(f" из {pages}")
        self.page.setEnabled(pages > 1)
        self.page.setValue(1)
        self._render(1)

    def _render(self, page):
        self.rows.setPlainText(self.items.page(page) if self.items is not None else "")
