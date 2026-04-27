from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
import sqlite3


@contextmanager
def managed_connection(db_path: Path | str) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(db_path)
    try:
        with connection:
            yield connection
    finally:
        connection.close()
