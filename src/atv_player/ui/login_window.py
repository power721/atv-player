import threading

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import (
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
import shiboken6


class _LoginWindowSignals(QObject):
    defaults_loaded = Signal(object)
    login_succeeded = Signal()
    login_failed = Signal(str)


class LoginWindow(QWidget):
    login_succeeded = Signal()

    def __init__(self, controller) -> None:
        super().__init__()
        self._controller = controller
        self._login_request_id = 0
        self._signals = _LoginWindowSignals(self)
        self._signals.defaults_loaded.connect(self._handle_defaults_loaded)
        self._signals.login_succeeded.connect(self._handle_login_succeeded)
        self._signals.login_failed.connect(self._handle_login_failed)
        self.setWindowTitle("alist-tvbox 登录")
        self.resize(720, 520)

        self.base_url_edit = QLineEdit()
        self.username_edit = QLineEdit()
        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.error_label = QLabel("")
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        self.login_button = QPushButton("登录")
        self.login_button.clicked.connect(self._on_login_clicked)

        form = QFormLayout()
        form.addRow("后端地址", self.base_url_edit)
        form.addRow("用户名", self.username_edit)
        form.addRow("密码", self.password_edit)

        self.content_container = QWidget()
        self.content_container.setMinimumWidth(460)
        self.content_container.setMaximumWidth(520)
        self.content_container.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )

        content_layout = QVBoxLayout(self.content_container)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.addLayout(form)
        content_layout.addWidget(self.error_label)
        content_layout.addWidget(self.login_button)

        centered_row = QHBoxLayout()
        centered_row.addStretch(1)
        centered_row.addWidget(self.content_container, 100)
        centered_row.addStretch(1)

        layout = QVBoxLayout(self)
        layout.addStretch(1)
        layout.addLayout(centered_row)
        layout.addStretch(1)

        self._load_defaults()

    def _load_defaults(self) -> None:
        def run() -> None:
            defaults = self._controller.load_defaults()
            if not self._can_deliver_worker_result():
                return
            self._signals.defaults_loaded.emit(defaults)

        threading.Thread(target=run, daemon=True).start()

    def _on_login_clicked(self) -> None:
        self._login_request_id += 1
        request_id = self._login_request_id
        self.login_button.setEnabled(False)

        def run() -> None:
            try:
                self._controller.login(
                    self.base_url_edit.text().strip(),
                    self.username_edit.text().strip(),
                    self.password_edit.text(),
                )
            except Exception as exc:
                if not self._can_deliver_worker_result():
                    return
                if request_id != self._login_request_id:
                    return
                self._signals.login_failed.emit(str(exc))
                return
            if not self._can_deliver_worker_result():
                return
            if request_id != self._login_request_id:
                return
            self._signals.login_succeeded.emit()

        threading.Thread(target=run, daemon=True).start()

    def set_error_message(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.setVisible(bool(message))

    def _handle_defaults_loaded(self, defaults) -> None:
        if not self.base_url_edit.text():
            self.base_url_edit.setText(defaults.base_url)
        if not self.username_edit.text():
            self.username_edit.setText(defaults.username)

    def _handle_login_succeeded(self) -> None:
        self.login_button.setEnabled(True)
        self.login_succeeded.emit()

    def _handle_login_failed(self, message: str) -> None:
        self.login_button.setEnabled(True)
        QMessageBox.critical(self, "登录失败", message)

    def _can_deliver_worker_result(self) -> bool:
        return shiboken6.isValid(self)
