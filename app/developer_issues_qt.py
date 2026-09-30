"""Desktop issue workspace with short English drafts and explicit model consent."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QHBoxLayout,
    QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QSplitter,
    QSpinBox, QTabWidget, QTableWidget, QTableWidgetItem, QTextBrowser, QVBoxLayout, QWidget,
)

from issue_workspace import DEFAULT_TEMPLATE, IssueStore, report_fields


STATUS = {
    "queued": "В очереди", "running": "Готовится", "draft": "Markdown готов",
    "poc_unverified": "PoC не проверен", "needs_evidence": "Нужны данные для PoC",
    "poc_error": "Markdown готов / ошибка PoC", "error": "Ошибка",
}


class DeveloperIssuesTab(QWidget):
    def minimumSizeHint(self) -> QSize:
        # A page's toolbar must not force every other tab wider than the window.
        return QSize(0, 0)

    def __init__(self, host: Any):
        super().__init__()
        self.host = host
        self.store = IssueStore(host.tool_directory)
        self.candidates: list[dict] = []
        self.queue: list[dict] = []
        self.current: dict | None = None
        self.dirty = False
        self.running = False
        self.stop_requested = False
        self._batch_ids: list[str] = []
        self._active_ids: set[str] = set()
        self._rewriting_id: str | None = None
        self._loading = False
        self._signature: tuple | None = None
        self._scope_job: Path | None = None
        self.build_ui()
        self.save_timer = QTimer(self)
        self.save_timer.setSingleShot(True)
        self.save_timer.setInterval(600)
        self.save_timer.timeout.connect(self.save_editor)
        self.queue_timer = QTimer(self)
        self.queue_timer.setInterval(250)
        self.queue_timer.timeout.connect(self.next_case)

    @staticmethod
    def action(text: str, callback: Any, tone: str = "neutral") -> QPushButton:
        # Import at construction time to share the desktop's theme without a
        # module-level import cycle.
        from triage_gui_qt import button
        widget = button(text, tone=tone)
        widget.clicked.connect(callback)
        return widget

    @staticmethod
    def make_table(headers: list[str]) -> QTableWidget:
        from triage_gui_qt import table
        widget = table(headers)
        widget.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        return widget

    def build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 9, 0, 0)
        header = QHBoxLayout()
        header.addWidget(QLabel("Issue разработчику"))
        self.scope = QComboBox()
        self.scope.addItems(["Текущая задача", "Все сохранённые задачи"])
        self.scope.currentIndexChanged.connect(lambda: self.refresh(force=True))
        header.addWidget(self.scope)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Поиск по проекту, правилу, файлу или ID")
        self.search.textChanged.connect(self.render_candidates)
        header.addWidget(self.search, 1)
        header.addWidget(self.action("Обновить", lambda: self.refresh(force=True)))
        header.addWidget(self.action("Шаблон…", self.edit_template, "violet"))
        layout.addLayout(header)
        self.summary = QLabel("Выберите Confirmed и добавьте в отдельную очередь issue.")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        split = QSplitter(Qt.Orientation.Horizontal)
        layout.addWidget(split, 1)
        left = QWidget()
        left_box = QVBoxLayout(left)
        left_box.setContentsMargins(0, 0, 6, 0)
        left_box.addWidget(QLabel("Confirmed · Ctrl/Shift для нескольких находок"))
        self.confirmed_table = self.make_table(["Проект", "Детектор", "Место", "Проверка", "PoC"])
        from triage_gui_qt import size_columns
        size_columns(self.confirmed_table, {0: 90, 1: 140, 3: 100, 4: 110}, 2)
        self.confirmed_table.itemSelectionChanged.connect(self.update_actions)
        left_box.addWidget(self.confirmed_table, 3)
        options = QHBoxLayout()
        self.with_poc = QCheckBox("Также создать PoC через Codex")
        self.with_poc.setToolTip("Только после подтверждения передачи данных. Созданный код не запускается.")
        options.addWidget(self.with_poc)
        options.addStretch()
        options.addWidget(QLabel("Одновременно:"))
        self.workers = QSpinBox()
        self.workers.setRange(1, 8)
        self.workers.setValue(self.store.workers())
        self.workers.setToolTip("Слоты подготовки issue/PoC. Число агентов анализа задаётся отдельно в настройках.")
        self.workers.valueChanged.connect(self.save_workers)
        options.addWidget(self.workers)
        left_box.addLayout(options)
        add = QHBoxLayout()
        self.add_selected = self.action("Выбранные в очередь", lambda: self.enqueue(False), "primary")
        self.add_all = self.action("Все Confirmed в очередь", lambda: self.enqueue(True), "violet")
        add.addWidget(self.add_selected)
        add.addWidget(self.add_all)
        left_box.addLayout(add)

        self.queue_summary = QLabel("Очередь issue пуста")
        left_box.addWidget(self.queue_summary)
        self.queue_table = self.make_table(["Состояние", "Проект", "Место"])
        size_columns(self.queue_table, {0: 170, 1: 105}, 2)
        self.queue_table.itemSelectionChanged.connect(self.select_case)
        left_box.addWidget(self.queue_table, 2)
        queue_actions = QHBoxLayout()
        self.start_button = self.action("Подготовить очередь", self.start_queue, "success")
        self.stop_button = self.action("Остановить после текущего", self.stop_queue, "warning")
        queue_actions.addWidget(self.start_button)
        queue_actions.addWidget(self.stop_button)
        left_box.addLayout(queue_actions)
        manage = QHBoxLayout()
        self.retry_button = self.action("Повторить выбранные", self.retry_selected)
        self.remove_button = self.action("Убрать из очереди", self.remove_selected)
        manage.addWidget(self.retry_button)
        manage.addWidget(self.remove_button)
        left_box.addLayout(manage)

        right = QWidget()
        right_box = QVBoxLayout(right)
        right_box.setContentsMargins(6, 0, 0, 0)
        right_box.addWidget(QLabel("Заголовок issue"))
        self.title = QLineEdit()
        self.title.setMaxLength(500)
        self.title.textChanged.connect(self.editor_changed)
        right_box.addWidget(self.title)
        views = QTabWidget()
        self.editor = QPlainTextEdit()
        self.editor.setPlaceholderText("После подготовки здесь появится редактируемый Markdown.")
        self.editor.textChanged.connect(self.editor_changed)
        self.preview = QTextBrowser()
        self.preview.setOpenExternalLinks(False)
        views.addTab(self.editor, "Markdown")
        views.addTab(self.preview, "Просмотр")
        views.currentChanged.connect(lambda *_: self.preview.setMarkdown(self.editor.toPlainText()))
        right_box.addWidget(views, 1)
        self.detail = QLabel("Локальный черновик. Проверьте текст и доказательства перед передачей разработчику.")
        self.detail.setWordWrap(True)
        right_box.addWidget(self.detail)
        copy = QHBoxLayout()
        self.copy_title_button = self.action("Копировать заголовок", self.copy_title)
        self.copy_body_button = self.action("Копировать Markdown", self.copy_body, "primary")
        self.export_button = self.action("Сохранить .md…", self.export_md, "success")
        copy.addWidget(self.copy_title_button)
        copy.addWidget(self.copy_body_button)
        copy.addWidget(self.export_button)
        right_box.addLayout(copy)
        files = QHBoxLayout()
        self.brief_button = self.action("Кратко на английском…", self.rewrite_brief, "primary")
        files.addWidget(self.brief_button)
        self.open_files_button = self.action("Папка issue", self.open_case)
        self.open_poc_button = self.action("Файлы PoC", self.open_poc, "violet")
        files.addWidget(self.open_files_button)
        files.addWidget(self.open_poc_button)
        self.attach_poc_button = self.action("Вставить PoC в Markdown", self.attach_poc)
        files.addWidget(self.attach_poc_button)
        files.addStretch()
        right_box.addLayout(files)
        split.addWidget(left)
        split.addWidget(right)
        split.setSizes([620, 660])
        self.set_editor_enabled(False)
        self.update_actions()

    def selected_keys(self, table: QTableWidget) -> set[str]:
        return {str(table.item(index.row(), 0).data(Qt.ItemDataRole.UserRole))
                for index in table.selectionModel().selectedRows() if table.item(index.row(), 0)}

    def fill(self, table: QTableWidget, rows: list[tuple], keys: list[str]) -> None:
        from triage_gui_qt import set_rows
        table.blockSignals(True)
        set_rows(table, rows, keys)
        table.blockSignals(False)

    def refresh(self, *, force: bool = False) -> None:
        from triage_gui import list_saved_jobs
        if not force and not self.isVisible():
            return
        try:
            paths = list_saved_jobs(self.host.tool_directory) if self.scope.currentIndex() else [self.host.job]
            paths = [path for path in paths if (path / "job.json").is_file()]
            signature = tuple((str(path), (path / "decisions.jsonl").stat().st_mtime_ns,
                               (path / "job.json").stat().st_mtime_ns)
                              for path in paths if (path / "decisions.jsonl").is_file())
            if force or self._signature != signature or self._scope_job != self.host.job:
                self.candidates, errors = self.store.candidates(paths)
                self._signature = signature
                self._scope_job = self.host.job
                self.render_candidates()
                if errors:
                    self.host.set_message("; ".join(errors[:3]), error=True)
            self.render_queue()
        except (OSError, ValueError) as exc:
            self.host.set_message(f"Не удалось прочитать очередь issue: {exc}", error=True)

    def render_candidates(self) -> None:
        query = self.search.text().strip().casefold()
        visible = [item for item in self.candidates if not query or query in
                   " ".join(str(item[key]) for key in ("project", "location", "detector", "marker_id")).casefold()]
        self.fill(self.confirmed_table, [(item["project"], item["detector"], item["location"],
                                         "Проверен" if item["verification"] == "verified" else "Не завершена",
                                         self.poc_status(item)) for item in visible],
                  [item["id"] for item in visible])
        self.summary.setText(f"Confirmed: {len(self.candidates)} · показано: {len(visible)}. Markdown создаётся локально; PoC — по отдельному согласию.")
        self.update_actions()

    def poc_status(self, item: dict) -> str:
        try:
            job = self.store.job(item["job"])
            task = self.host.artifact_tasks.active.get((job.resolve(), item["marker_id"]))
            if task and "PoC" in task.kind:
                return "Готовится"
            directory, metadata = self.store.existing_poc(item)
            if directory:
                return "Не проверен" if metadata.get("status") == "generated_unverified" else "Нужны данные"
            return "Нет"
        except (OSError, ValueError):
            return "Недоступен"

    def save_workers(self, value: int) -> None:
        try:
            self.store.save_workers(value)
            if self.running:
                self.next_case()
        except (OSError, ValueError) as exc:
            self.host.set_message(str(exc), error=True)

    def render_queue(self) -> None:
        queue = self.store.load_queue()
        if queue != self.queue:
            self.queue = queue
            self.fill(self.queue_table, [(STATUS.get(item.get("status"), "Прервано"), item["project"], item["location"]) for item in queue],
                      [item["id"] for item in queue])
            for index, item in enumerate(queue):
                for col in range(3):
                    self.queue_table.item(index, col).setToolTip(str(item.get("message") or item["marker_id"]))
            if self.current:
                current = next((item for item in queue if item["id"] == self.current["id"]), None)
                if current:
                    self.current = current
                    if not self.editor.isEnabled():
                        self.current = None
                        self.select_case()
                    else:
                        self.detail.setText("Локальный черновик · " + str(current.get("message") or ""))
                        self.detail.setToolTip(str(self.store.case_dir(current) / "body.md"))
                else:
                    self.current = None
                    self._loading = True
                    self.title.clear()
                    self.editor.clear()
                    self.preview.clear()
                    self._loading = False
                    self.dirty = False
                    self.set_editor_enabled(False)
                    self.detail.setText("Элемент убран из очереди. Сохранённые файлы остались в папке issue.")
        pending = sum(item.get("status") == "queued" for item in queue)
        self.queue_summary.setText(f"Очередь issue: {pending} ожидают · {len(self._active_ids)} в работе · всего {len(queue)}")
        self.update_actions()

    def enqueue(self, all_confirmed: bool) -> None:
        keys = self.selected_keys(self.confirmed_table)
        chosen = self.candidates if all_confirmed else [item for item in self.candidates if item["id"] in keys]
        if not chosen:
            return
        try:
            count = self.store.enqueue(chosen, with_poc=self.with_poc.isChecked())
            self.render_queue()
            self.host.set_message(f"В очередь issue добавлено: {count}. Нажмите «Подготовить очередь».")
        except (OSError, ValueError, SystemExit) as exc:
            self.host.set_message(str(exc), error=True)

    def start_queue(self) -> None:
        if self.running:
            return
        self.render_queue()
        pending = [item for item in self.queue if item.get("status") == "queued"]
        if not pending:
            return
        needs_wording = any(not (self.store.case_dir(item) / "body.md").exists() for item in pending)
        has_poc = any(item.get("with_poc") for item in pending)
        if needs_wording or has_poc:
            details = "Для краткого английского issue в Codex будет передан текст сохранённого анализа выбранных маркеров. " if needs_wording else ""
            if has_poc:
                details += "Для PoC дополнительно будут переданы связанные фрагменты исходников. "
            answer = QMessageBox.question(
                self, "Подготовка issue через Codex",
                details +
                "Будет использована модель каждой задачи и учётная запись Codex на этом компьютере. "
                "Согласие действует только на текущий список очереди. Файлы сохраняются локально и не запускаются. Продолжить?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self._batch_ids = [item["id"] for item in pending]
        self.running = True
        self.stop_requested = False
        self.queue_timer.start()
        self.next_case()

    def next_case(self) -> None:
        if not self.running:
            return
        if (self.stop_requested or not self._batch_ids) and not self._active_ids:
            self.running = False
            self.queue_timer.stop()
            self.render_queue()
            errors = sum(item.get("status") in {"error", "poc_error", "needs_evidence"} for item in self.queue)
            self.host.set_message("Очередь issue остановлена." if self.stop_requested else
                                  f"Подготовка очереди завершена. Элементов с ошибками или недостающими данными: {errors}. Тексты доступны для копирования и сохранения .md.")
            return
        if self.stop_requested:
            return
        for key in list(self._batch_ids):
            if len(self._active_ids) >= self.workers.value():
                break
            try:
                item = next((item for item in self.store.load_queue() if item["id"] == key and item.get("status") == "queued"), None)
                if item is None:
                    self._batch_ids.remove(key)
                    continue
                job_dir = self.host.tool_directory / item["job"]
                if self.host.artifact_tasks.is_active(job_dir, item["marker_id"]):
                    continue
                item = self.store.claim(key)
                if item is None:
                    self._batch_ids.remove(key)
                    continue
                self._active_ids.add(key)
                self._batch_ids.remove(key)
                started = self.host.run_artifact(
                    job_dir, item["marker_id"],
                    lambda item=item: self.store.prepare(item, english_via_codex=True),
                    lambda result, key=key: self.case_done(key, result),
                    f"Issue: {item['project']} · {item['location']}…", kind="Issue/PoC" if item.get("with_poc") else "Issue",
                    failed=lambda exc, key=key: self.case_done(key, {"status": "error", "message": str(exc)}),
                )
                if not started:
                    self._active_ids.discard(key)
                    self._batch_ids.append(key)
                    self.store.update(key, status="queued")
                self.render_queue()
            except (OSError, ValueError, SystemExit) as exc:
                self.stop_requested = True
                self.host.set_message(str(exc), error=True)
                self.update_actions()
                break

    def case_done(self, key: str, result: dict) -> None:
        self._active_ids.discard(key)
        try:
            self.store.update(key, **result)
            self.render_queue()
            if self.current is None:
                for index, item in enumerate(self.queue):
                    if item["id"] == key:
                        self.queue_table.selectRow(index)
                        break
        except (OSError, ValueError, SystemExit) as exc:
            self.stop_requested = True
            self.host.set_message(str(exc), error=True)
        QTimer.singleShot(0, self.next_case)

    def stop_queue(self) -> None:
        self.stop_requested = True
        self.host.set_message("Очередь остановится после сохранения уже запущенных материалов.")
        self.update_actions()
        self.next_case()

    def retry_selected(self) -> None:
        keys = self.selected_keys(self.queue_table)
        try:
            self.store.retry(keys)
            self.render_queue()
        except (OSError, ValueError, SystemExit) as exc:
            self.host.set_message(str(exc), error=True)

    def remove_selected(self) -> None:
        if not self.save_editor():
            return
        try:
            self.store.remove(self.selected_keys(self.queue_table))
            self.render_queue()
        except (OSError, ValueError, SystemExit) as exc:
            self.host.set_message(str(exc), error=True)

    def set_editor_enabled(self, enabled: bool) -> None:
        self.title.setEnabled(enabled)
        self.editor.setEnabled(enabled)

    def select_case(self) -> None:
        index = self.queue_table.currentRow()
        if index < 0 or index >= len(self.queue):
            self.update_actions()
            return
        item = self.queue[index]
        if self.current and item["id"] == self.current["id"] and self.editor.isEnabled():
            self.update_actions()
            return
        if not self.save_editor():
            return
        try:
            directory = self.store.case_dir(item)
            self.current = item
            self._loading = True
            from developer_issues import read_json
            title_path = self.store._check(directory / "title.json")
            body_path = self.store._check(directory / "body.md")
            exists = title_path.is_file() and body_path.is_file()
            self.title.setText(read_json(title_path)["title"] if exists else "")
            self.editor.setPlainText(body_path.read_text(encoding="utf-8") if exists else "")
            self.preview.setMarkdown(self.editor.toPlainText())
            self.dirty = False
            self.set_editor_enabled(exists)
            self.detail.setText("Локальный черновик · " + str(item.get("message") or "Черновик ещё не подготовлен."))
            self.detail.setToolTip(str(directory / "body.md"))
        except (OSError, ValueError) as exc:
            self.host.set_message(str(exc), error=True)
        finally:
            self._loading = False
            self.update_actions()

    def editor_changed(self) -> None:
        if self._loading or self.current is None:
            return
        self.dirty = True
        self.save_timer.start()

    def save_editor(self) -> bool:
        self.save_timer.stop()
        if not self.dirty or self.current is None:
            return True
        try:
            self.store.save_draft(self.current, self.title.text(), self.editor.toPlainText())
            self.dirty = False
            return True
        except (OSError, ValueError) as exc:
            self.host.set_message(f"Черновик не сохранён: {exc}", error=True)
            return False

    def copy_title(self) -> None:
        if self.save_editor():
            QApplication.clipboard().setText(self.title.text())
            self.host.set_message("Заголовок issue скопирован.")

    def rewrite_brief(self) -> None:
        if self.current is None or self._rewriting_id or not self.save_editor():
            return
        item = dict(self.current)
        job_dir = self.host.tool_directory / item["job"]
        if self.host.artifact_tasks.is_active(job_dir, item["marker_id"]):
            self.host.set_message("Материалы этого маркера уже готовятся.")
            return
        answer = QMessageBox.question(
            self, "Краткий английский issue",
            "В Codex будет передан текст сохранённого анализа этого маркера. "
            "Черновик будет заменён коротким английским текстом: Description и Impact, без кода. "
            "Предыдущая версия сохранится в папке previous. Продолжить?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._rewriting_id = item["id"]
        self.set_editor_enabled(False)
        self.update_actions()
        def finished(result: dict) -> None:
            self._rewriting_id = None
            self.store.update(item["id"], **result)
            self.render_queue()
            if self.current and self.current["id"] == item["id"]:
                self.select_case()
            self.host.set_message(result["message"])
        def failed(exc: Exception) -> None:
            self._rewriting_id = None
            if self.current and self.current["id"] == item["id"]:
                self.select_case()
            self.update_actions()
            self.host.set_message(str(exc), error=True)
        started = self.host.run_artifact(
            job_dir, item["marker_id"], lambda: self.store.rewrite_brief(item), finished,
            "Готовится краткий английский issue…", kind="Issue EN", failed=failed,
        )
        if not started:
            failed(ValueError("Материалы этого маркера уже готовятся."))

    def copy_body(self) -> None:
        if self.save_editor():
            QApplication.clipboard().setText(self.editor.toPlainText())
            self.host.set_message("Markdown issue скопирован.")

    def export_md(self) -> None:
        if self.current is None or not self.save_editor():
            return
        filename, _ = QFileDialog.getSaveFileName(self, "Сохранить issue", "issue.md", "Markdown (*.md)")
        if filename:
            try:
                Path(filename).write_text(f"# {self.title.text()}\n\n{self.editor.toPlainText()}", encoding="utf-8")
                self.host.set_message(f"Issue сохранён: {filename}")
            except OSError as exc:
                self.host.set_message(str(exc), error=True)

    def open_case(self) -> None:
        if self.current and self.save_editor():
            self.open_directory(str(self.store.case_dir(self.current)))

    def open_poc(self) -> None:
        item = next((item for item in self.queue if self.current and item["id"] == self.current["id"]), {})
        try:
            if item.get("poc"):
                path = self.store._check(Path(item["poc"]))
                if path.is_dir():
                    self.open_directory(str(path))
        except (OSError, ValueError) as exc:
            self.host.set_message(str(exc), error=True)

    def attach_poc(self) -> None:
        if self.current is None:
            return
        try:
            attachment = self.store.poc_attachment(self.current)
            if attachment.strip() in self.editor.toPlainText():
                self.host.set_message("Этот PoC уже включён в Markdown.")
                return
            self.editor.appendPlainText("\n" + attachment)
            self.save_editor()
            self.host.set_message("PoC включён в Markdown с пометкой «не запускался».")
        except (OSError, ValueError) as exc:
            self.host.set_message(str(exc), error=True)

    def open_directory(self, path: str) -> None:
        try:
            os.startfile(path)
        except OSError as exc:
            self.host.set_message(str(exc), error=True)

    def edit_template(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("Шаблон Markdown issue")
        dialog.resize(800, 700)
        layout = QVBoxLayout(dialog)
        hint = QLabel("Поля: " + ", ".join("$" + key for key in report_fields({}, {})) + ". Для обычного $ пишите $$.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        editor = QPlainTextEdit(self.store.template())
        layout.addWidget(editor)
        actions = QHBoxLayout()
        actions.addWidget(self.action("Вернуть стандартный", lambda: editor.setPlainText(DEFAULT_TEMPLATE)))
        def save() -> None:
            try:
                self.store.save_template(editor.toPlainText())
                dialog.accept()
            except (OSError, ValueError) as exc:
                QMessageBox.warning(dialog, "Ошибка шаблона", str(exc))
        actions.addStretch()
        actions.addWidget(self.action("Сохранить", save, "success"))
        actions.addWidget(self.action("Отмена", dialog.reject))
        layout.addLayout(actions)
        dialog.exec()

    def update_actions(self) -> None:
        if not hasattr(self, "open_poc_button"):
            return
        rewriting = self.current is not None and self.current["id"] == self._rewriting_id
        if rewriting:
            self.set_editor_enabled(False)
        self.brief_button.setEnabled(self.editor.isEnabled() and not self._rewriting_id)
        self.add_selected.setEnabled(bool(self.selected_keys(self.confirmed_table)))
        self.add_all.setEnabled(bool(self.candidates))
        self.with_poc.setEnabled(True)
        self.start_button.setEnabled(not self.running and any(item.get("status") == "queued" for item in self.queue))
        self.stop_button.setEnabled(self.running and not self.stop_requested)
        selected = bool(self.selected_keys(self.queue_table))
        keys = self.selected_keys(self.queue_table)
        selected_idle = selected and all(item.get("status") != "running" and item["id"] != self._rewriting_id
                                        for item in self.queue if item["id"] in keys)
        self.retry_button.setEnabled(selected_idle)
        self.remove_button.setEnabled(selected_idle)
        has_text = self.current is not None and self.editor.isEnabled()
        for action in (self.copy_title_button, self.copy_body_button, self.export_button, self.open_files_button):
            action.setEnabled(has_text)
        item = next((item for item in self.queue if self.current and item["id"] == self.current["id"]), {})
        self.open_poc_button.setEnabled(bool(item.get("poc")))
        self.attach_poc_button.setEnabled(has_text and item.get("status") == "poc_unverified")
