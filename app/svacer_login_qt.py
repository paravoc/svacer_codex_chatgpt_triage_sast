"""Themed, asynchronous Svacer sign-in for the desktop interface."""
from concurrent.futures import ThreadPoolExecutor
import threading

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
                              QProgressBar, QPushButton, QVBoxLayout)

from svacer_connection import LoginCancelled, LoginError, start_connection, stop_owned_process
from triage_connector.client import server_url as checked_server_url
from triage_queue import atomic_write_json, complete_desktop_settings


class SvacerLoginDialog(QDialog):
    def __init__(self, parent, app_directory, server_url, mcp_url, token, *, button_factory=None):
        super().__init__(parent)
        self.app_directory, self.server_url = app_directory, server_url
        self.mcp_url, self.token = mcp_url, token
        self.future = None
        self.cancel = threading.Event()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="svacer-login")
        self._closed = False
        self._cleaning = False
        self.setWindowTitle("Вход в Svacer")
        self.setMinimumWidth(450)
        self.resize(540, 550)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(14)

        def text(value, name=None):
            widget = QLabel(value)
            widget.setTextFormat(Qt.TextFormat.PlainText)
            widget.setWordWrap(True)
            if name:
                widget.setObjectName(name)
            return widget

        layout.addWidget(text("Вход в Svacer", "title"))
        layout.addWidget(text("Подключение к вашему серверу без дополнительных окон консоли.", "muted"))
        card = QFrame()
        card.setObjectName("subcard")
        form = QVBoxLayout(card)
        form.setContentsMargins(16, 14, 16, 16)
        form.setSpacing(10)
        form.addWidget(text("Сервер", "muted"))
        try:
            display_server = checked_server_url(server_url)
        except Exception:
            display_server = str(server_url or "").strip()
        self.server_edit = QLineEdit(display_server)
        self.server_edit.setMinimumHeight(40)
        self.server_edit.setPlaceholderText("https://svacer.example.company")
        self.server_edit.setMaxLength(2048)
        self.server_edit.setAccessibleName("Адрес сервера Svacer")
        form.addWidget(self.server_edit)
        self.server_hint = text("Можно вставить адрес сервера или ссылку на проект/снимок.", "muted")
        form.addWidget(self.server_hint)
        form.addWidget(text("Логин"))
        self.login_edit = QLineEdit()
        self.login_edit.setMinimumHeight(40)
        self.login_edit.setPlaceholderText("Ваш логин Svacer")
        self.login_edit.setMaxLength(512)
        self.login_edit.setAccessibleName("Логин Svacer")
        form.addWidget(self.login_edit)
        form.addWidget(text("Пароль"))
        self.password_edit = QLineEdit()
        self.password_edit.setMinimumHeight(40)
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_edit.setMaxLength(4096)
        self.password_edit.setAccessibleName("Пароль Svacer")
        self.password_edit.setPlaceholderText("Введите пароль")
        form.addWidget(self.password_edit)
        layout.addWidget(card)
        layout.addWidget(text("Пароль не сохраняется на диск. Подключение остаётся в фоне после "
                              "закрытия приложения; для выхода нажмите «Выйти из Svacer».", "muted"))
        self.http_warning = text(
            "Сервер использует HTTP без шифрования. Подключайтесь только через доверенную сеть/VPN."
        )
        self.http_warning.setStyleSheet("color: #e5bd79;")
        layout.addWidget(self.http_warning)
        self.status = text("Введите данные для входа.", "muted")
        layout.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(5)
        self.progress.hide()
        layout.addWidget(self.progress)
        layout.addStretch()
        buttons = QHBoxLayout()
        make = button_factory or (lambda caption, **_kwargs: QPushButton(caption))
        self.cancel_button = make("Отмена")
        self.submit_button = make("Войти", tone="success")
        self.submit_button.setDefault(True)
        self.cancel_button.setAutoDefault(False)
        buttons.addWidget(self.cancel_button)
        buttons.addStretch()
        buttons.addWidget(self.submit_button)
        layout.addLayout(buttons)
        self.timer = QTimer(self)
        self.timer.setInterval(80)
        self.timer.timeout.connect(self.poll)
        self.submit_button.clicked.connect(self.submit)
        self.cancel_button.clicked.connect(self.reject)
        self.server_edit.textChanged.connect(self.update_submit)
        self.login_edit.textChanged.connect(self.update_submit)
        self.password_edit.textChanged.connect(self.update_submit)
        self.server_edit.returnPressed.connect(self.login_edit.setFocus)
        self.login_edit.returnPressed.connect(self.password_edit.setFocus)
        self.password_edit.returnPressed.connect(self.submit)
        self.finished.connect(self.shutdown)
        self.update_submit()
        (self.login_edit if display_server else self.server_edit).setFocus()

    def validated_server_url(self):
        try:
            return checked_server_url(self.server_edit.text())
        except Exception:
            return ""

    def save_server_url(self, value):
        path = self.app_directory / "svacer-settings.json"
        settings = {}
        if path.is_file():
            try:
                import json
                loaded = json.loads(path.read_text(encoding="utf-8-sig"))
                if isinstance(loaded, dict):
                    settings = loaded
            except (OSError, ValueError):
                settings = {}
        settings["svacer_url"] = value
        settings.setdefault("mcp_url", self.mcp_url)
        settings = complete_desktop_settings(settings)
        atomic_write_json(path, settings)

    def update_submit(self):
        valid_server = self.validated_server_url()
        self.server_hint.setText(
            f"Сервер для подключения: {valid_server}"
            if valid_server and valid_server != self.server_edit.text().strip().rstrip("/")
            else "Можно вставить адрес сервера или ссылку на проект/снимок."
        )
        self.http_warning.setVisible(valid_server.startswith("http://"))
        self.submit_button.setEnabled(self.future is None and bool(valid_server)
                                      and bool(self.login_edit.text().strip()) and bool(self.password_edit.text()))

    def submit(self):
        if self._closed or not self.submit_button.isEnabled() or self.future is not None:
            return
        self.server_url = self.validated_server_url()
        if not self.server_url:
            self.update_submit()
            return
        try:
            self.save_server_url(self.server_url)
        except OSError:
            self.status.setStyleSheet("color: #ed9292;")
            self.status.setText("Не удалось сохранить адрес сервера. Проверьте доступ к папке приложения.")
            return
        self.cancel.clear()
        self.future = self.executor.submit(
            start_connection, self.app_directory, self.server_url, self.mcp_url, self.token,
            self.login_edit.text(), self.password_edit.text(), self.cancel,
        )
        self.password_edit.clear()
        self.server_edit.setEnabled(False)
        self.login_edit.setEnabled(False)
        self.password_edit.setEnabled(False)
        self.submit_button.setEnabled(False)
        self.submit_button.setText("Подключение…")
        self.status.setStyleSheet("color: #9da5b0;")
        self.status.setText("Проверяю вход и запускаю локальное подключение…")
        self.progress.show()
        self.timer.start()

    def poll(self):
        if self.future is None or not self.future.done():
            return
        future, self.future = self.future, None
        if self._cleaning:
            self._cleaning = False
            try:
                future.result()
            except Exception:
                self.status.setText("Не удалось остановить подключение. Используйте служебное меню «Остановить Svacer MCP».")
                self.status.setStyleSheet("color: #ed9292;")
                self.progress.hide()
                self.timer.stop()
                self.cancel_button.setEnabled(True)
            else:
                super().reject()
            return
        try:
            process = future.result()
        except LoginCancelled:
            super().reject()
            return
        except Exception as exc:
            message = str(exc) if isinstance(exc, LoginError) else "Ошибка подключения. Проверьте сеть и повторите вход."
            self.status.setText(message)
            self.status.setStyleSheet("color: #ed9292;")
        else:
            if self.cancel.is_set():
                # Cancellation may arrive between worker success and this UI tick.
                self._cleaning = True
                self.future = self.executor.submit(stop_owned_process, process)
                return
            self.accept()
            return
        self.timer.stop()
        self.progress.hide()
        self.server_edit.setEnabled(True)
        self.login_edit.setEnabled(True)
        self.password_edit.setEnabled(True)
        self.cancel_button.setEnabled(True)
        self.submit_button.setText("Войти")
        self.password_edit.setFocus()
        self.update_submit()

    def reject(self):
        self.password_edit.clear()
        if self.future is not None:
            self.cancel.set()
            self.status.setText("Отменяю подключение…")
            self.cancel_button.setEnabled(False)
            return
        super().reject()

    def closeEvent(self, event):
        if self.future is not None:
            self.reject()
            event.ignore()
        else:
            super().closeEvent(event)

    def shutdown(self, *_args):
        if self._closed:
            return
        self._closed = True
        self.cancel.set()
        self.timer.stop()
        self.password_edit.clear()
        self.login_edit.clear()
        self.token = ""
        if self.future is not None and not self._cleaning:
            def discard(future):
                try:
                    process = future.result()
                except Exception:
                    return
                threading.Thread(target=stop_owned_process, args=(process,), daemon=False).start()
            self.future.add_done_callback(discard)
        self.executor.shutdown(wait=False, cancel_futures=not self._cleaning)
