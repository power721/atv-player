from PySide6.QtCore import Signal
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


class LoginWindow(QWidget):
    login_succeeded = Signal()

    def __init__(self, controller) -> None:
        super().__init__()
        self._controller = controller
        self.setWindowTitle("alist-tvbox 登录")
        self.resize(720, 520)

        defaults = controller.load_defaults()
        self.base_url_edit = QLineEdit(defaults.base_url)
        self.username_edit = QLineEdit(defaults.username)
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

    def _on_login_clicked(self) -> None:
        try:
            self._controller.login(
                self.base_url_edit.text().strip(),
                self.username_edit.text().strip(),
                self.password_edit.text(),
            )
        except Exception as exc:
            QMessageBox.critical(self, "登录失败", str(exc))
            return
        self.login_succeeded.emit()

    def set_error_message(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.setVisible(bool(message))
