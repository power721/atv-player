from __future__ import annotations

import shiboken6
from PySide6.QtCore import QObject


class AsyncGuardMixin:
    def _init_async_guard(self) -> None:
        self._async_guard_active = True
        if isinstance(self, QObject):
            self.destroyed.connect(self._deactivate_async_guard)

    def _deactivate_async_guard(self, *_args) -> None:
        self._async_guard_active = False

    def _can_deliver_async_result(self) -> bool:
        return bool(getattr(self, "_async_guard_active", False)) and shiboken6.isValid(self)

    def _connect_async_signal(self, signal, handler) -> None:
        signal.connect(
            lambda *args, _handler=handler: _handler(*args) if self._can_deliver_async_result() else None
        )
