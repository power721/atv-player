from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFormLayout,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class LoginWindow(QWidget):
    login_succeeded = Signal()

    def __init__(self, controller) -> None:
        super().__init__()
        self._controller = controller
        self.setWindowTitle("alist-tvbox 登录")

        defaults = controller.load_defaults()
        self.base_url_edit = QLineEdit(defaults.base_url)
        self.username_edit = QLineEdit(defaults.username)
        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.login_button = QPushButton("登录")
        self.login_button.clicked.connect(self._on_login_clicked)

        form = QFormLayout()
        form.addRow("后端地址", self.base_url_edit)
        form.addRow("用户名", self.username_edit)
        form.addRow("密码", self.password_edit)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.login_button)

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
