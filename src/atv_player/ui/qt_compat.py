from __future__ import annotations

from PySide6.QtCore import QByteArray


def to_qbytearray(data: bytes) -> QByteArray:
    return QByteArray(data)


def qbytearray_to_bytes(data: QByteArray) -> bytes:
    return bytes(data.data())
