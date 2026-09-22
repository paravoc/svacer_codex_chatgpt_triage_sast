"""Graphical project/source setup; network discovery never blocks the UI thread."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import re

from PySide6.QtCore import QEvent, QSize, Qt, QTimer
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFrame, QHBoxLayout, QLabel,
                              QLineEdit, QListWidget, QListWidgetItem, QProgressBar,
                              QPushButton, QScrollArea, QVBoxLayout, QWidget)

from project_setup import (COMMIT_RE, create_project, list_remote_refs, repository_search_query,
                           repository_url, scope_fields, scope_options,
                           search_public_github_repositories, snapshot_fields, source_fields,
                           update_project_source)


def ref_matches_query(name: str, query: str) -> bool:
    name, query = name.casefold(), query.strip().casefold()
    if re.fullmatch(r"v?\d[\d.]*", query):
        # A version prefix 2.11 must not match the tail of e.g. v2.12.11.
        return re.search(r"(?<![\w.])v?" + re.escape(query.removeprefix("v")), name) is not None
    return query in name


def preferred_project_dialog_size(available_width: int, available_height: int) -> QSize:
    """Use most of the screen while leaving room for the taskbar and window frame."""
    return QSize(
        max(1, min(1320, int(available_width) - 48)),
        max(1, min(1000, int(available_height) - 48)),
    )


class ProjectSetupDialog(QDialog):
    def __init__(self, parent, root: Path, settings: dict, *, job: Path | None = None,
                 job_data: dict | None = None, button_factory=None, scope_loader=None):
        super().__init__(parent)
        self.root, self.settings, self.job = root, settings, job
        self.job_data = job_data or {}
        self.created_job = None
        self.refs = []
        self.repository_results = []
        self.loaded_url = ""
        self.future = None
        self.repository_future = None
        self.scope_future = None
        self.scope_loader = scope_loader
        self._updating_scope = False
        self._scope_generation = 0
        self._scope_base = None
        self._closed = False
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="git-refs")
        self.setWindowTitle("Исходники проекта" if job else "Новый проект")
        available = self.screen().availableGeometry()
        preferred = preferred_project_dialog_size(available.width(), available.height())
        self.resize(preferred)
        self.setMinimumWidth(min(760, preferred.width()))
        self.setMinimumHeight(min(520, preferred.height()))
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        self.form_scroll = QScrollArea()
        self.form_scroll.setWidgetResizable(True)
        self.form_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.form_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.form_body = QWidget()
        self.form_body.setObjectName("projectSetupForm")
        self.form_body.setStyleSheet("QWidget#projectSetupForm { background: #111317; }")
        self.form_scroll.viewport().setObjectName("projectSetupViewport")
        self.form_scroll.viewport().setStyleSheet("QWidget#projectSetupViewport { background: #111317; }")
        self.form_scroll.setWidget(self.form_body)
        outer.addWidget(self.form_scroll, 1)
        layout = QVBoxLayout(self.form_body)
        self.form_layout = layout
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(12)

        def text(value, kind=None):
            widget = QLabel(value)
            widget.setWordWrap(True)
            widget.setTextFormat(Qt.TextFormat.PlainText)
            if kind:
                widget.setObjectName(kind)
            layout.addWidget(widget)
            return widget

        def action(value, tone="neutral"):
            widget = button_factory(value, tone=tone) if button_factory else QPushButton(value)
            widget.setProperty("tone", tone)
            widget.setAutoDefault(False)
            return widget

        text("Выберите точную версию исходников", "title")
        text("Название ветки в Svacer не всегда совпадает с Git-тегом. "
             "Выберите версию, на которой получен снимок: существование тега само по себе не подтверждает это соответствие.", "muted")
        text("1. Проект и снимок Svacer (не Git-ветка)")
        row = QHBoxLayout()
        self.snapshot = QLineEdit(str(self.job_data.get("snapshot_url") or ""))
        self.snapshot.setPlaceholderText("https://svacer…/project/…/branch/…/snapshot/…")
        self.snapshot.setReadOnly(job is not None)
        row.addWidget(self.snapshot, 1)
        self.scope_button = action("Выбрать снимок", "primary")
        self.scope_button.setVisible(job is None)
        self.scope_button.clicked.connect(self.load_scope)
        row.addWidget(self.scope_button)
        layout.addLayout(row)
        self.scope_frame = QFrame()
        scope_layout = QVBoxLayout(self.scope_frame)
        scope_layout.setContentsMargins(0, 0, 0, 0)
        self.svacer_branch = QComboBox()
        self.svacer_snapshot = QComboBox()
        for widget, name in ((self.svacer_branch, "Ветка Svacer"),
                             (self.svacer_snapshot, "Снимок Svacer")):
            widget.setAccessibleName(name)
            widget.setMaxVisibleItems(6)
            scope_layout.addWidget(QLabel(name))
            scope_layout.addWidget(widget)
        self.scope_status = QLabel()
        self.scope_status.setWordWrap(True)
        self.scope_status.setTextFormat(Qt.TextFormat.PlainText)
        self.scope_status.setObjectName("muted")
        scope_layout.addWidget(self.scope_status)
        layout.addWidget(self.scope_frame)
        self.scope_frame.hide()
        text("2. Git-репозиторий")
        search_row = QHBoxLayout()
        self.repository_query = QLineEdit()
        self.repository_query.setPlaceholderText("Название или ключевые слова, например luajit")
        search_row.addWidget(self.repository_query, 1)
        self.repository_search_button = action("Найти на GitHub", "primary")
        self.repository_search_button.clicked.connect(self.search_repositories)
        search_row.addWidget(self.repository_search_button)
        layout.addLayout(search_row)
        self.repository_frame = QFrame()
        self.repository_frame.setObjectName("repositoryFrame")
        self.repository_frame.setFixedHeight(150)
        self.repository_frame.setStyleSheet("""
            QFrame#repositoryFrame { background: #20242b; border: 1px solid #3a424c; border-radius: 8px; }
            QListWidget { background: transparent; color: #e7e9ed; border: 0; outline: 0; }
            QListWidget::item { padding: 3px 7px; border-radius: 4px; }
            QListWidget::item:hover { background: #2c3a47; }
            QListWidget::item:selected { background: #355e81; color: #f1f9ff; }
        """)
        repository_results_layout = QVBoxLayout(self.repository_frame)
        repository_results_layout.setContentsMargins(8, 6, 8, 6)
        repository_results_layout.setSpacing(4)
        self.repository_count = QLabel("Введите ключевые слова и нажмите «Найти на GitHub»")
        self.repository_count.setObjectName("muted")
        repository_results_layout.addWidget(self.repository_count)
        self.repository_list = QListWidget()
        self.repository_list.setAccessibleName("Найденные публичные GitHub-репозитории")
        self.repository_list.setUniformItemSizes(True)
        self.repository_list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.repository_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.repository_list.setTextElideMode(Qt.TextElideMode.ElideRight)
        repository_results_layout.addWidget(self.repository_list)
        layout.addWidget(self.repository_frame)
        text("GitHub получает только введённую поисковую фразу. Данные Svacer, маркеры и исходники не отправляются.", "muted")
        row = QHBoxLayout()
        self.repository = QLineEdit(str(self.job_data.get("repository_url") or ""))
        self.repository.setPlaceholderText("https://github.com/owner/repository.git")
        row.addWidget(self.repository, 1)
        self.load_button = action("Теги и ветки", "primary")
        self.load_button.clicked.connect(self.load_refs)
        row.addWidget(self.load_button)
        layout.addLayout(row)
        text("Список запрашивается напрямую через Git: GitHub, GitLab и другие Git-серверы. "
             "Исходники и данные Svacer при этом не отправляются.", "muted")
        text("3. Версия исходников")
        row = QHBoxLayout()
        self.kind = QComboBox()
        for title, key in (("Теги", "tag"), ("Ветки", "branch"), ("Точный commit SHA", "commit")):
            self.kind.addItem(title, key)
        row.addWidget(self.kind)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Поиск версии, например 2.11.2")
        row.addWidget(self.search, 1)
        layout.addLayout(row)
        self.ref_frame = QFrame()
        self.ref_frame.setObjectName("refFrame")
        self.ref_frame.setFixedHeight(166)
        self.ref_frame.setStyleSheet("""
            QFrame#refFrame { background: #20242b; border: 1px solid #3a424c; border-radius: 8px; }
            QListWidget { background: transparent; color: #e7e9ed; border: 0; outline: 0; }
            QListWidget::item { padding: 3px 7px; border-radius: 4px; }
            QListWidget::item:hover { background: #2c3a47; }
            QListWidget::item:selected { background: #355e81; color: #f1f9ff; }
        """)
        refs_layout = QVBoxLayout(self.ref_frame)
        refs_layout.setContentsMargins(8, 6, 8, 6)
        refs_layout.setSpacing(4)
        self.ref_count = QLabel("Загрузите теги и ветки, затем выберите версию")
        self.ref_count.setObjectName("muted")
        refs_layout.addWidget(self.ref_count)
        self.ref_list = QListWidget()
        self.ref_list.setAccessibleName("Подходящие версии исходников")
        self.ref_list.setUniformItemSizes(True)
        self.ref_list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.ref_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.ref_list.setTextElideMode(Qt.TextElideMode.ElideRight)
        refs_layout.addWidget(self.ref_list)
        layout.addWidget(self.ref_frame)
        self.commit = QLineEdit()
        self.commit.setPlaceholderText("Полный commit SHA (40 или 64 шестнадцатеричных символа)")
        self.commit.hide()
        layout.addWidget(self.commit)
        self.selected_hint = text("Версия ещё не выбрана.", "muted")
        # Reserve space for ref, full SHA and the manual-SHA notice. Wrapped
        # QLabel height hints can otherwise overlap a fixed-height results frame.
        self.selected_hint.setMinimumHeight(self.selected_hint.fontMetrics().lineSpacing() * 3)
        self.selected_hint.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        if self.job_data.get("git_ref"):
            text(f"Сейчас сохранено: {self.job_data['git_ref']}. Выберите версию из загруженного списка.", "muted")
            self.search.setText(str(self.job_data["git_ref"]))
        self.confirm = QCheckBox("Подтверждаю: выбранная Git-ревизия соответствует\nвыбранному снимку Svacer")
        layout.addWidget(self.confirm)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.hide()
        layout.addWidget(self.progress)
        self.message = text(
            "После создания проекта маркеры загрузятся автоматически. "
            "Анализ не запускается, разметка Svacer не меняется.", "muted",
        )
        layout.addStretch(1)
        self.validation = QLabel()
        self.validation.setWordWrap(True)
        self.validation.setTextFormat(Qt.TextFormat.PlainText)
        self.validation.setStyleSheet("color: #e0b965;")
        outer.addWidget(self.validation)
        row = QHBoxLayout()
        cancel = action("Отмена")
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        row.addStretch(1)
        self.save_button = action("Сохранить исходники" if job else "Создать проект", "success")
        self.save_button.clicked.connect(self.save_project)
        row.addWidget(self.save_button)
        outer.addLayout(row)
        self.timer = QTimer(self)
        self.timer.setInterval(80)
        self.timer.timeout.connect(self.drain_refs)
        self.scope_timer = QTimer(self)
        self.scope_timer.setInterval(80)
        self.scope_timer.timeout.connect(self.drain_scope)
        self.repository_timer = QTimer(self)
        self.repository_timer.setInterval(80)
        self.repository_timer.timeout.connect(self.drain_repository_search)
        self.finished.connect(self.shutdown)
        self.repository_query.returnPressed.connect(self.search_repositories)
        self.repository_query.textChanged.connect(self.invalidate_repository_results)
        self.repository_list.currentItemChanged.connect(self.repository_selected)
        self.repository.textChanged.connect(self.invalidate_refs)
        self.kind.currentIndexChanged.connect(self.filter_refs)
        self.search.textChanged.connect(self.filter_refs)
        self.ref_list.currentItemChanged.connect(self.selection_changed)
        self.commit.textChanged.connect(self.selection_changed)
        self.snapshot.textChanged.connect(self.snapshot_changed)
        self.svacer_branch.currentIndexChanged.connect(self.branch_changed)
        self.svacer_snapshot.currentIndexChanged.connect(self.snapshot_selected)
        self.confirm.toggled.connect(self.update_save_state)
        self.update_save_state()

    def fit_form_height(self):
        if not hasattr(self, "form_layout"):
            return
        layout = self.form_layout
        width = self.form_scroll.viewport().width()
        height = max(layout.minimumSize().height(), layout.totalHeightForWidth(width))
        if self.form_body.minimumHeight() != height:
            self.form_body.setMinimumHeight(height)
        # Keep the actions visible even at Windows scaling / small screen sizes.
        available = self.screen().availableGeometry().height() - 48
        self.setMinimumHeight(min(520, max(250, available)))
        if self.height() > available:
            self.resize(self.width(), max(self.minimumHeight(), available))

    def event(self, event):
        result = super().event(event)
        if event.type() in (QEvent.Type.LayoutRequest, QEvent.Type.Show):
            self.fit_form_height()
        return result

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.fit_form_height()

    def shutdown(self, *_):
        self._closed = True
        self.timer.stop()
        self.scope_timer.stop()
        self.repository_timer.stop()
        self.executor.shutdown(wait=False, cancel_futures=True)

    def snapshot_changed(self, *_):
        self.confirm.setChecked(False)
        if not self._updating_scope:
            self._scope_generation += 1
            self._scope_base = None
            self.svacer_branch.blockSignals(True)
            self.svacer_snapshot.blockSignals(True)
            self.svacer_branch.clear()
            self.svacer_snapshot.clear()
            self.svacer_branch.blockSignals(False)
            self.svacer_snapshot.blockSignals(False)
            self.scope_frame.hide()
        self.update_save_state()

    def set_scope_url(self, url):
        self._updating_scope = True
        try:
            self.snapshot.setText(url)
        finally:
            self._updating_scope = False

    def load_scope(self):
        if self.scope_future is not None or self.job is not None:
            return
        self.scope_frame.show()
        try:
            scope = scope_fields(self.snapshot.text(), str(self.settings.get("svacer_url") or ""))
            if self.scope_loader is None:
                raise ValueError("Вставьте полную ссылку на снимок или откройте форму из подключённого приложения.")
        except ValueError as exc:
            self.scope_status.setText(str(exc))
            return
        self._scope_base = scope["scope_url"].split("/branch/")[0]
        for combo in (self.svacer_branch, self.svacer_snapshot):
            combo.blockSignals(True)
            combo.clear()
            combo.blockSignals(False)
        self.confirm.setChecked(False)
        self.scope_request("branch", scope)

    def scope_request(self, kind, scope):
        generation = self._scope_generation
        def work():
            return scope_options(self.scope_loader(kind, scope), kind, scope["project_id"])
        self.scope_request_data = (generation, kind, scope)
        self.scope_future = self.executor.submit(work)
        self.scope_status.setText("Загружаю ветки Svacer…" if kind == "branch" else "Загружаю снимки Svacer…")
        self.scope_button.setEnabled(False)
        self.svacer_branch.setEnabled(False)
        self.svacer_snapshot.setEnabled(False)
        self.update_save_state()
        self.scope_timer.start()

    def drain_scope(self):
        if self._closed or self.scope_future is None or not self.scope_future.done():
            return
        self.scope_timer.stop()
        future, self.scope_future = self.scope_future, None
        generation, kind, scope = self.scope_request_data
        self.scope_button.setEnabled(True)
        self.svacer_branch.setEnabled(True)
        self.svacer_snapshot.setEnabled(True)
        if generation != self._scope_generation:
            self.update_save_state()
            return
        try:
            rows = future.result()
        except Exception as exc:
            self.scope_status.setText(str(exc) if isinstance(exc, ValueError)
                                      else "Не удалось загрузить список. Проверьте подключение к Svacer и повторите.")
            self.update_save_state()
            return
        combo = self.svacer_branch if kind == "branch" else self.svacer_snapshot
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("Выберите ветку…" if kind == "branch" else "Выберите снимок…", None)
        for row in rows:
            combo.addItem(row["label"], row)
        combo.blockSignals(False)
        self.scope_status.setText("Выберите ветку Svacer, затем конкретный снимок."
                                  if rows and kind == "branch" else
                                  "Выберите снимок: последний снимок не обязательно соответствует Git-ветке."
                                  if rows else "В этой ветке нет снимков." if kind == "snapshot"
                                  else "В проекте нет доступных веток Svacer.")
        if kind == "branch":
            # A branch explicitly supplied in the URL is already a user choice.
            index = next((i + 1 for i, row in enumerate(rows) if row["id"] == scope.get("branch_id")), 0)
            if index:
                combo.setCurrentIndex(index)
        self.update_save_state()

    def branch_changed(self, *_):
        if self.scope_future is not None or not self._scope_base:
            return
        row = self.svacer_branch.currentData()
        self.svacer_snapshot.blockSignals(True)
        self.svacer_snapshot.clear()
        self.svacer_snapshot.blockSignals(False)
        self.set_scope_url(self._scope_base + (f"/branch/{row['id']}" if row else ""))
        if row:
            scope = scope_fields(self.snapshot.text(), str(self.settings.get("svacer_url") or ""))
            self.scope_request("snapshot", scope)

    def snapshot_selected(self, *_):
        row, branch = self.svacer_snapshot.currentData(), self.svacer_branch.currentData()
        if not self._scope_base or not branch:
            return
        url = f"{self._scope_base}/branch/{branch['id']}"
        self.set_scope_url(url + (f"/snapshot/{row['id']}" if row else ""))
        if row:
            self.scope_status.setText(
                f"Снимок выбран. Commit из метаданных Svacer: {row['commit']}"
                if row.get("commit") else
                "Снимок выбран. Выберите соответствующий Git-тег, ветку или точный SHA ниже."
            )

    def invalidate_refs(self, *_):
        self.loaded_url = ""
        self.refs = []
        self.filter_refs()

    def invalidate_repository_results(self, *_):
        if self.repository_future is None:
            self.repository_results = []
            self.repository_list.clear()
            self.repository_count.setText("Введите ключевые слова и нажмите «Найти на GitHub»")

    def search_repositories(self):
        if self.repository_future is not None:
            return
        try:
            query = repository_search_query(self.repository_query.text())
        except ValueError as exc:
            self.repository_count.setText(str(exc))
            return
        self.repository_results = []
        self.repository_list.clear()
        self.repository_request_query = query
        self.repository_future = self.executor.submit(search_public_github_repositories, query)
        self.repository_search_button.setEnabled(False)
        self.repository_count.setText(f"Ищу «{query}» на GitHub…")
        self.repository_timer.start()

    def drain_repository_search(self):
        if self._closed or self.repository_future is None or not self.repository_future.done():
            return
        self.repository_timer.stop()
        future, self.repository_future = self.repository_future, None
        self.repository_search_button.setEnabled(True)
        try:
            current_query = repository_search_query(self.repository_query.text())
        except ValueError:
            current_query = ""
        if current_query != self.repository_request_query:
            self.repository_count.setText("Фраза изменена — запустите поиск ещё раз.")
            return
        try:
            rows = future.result()
        except Exception as exc:
            self.repository_count.setText(
                str(exc) if isinstance(exc, ValueError) else "Не удалось выполнить поиск GitHub."
            )
            return
        self.repository_results = rows
        for row in rows:
            suffix = f" · ★ {row['stars']}" + (f" · {row['language']}" if row["language"] else "")
            item = QListWidgetItem(str(row["name"]) + suffix)
            item.setData(Qt.ItemDataRole.UserRole, row)
            details = str(row["url"])
            if row["description"]:
                details += "\n" + str(row["description"])
            item.setToolTip(details)
            item.setSizeHint(QSize(0, 28))
            self.repository_list.addItem(item)
        self.repository_count.setText(
            f"Найдено: {len(rows)} · выберите репозиторий" if rows else
            "Ничего не найдено — измените поисковую фразу"
        )

    def repository_selected(self, item, _previous=None):
        if item is None:
            return
        row = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(row, dict):
            return
        self.repository.setText(str(row["url"]))
        self.message.setText(
            f"Выбран {row['name']}. Теперь загрузите теги и ветки и укажите точную версию снимка."
        )

    def load_refs(self):
        if self.future is not None:
            return
        try:
            url = repository_url(self.repository.text())
        except ValueError as exc:
            self.message.setText(str(exc))
            return
        self.refs, self.loaded_url = [], ""
        self.filter_refs()
        self.request_url = url
        self.future = self.executor.submit(list_remote_refs, url)
        self.load_button.setEnabled(False)
        self.progress.show()
        self.message.setText("Получаю теги и ветки… Окно можно закрыть, анализ не запускается.")
        self.update_save_state()
        self.timer.start()

    def drain_refs(self):
        if self._closed or self.future is None or not self.future.done():
            return
        self.timer.stop()
        future, self.future = self.future, None
        self.load_button.setEnabled(True)
        self.progress.hide()
        if self.repository.text().strip().rstrip("/") != self.request_url:
            self.message.setText("URL изменён. Загрузите список для нового репозитория.")
            self.update_save_state()
            return
        try:
            refs = future.result()
        except Exception as exc:
            self.message.setText(str(exc) if isinstance(exc, ValueError) else "Не удалось получить список версий.")
            self.update_save_state()
            return
        self.loaded_url, self.refs = self.request_url, refs
        tags = sum(r["kind"] == "tag" for r in refs)
        branches = len(refs) - tags
        if not tags and branches and self.kind.currentData() == "tag":
            self.kind.setCurrentIndex(1)
            self.search.clear()
        elif tags and not branches and self.kind.currentData() == "branch":
            self.kind.setCurrentIndex(0)
            self.search.clear()
        self.filter_refs()
        self.message.setText(f"Загружено: тегов {tags}, веток {len(refs) - tags}. Выберите нужную версию."
                             if refs else "Тегов и веток нет: репозиторий может быть пустым. "
                             "Если известен существующий commit, выберите «Точный commit SHA».")

    def filter_refs(self, *_):
        item = self.ref_list.currentItem()
        previous = item.data(Qt.ItemDataRole.UserRole) if item else None
        kind = self.kind.currentData()
        manual = kind == "commit"
        self.search.setVisible(not manual)
        self.ref_frame.setVisible(not manual)
        self.commit.setVisible(manual)
        self.ref_list.blockSignals(True)
        self.ref_list.setUpdatesEnabled(False)
        self.ref_list.clear()
        query = self.search.text().strip().casefold()
        total = 0
        selected = -1
        for row in self.refs:
            if row["kind"] != kind:
                continue
            total += 1
            if ref_matches_query(row["name"], query):
                item = QListWidgetItem(f"{row['name']}  ·  {row['commit'][:12]}")
                item.setData(Qt.ItemDataRole.UserRole, row)
                item.setToolTip(f"{row['ref']}\nCommit: {row['commit']}")
                item.setSizeHint(QSize(0, 28))
                self.ref_list.addItem(item)
                if previous == row:
                    selected = self.ref_list.count() - 1
        self.ref_list.setCurrentRow(selected)
        self.ref_list.setUpdatesEnabled(True)
        self.ref_list.blockSignals(False)
        count = self.ref_list.count()
        self.ref_count.setText(
            "Загрузите теги и ветки, затем выберите версию" if not self.loaded_url else
            f"Найдено: {count} из {total} · выберите строку" if count else
            "Совпадений нет — измените поиск" if query else "Для этого типа версий список пуст"
        )
        self.selection_changed()

    def selection(self):
        if self.kind.currentData() == "commit":
            commit = self.commit.text().strip().lower()
            if COMMIT_RE.fullmatch(commit):
                return {"kind": "commit", "ref": commit, "name": commit, "commit": commit}
            return None
        item = self.ref_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if self.loaded_url and item else None

    def selection_changed(self, *_):
        self.confirm.setChecked(False)
        selection = self.selection()
        if selection:
            message = f"{selection['ref']}\nCommit: {selection['commit']}"
            if selection["kind"] == "commit":
                message += "\nДоступность SHA проверяется при загрузке исходников, до запуска модели."
            elif selection["kind"] == "branch":
                message += "\nЭто последний commit ветки на момент загрузки списка. Сохраняется именно этот SHA."
            self.selected_hint.setText(message)
        else:
            self.selected_hint.setText("Выберите версию из списка." if self.refs else "Версия ещё не выбрана.")
        self.update_save_state()

    def update_save_state(self, *_):
        problems = []
        if self.future is not None or self.scope_future is not None:
            problems.append("Дождитесь окончания загрузки списка.")
        if self.job is None:
            try:
                snapshot_fields(self.snapshot.text(), str(self.settings.get("svacer_url") or ""))
            except ValueError as exc:
                problems.append(str(exc))
        try:
            repository_url(self.repository.text())
        except ValueError as exc:
            problems.append(str(exc))
        selection = self.selection()
        if not selection:
            problems.append("Введите полный SHA: 40 или 64 символа 0–9, a–f. Короткий SHA не подходит."
                            if self.kind.currentData() == "commit" else "Выберите Git-тег или Git-ветку из списка.")
        else:
            try:
                source_fields(self.repository.text(), selection)
            except ValueError as exc:
                if str(exc) not in problems:
                    problems.append(str(exc))
            if not self.confirm.isChecked():
                problems.append("Подтвердите галочкой соответствие Git-ревизии выбранному снимку Svacer.")
        self.save_button.setEnabled(not problems)
        explanation = "\n".join(problems)
        self.validation.setText(explanation)
        self.validation.setVisible(bool(problems))
        self.save_button.setToolTip(explanation or "Создать проект и автоматически загрузить маркеры")

    def save_project(self):
        self.update_save_state()
        if not self.save_button.isEnabled():
            return
        try:
            selection = self.selection()
            source_fields(self.repository.text(), selection)
            if self.job is None:
                self.created_job = create_project(self.root, self.settings, self.snapshot.text(),
                                                  self.repository.text(), selection)
            else:
                update_project_source(self.job, self.root, self.repository.text(), selection)
        except (ValueError, OSError) as exc:
            self.message.setText(str(exc))
            return
        self.accept()
