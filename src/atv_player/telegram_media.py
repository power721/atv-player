from __future__ import annotations

import asyncio
import concurrent.futures
from collections.abc import AsyncIterator, Iterable, Iterator
from dataclasses import dataclass
import logging
from pathlib import Path
import queue
import re
import sqlite3
import threading
import time
from typing import Any

from atv_player.sqlite_utils import managed_connection


logger = logging.getLogger(__name__)

TELEGRAM_MEDIA_SCHEME = "telegram://media/"

_MAGNET_RE = re.compile(r"magnet:\?xt=urn:btih:[^\s<>'\"，。；]+", re.IGNORECASE)
_ED2K_RE = re.compile(r"ed2k://[^\s<>'\"，。；]+", re.IGNORECASE)
_DRIVE_PATTERNS = (
    re.compile(r"https?://(?:www\.)?aliyundrive\.com/s/[^\s<>'\"，。；]+", re.IGNORECASE),
    re.compile(r"https?://(?:www\.)?alipan\.com/s/[^\s<>'\"，。；]+", re.IGNORECASE),
    re.compile(r"https?://pan\.quark\.cn/s/[^\s<>'\"，。；]+", re.IGNORECASE),
    re.compile(r"https?://pan\.baidu\.com/s/[^\s<>'\"，。；]+", re.IGNORECASE),
    re.compile(r"https?://(?:www\.)?123(?:pan|684)\.com/s/[^\s<>'\"，。；]+", re.IGNORECASE),
    re.compile(r"https?://drive\.uc\.cn/s/[^\s<>'\"，。；]+", re.IGNORECASE),
)
_VIDEO_FILE_EXTENSIONS = {
    ".3gp",
    ".avi",
    ".divx",
    ".f4v",
    ".flv",
    ".iso",
    ".m2ts",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".mts",
    ".rm",
    ".rmvb",
    ".ts",
    ".vob",
    ".webm",
    ".wmv",
}


class TelegramMediaError(RuntimeError):
    pass


class TelethonUnavailableError(TelegramMediaError):
    pass


@dataclass(slots=True, frozen=True)
class TelegramUserInfo:
    id: int
    username: str = ""
    first_name: str = ""
    last_name: str = ""
    phone: str = ""

    @property
    def display_name(self) -> str:
        name = " ".join(part for part in (self.first_name, self.last_name) if part).strip()
        return name or self.username or str(self.id)


@dataclass(slots=True, frozen=True)
class TelegramQrLoginInfo:
    url: str


@dataclass(slots=True, frozen=True)
class TelegramIndexBatch:
    resource_count: int
    message_count: int
    oldest_msg_id: int = 0


@dataclass(slots=True, frozen=True)
class TelegramChatInfo:
    id: int
    title: str
    kind: str
    username: str = ""
    enabled: bool = False
    browse_enabled: bool = False
    last_indexed_msg_id: int = 0

    @property
    def search_enabled(self) -> bool:
        return self.enabled


@dataclass(slots=True, frozen=True)
class TelegramResource:
    key: str
    kind: str
    chat_id: int
    msg_id: int
    chat_title: str
    title: str
    text: str = ""
    url: str = ""
    file_name: str = ""
    size: int = 0
    mime_type: str = ""
    date: int = 0

    @property
    def media_uri(self) -> str:
        return f"{TELEGRAM_MEDIA_SCHEME}{self.chat_id}/{self.msg_id}"


def telegram_media_uri(chat_id: int | str, msg_id: int | str) -> str:
    return f"{TELEGRAM_MEDIA_SCHEME}{int(chat_id)}/{int(msg_id)}"


def parse_telegram_media_uri(value: str) -> tuple[int, int] | None:
    text = str(value or "").strip()
    if not text.startswith(TELEGRAM_MEDIA_SCHEME):
        return None
    payload = text.removeprefix(TELEGRAM_MEDIA_SCHEME)
    chat_id_text, separator, msg_id_text = payload.partition("/")
    if not separator:
        return None
    try:
        return int(chat_id_text), int(msg_id_text)
    except ValueError:
        return None


def extract_drive_links(text: str) -> list[str]:
    links: list[str] = []
    seen: set[str] = set()
    for pattern in _DRIVE_PATTERNS:
        for match in pattern.findall(text or ""):
            normalized = match.rstrip(").,，。；;")
            if normalized and normalized not in seen:
                links.append(normalized)
                seen.add(normalized)
    return links


def extract_magnet_links(text: str) -> list[str]:
    links: list[str] = []
    seen: set[str] = set()
    for match in _MAGNET_RE.findall(text or ""):
        normalized = match.rstrip(").,，。；;")
        if normalized and normalized not in seen:
            links.append(normalized)
            seen.add(normalized)
    return links


def extract_ed2k_links(text: str) -> list[str]:
    links: list[str] = []
    seen: set[str] = set()
    for match in _ED2K_RE.findall(text or ""):
        normalized = match.rstrip(").,，。；;")
        if normalized and normalized not in seen:
            links.append(normalized)
            seen.add(normalized)
    return links


class TelegramMediaRepository:
    def __init__(self, db_path: Path | str) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self):
        return managed_connection(self._db_path)

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS telegram_chats (
                    chat_id INTEGER PRIMARY KEY,
                    title TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    username TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 0,
                    browse_enabled INTEGER NOT NULL DEFAULT 0,
                    last_indexed_msg_id INTEGER NOT NULL DEFAULT 0,
                    updated_at INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(telegram_chats)").fetchall()
            }
            if "browse_enabled" not in columns:
                conn.execute(
                    "ALTER TABLE telegram_chats ADD COLUMN browse_enabled INTEGER NOT NULL DEFAULT 0"
                )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS telegram_resources (
                    key TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    chat_id INTEGER NOT NULL,
                    msg_id INTEGER NOT NULL,
                    chat_title TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL DEFAULT '',
                    text TEXT NOT NULL DEFAULT '',
                    url TEXT NOT NULL DEFAULT '',
                    file_name TEXT NOT NULL DEFAULT '',
                    size INTEGER NOT NULL DEFAULT 0,
                    mime_type TEXT NOT NULL DEFAULT '',
                    date INTEGER NOT NULL DEFAULT 0,
                    indexed_at INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_telegram_resources_chat_msg "
                "ON telegram_resources(chat_id, msg_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_telegram_resources_kind "
                "ON telegram_resources(kind)"
            )
            try:
                conn.execute(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS telegram_resources_fts
                    USING fts5(key UNINDEXED, kind, chat_title, title, text, file_name, url)
                    """
                )
            except sqlite3.OperationalError:
                pass

    def upsert_chat(self, chat: TelegramChatInfo) -> None:
        now = int(time.time())
        with self._connect() as conn:
            existing = conn.execute(
                """
                SELECT enabled, browse_enabled, last_indexed_msg_id
                FROM telegram_chats
                WHERE chat_id = ?
                """,
                (chat.id,),
            ).fetchone()
            enabled = int(chat.enabled)
            browse_enabled = int(chat.browse_enabled)
            last_indexed_msg_id = int(chat.last_indexed_msg_id)
            if existing is not None:
                enabled = int(existing[0])
                browse_enabled = int(existing[1])
                last_indexed_msg_id = max(last_indexed_msg_id, int(existing[2] or 0))
            conn.execute(
                """
                INSERT INTO telegram_chats (
                    chat_id, title, kind, username, enabled, browse_enabled,
                    last_indexed_msg_id, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    title = excluded.title,
                    kind = excluded.kind,
                    username = excluded.username,
                    enabled = excluded.enabled,
                    browse_enabled = excluded.browse_enabled,
                    last_indexed_msg_id = excluded.last_indexed_msg_id,
                    updated_at = excluded.updated_at
                """,
                (
                    chat.id,
                    chat.title,
                    chat.kind,
                    chat.username.strip(),
                    enabled,
                    browse_enabled,
                    last_indexed_msg_id,
                    now,
                ),
            )

    def set_chat_enabled(self, chat_id: int, enabled: bool) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE telegram_chats SET enabled = ?, updated_at = ? WHERE chat_id = ?",
                (int(enabled), int(time.time()), int(chat_id)),
            )

    def set_chat_browse_enabled(self, chat_id: int, enabled: bool) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE telegram_chats SET browse_enabled = ?, updated_at = ? WHERE chat_id = ?",
                (int(enabled), int(time.time()), int(chat_id)),
            )

    def update_last_indexed_msg_id(self, chat_id: int, msg_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE telegram_chats
                SET last_indexed_msg_id = MAX(last_indexed_msg_id, ?), updated_at = ?
                WHERE chat_id = ?
                """,
                (int(msg_id), int(time.time()), int(chat_id)),
            )

    def list_chats(self) -> list[TelegramChatInfo]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT chat_id, title, kind, username, enabled, browse_enabled,
                       last_indexed_msg_id
                FROM telegram_chats
                ORDER BY title COLLATE NOCASE
                """
            ).fetchall()
        return [
            TelegramChatInfo(
                id=int(row[0]),
                title=str(row[1] or ""),
                kind=str(row[2] or ""),
                username=str(row[3] or "").strip(),
                enabled=bool(row[4]),
                browse_enabled=bool(row[5]),
                last_indexed_msg_id=int(row[6] or 0),
            )
            for row in rows
        ]

    def enabled_chat_ids(self) -> list[int]:
        return self.search_enabled_chat_ids()

    def search_enabled_chat_ids(self) -> list[int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT chat_id FROM telegram_chats WHERE enabled = 1 ORDER BY title COLLATE NOCASE"
            ).fetchall()
        return [int(row[0]) for row in rows]

    def search_enabled_private_chat_ids(self) -> list[int]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT chat_id
                FROM telegram_chats
                WHERE enabled = 1 AND TRIM(username) = ''
                ORDER BY title COLLATE NOCASE
                """
            ).fetchall()
        return [int(row[0]) for row in rows]

    def browse_enabled_chat_ids(self) -> list[int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT chat_id FROM telegram_chats WHERE browse_enabled = 1 ORDER BY title COLLATE NOCASE"
            ).fetchall()
        return [int(row[0]) for row in rows]

    def upsert_resources(self, resources: Iterable[TelegramResource]) -> None:
        rows = list(resources)
        if not rows:
            return
        now = int(time.time())
        with self._connect() as conn:
            for resource in rows:
                conn.execute(
                    """
                    INSERT INTO telegram_resources (
                        key, kind, chat_id, msg_id, chat_title, title, text, url,
                        file_name, size, mime_type, date, indexed_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        kind = excluded.kind,
                        chat_id = excluded.chat_id,
                        msg_id = excluded.msg_id,
                        chat_title = excluded.chat_title,
                        title = excluded.title,
                        text = excluded.text,
                        url = excluded.url,
                        file_name = excluded.file_name,
                        size = excluded.size,
                        mime_type = excluded.mime_type,
                        date = excluded.date,
                        indexed_at = excluded.indexed_at
                    """,
                    (
                        resource.key,
                        resource.kind,
                        resource.chat_id,
                        resource.msg_id,
                        resource.chat_title,
                        resource.title,
                        resource.text,
                        resource.url,
                        resource.file_name,
                        resource.size,
                        resource.mime_type,
                        resource.date,
                        now,
                    ),
                )
                try:
                    conn.execute("DELETE FROM telegram_resources_fts WHERE key = ?", (resource.key,))
                    conn.execute(
                        """
                        INSERT INTO telegram_resources_fts (
                            key, kind, chat_title, title, text, file_name, url
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            resource.key,
                            resource.kind,
                            resource.chat_title,
                            resource.title,
                            resource.text,
                            resource.file_name,
                            resource.url,
                        ),
                    )
                except sqlite3.OperationalError:
                    pass

    def get_media(self, chat_id: int, msg_id: int) -> TelegramResource | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT key, kind, chat_id, msg_id, chat_title, title, text, url,
                       file_name, size, mime_type, date
                FROM telegram_resources
                WHERE chat_id = ? AND msg_id = ? AND kind = 'media'
                LIMIT 1
                """,
                (int(chat_id), int(msg_id)),
            ).fetchone()
        return _resource_from_row(row) if row is not None else None

    def search(
        self,
        keyword: str,
        *,
        limit: int = 30,
        offset: int = 0,
        chat_ids: Iterable[int] | None = None,
        kinds: Iterable[str] | None = None,
    ) -> list[TelegramResource]:
        query = str(keyword or "").strip()
        if not query:
            return self.recent(limit=limit, offset=offset, chat_ids=chat_ids, kinds=kinds)
        like = f"%{query}%"
        extra_clause, extra_params = _resource_query_filters(chat_ids=chat_ids, kinds=kinds)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT key, kind, chat_id, msg_id, chat_title, title, text, url,
                       file_name, size, mime_type, date
                FROM telegram_resources
                WHERE (
                    title LIKE ?
                    OR text LIKE ?
                    OR file_name LIKE ?
                    OR url LIKE ?
                    OR chat_title LIKE ?
                )
                {extra_clause}
                ORDER BY
                    CASE kind WHEN 'media' THEN 0 WHEN 'drive' THEN 1 WHEN 'magnet' THEN 2 ELSE 3 END,
                    date DESC,
                    msg_id DESC
                LIMIT ? OFFSET ?
                """,
                (like, like, like, like, like, *extra_params, int(limit), int(offset)),
            ).fetchall()
        return [_resource_from_row(row) for row in rows]

    def recent(
        self,
        *,
        limit: int = 30,
        offset: int = 0,
        chat_ids: Iterable[int] | None = None,
        kinds: Iterable[str] | None = None,
    ) -> list[TelegramResource]:
        where_clause, params = _resource_query_filters(chat_ids=chat_ids, kinds=kinds, prefix="WHERE")
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT key, kind, chat_id, msg_id, chat_title, title, text, url,
                       file_name, size, mime_type, date
                FROM telegram_resources
                {where_clause}
                ORDER BY
                    CASE kind WHEN 'media' THEN 0 WHEN 'drive' THEN 1 WHEN 'magnet' THEN 2 ELSE 3 END,
                    date DESC,
                    msg_id DESC
                LIMIT ? OFFSET ?
                """,
                (*params, int(limit), int(offset)),
            ).fetchall()
        return [_resource_from_row(row) for row in rows]


def _resource_query_filters(
    *,
    chat_ids: Iterable[int] | None = None,
    kinds: Iterable[str] | None = None,
    prefix: str = "AND",
) -> tuple[str, list[object]]:
    clauses: list[str] = []
    params: list[object] = []
    if chat_ids is not None:
        normalized_chat_ids = [int(chat_id) for chat_id in chat_ids]
        if not normalized_chat_ids:
            return f"{prefix} 0", []
        placeholders = ", ".join("?" for _ in normalized_chat_ids)
        clauses.append(f"chat_id IN ({placeholders})")
        params.extend(normalized_chat_ids)
    if kinds is not None:
        normalized_kinds = [str(kind or "").strip() for kind in kinds if str(kind or "").strip()]
        if not normalized_kinds:
            return f"{prefix} 0", []
        placeholders = ", ".join("?" for _ in normalized_kinds)
        clauses.append(f"kind IN ({placeholders})")
        params.extend(normalized_kinds)
    if not clauses:
        return "", []
    return f"{prefix} " + " AND ".join(clauses), params


def _resource_from_row(row: tuple[Any, ...]) -> TelegramResource:
    return TelegramResource(
        key=str(row[0]),
        kind=str(row[1]),
        chat_id=int(row[2]),
        msg_id=int(row[3]),
        chat_title=str(row[4] or ""),
        title=str(row[5] or ""),
        text=str(row[6] or ""),
        url=str(row[7] or ""),
        file_name=str(row[8] or ""),
        size=int(row[9] or 0),
        mime_type=str(row[10] or ""),
        date=int(row[11] or 0),
    )


class TelegramMediaService:
    def __init__(
        self,
        *,
        repository: TelegramMediaRepository,
        api_id: int = 0,
        api_hash: str = "",
        session_path: Path | str | None = None,
        client_factory: Any = None,
        request_size: int = 512 * 1024,
    ) -> None:
        self.repository = repository
        self.api_id = int(api_id or 0)
        self.api_hash = str(api_hash or "").strip()
        self.session_path = Path(session_path) if session_path is not None else None
        self._client_factory = client_factory
        self._request_size = max(64 * 1024, int(request_size))
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._client: Any = None
        self._client_lock = threading.Lock()
        self._qr_login: Any = None
        self._phone_code_hash_by_phone: dict[str, str] = {}

    @property
    def configured(self) -> bool:
        return self._client_factory is not None or (self.api_id > 0 and bool(self.api_hash))

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is not None:
            return self._loop
        loop = asyncio.new_event_loop()

        def run() -> None:
            asyncio.set_event_loop(loop)
            loop.run_forever()

        thread = threading.Thread(target=run, daemon=True, name="atv-telegram-media")
        thread.start()
        self._loop = loop
        self._thread = thread
        return loop

    def _run(self, coro):
        loop = self._ensure_loop()
        return asyncio.run_coroutine_threadsafe(coro, loop).result()

    def configure(self, *, api_id: int, api_hash: str, session_path: Path | str | None = None) -> None:
        self.api_id = int(api_id or 0)
        self.api_hash = str(api_hash or "").strip()
        if session_path is not None:
            self.session_path = Path(session_path)
        self._qr_login = None
        self._phone_code_hash_by_phone.clear()
        if self._client is not None:
            async def disconnect() -> None:
                client = self._client
                self._client = None
                disconnect_method = getattr(client, "disconnect", None)
                if callable(disconnect_method):
                    result = disconnect_method()
                    if hasattr(result, "__await__"):
                        await result

            self._run(disconnect())

    def close(self) -> None:
        loop = self._loop
        if loop is None:
            self._client = None
            self._qr_login = None
            self._phone_code_hash_by_phone.clear()
            return

        async def shutdown() -> None:
            client = self._client
            self._client = None
            self._qr_login = None
            self._phone_code_hash_by_phone.clear()
            if client is None:
                return
            disconnect_method = getattr(client, "disconnect", None)
            if callable(disconnect_method):
                result = disconnect_method()
                if hasattr(result, "__await__"):
                    await result

        future = asyncio.run_coroutine_threadsafe(shutdown(), loop)
        try:
            future.result(timeout=5.0)
        except Exception:
            logger.debug("Telegram media service shutdown did not finish cleanly", exc_info=True)
        loop.call_soon_threadsafe(loop.stop)
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
        if not loop.is_running():
            loop.close()
        self._loop = None
        self._thread = None

    async def _ensure_client(self):
        if self._client is not None:
            return self._client
        with self._client_lock:
            if self._client is not None:
                return self._client
            factory = self._client_factory
            if factory is None:
                try:
                    from telethon import TelegramClient
                except ImportError as exc:
                    raise TelethonUnavailableError("Telethon is not installed") from exc
                if not self.configured or self.session_path is None:
                    raise TelegramMediaError("Telegram api_id/api_hash/session_path is not configured")
                factory = TelegramClient
            client = factory(str(self.session_path or "atv-player-telegram"), self.api_id, self.api_hash)
            connect = getattr(client, "connect", None)
            if callable(connect):
                result = connect()
                if hasattr(result, "__await__"):
                    await result
            self._client = client
            return client

    def send_login_code(self, phone: str) -> Any:
        async def run():
            client = await self._ensure_client()
            result = await client.send_code_request(phone)
            phone_code_hash = str(getattr(result, "phone_code_hash", "") or "")
            if phone_code_hash:
                self._phone_code_hash_by_phone[str(phone).strip()] = phone_code_hash
            return result

        return self._run(run())

    def sign_in(self, *, phone: str, code: str, password: str = "") -> TelegramUserInfo:
        async def run():
            client = await self._ensure_client()
            phone_text = str(phone).strip()
            kwargs: dict[str, object] = {"phone": phone_text, "code": code}
            phone_code_hash = self._phone_code_hash_by_phone.get(phone_text, "")
            if phone_code_hash:
                kwargs["phone_code_hash"] = phone_code_hash
            try:
                await client.sign_in(**kwargs)
            except Exception as exc:
                if not password:
                    raise
                name = exc.__class__.__name__
                if name not in {"SessionPasswordNeededError", "PasswordHashInvalidError"}:
                    raise
                await client.sign_in(password=password)
            return _user_info_from_telethon(await client.get_me())

        return self._run(run())

    def start_qr_login(self) -> TelegramQrLoginInfo:
        async def run() -> TelegramQrLoginInfo:
            client = await self._ensure_client()
            qr_login = await client.qr_login()
            self._qr_login = qr_login
            return TelegramQrLoginInfo(url=str(getattr(qr_login, "url", "") or ""))

        return self._run(run())

    def complete_qr_login(self, *, password: str = "") -> TelegramUserInfo:
        async def run() -> TelegramUserInfo:
            client = await self._ensure_client()
            qr_login = self._qr_login
            if qr_login is None:
                raise TelegramMediaError("QR login has not been started")
            try:
                await qr_login.wait()
            except Exception as exc:
                if not password:
                    raise
                if exc.__class__.__name__ != "SessionPasswordNeededError":
                    raise
                await client.sign_in(password=password)
            self._qr_login = None
            return _user_info_from_telethon(await client.get_me())

        return self._run(run())

    def is_authorized(self) -> bool:
        async def run() -> bool:
            client = await self._ensure_client()
            checker = getattr(client, "is_user_authorized", None)
            if not callable(checker):
                return False
            result = checker()
            if hasattr(result, "__await__"):
                result = await result
            return bool(result)

        if not self.configured:
            return False
        return bool(self._run(run()))

    def get_user_info(self) -> TelegramUserInfo | None:
        async def run():
            client = await self._ensure_client()
            me = await client.get_me()
            return _user_info_from_telethon(me) if me is not None else None

        if not self.configured:
            return None
        return self._run(run())

    def logout(self) -> None:
        async def run() -> None:
            client = await self._ensure_client()
            logout = getattr(client, "log_out", None)
            if callable(logout):
                result = logout()
                if hasattr(result, "__await__"):
                    await result

        if self.configured:
            self._run(run())

    def refresh_chats(self) -> list[TelegramChatInfo]:
        async def run() -> list[TelegramChatInfo]:
            client = await self._ensure_client()
            async for dialog in client.iter_dialogs():
                if not (getattr(dialog, "is_group", False) or getattr(dialog, "is_channel", False)):
                    continue
                entity = getattr(dialog, "entity", None)
                chat = TelegramChatInfo(
                    id=int(getattr(dialog, "id")),
                    title=str(getattr(dialog, "name", "") or ""),
                    kind="channel" if getattr(dialog, "is_channel", False) else "group",
                    username=str(getattr(entity, "username", "") or "").strip(),
                    enabled=False,
                )
                self.repository.upsert_chat(chat)
            return self.repository.list_chats()

        return self._run(run())

    def set_chat_enabled(self, chat_id: int, enabled: bool) -> None:
        self.repository.set_chat_enabled(chat_id, enabled)

    def set_chat_browse_enabled(self, chat_id: int, enabled: bool) -> None:
        self.repository.set_chat_browse_enabled(chat_id, enabled)

    def list_chats(self) -> list[TelegramChatInfo]:
        return self.repository.list_chats()

    def sync_enabled_chats(self, *, limit_per_chat: int | None = 500) -> int:
        total = 0
        for chat in self.repository.list_chats():
            if not (chat.enabled or chat.browse_enabled):
                continue
            total += self.sync_chat(chat.id, limit=limit_per_chat)
        return total

    def sync_chat(self, chat_id: int, *, limit: int | None = 500) -> int:
        chat = next((item for item in self.repository.list_chats() if item.id == int(chat_id)), None)
        last_indexed_msg_id = chat.last_indexed_msg_id if chat is not None else 0
        chat_title = chat.title if chat is not None else str(chat_id)

        async def run() -> tuple[int, int]:
            client = await self._ensure_client()
            resources: list[TelegramResource] = []
            max_msg_id = last_indexed_msg_id
            async for msg in client.iter_messages(int(chat_id), min_id=last_indexed_msg_id, limit=limit):
                msg_id = int(getattr(msg, "id", 0) or 0)
                if msg_id <= 0:
                    continue
                max_msg_id = max(max_msg_id, msg_id)
                resources.extend(_resources_from_message(msg, chat_id=int(chat_id), chat_title=chat_title))
            self.repository.upsert_resources(resources)
            if max_msg_id > last_indexed_msg_id:
                self.repository.update_last_indexed_msg_id(int(chat_id), max_msg_id)
            return len(resources), max_msg_id

        count, _max_msg_id = self._run(run())
        return count

    def index_chat_recent_page(
        self,
        chat_id: int,
        *,
        limit: int | None = 30,
        offset_id: int = 0,
    ) -> TelegramIndexBatch:
        chat = next((item for item in self.repository.list_chats() if item.id == int(chat_id)), None)
        chat_title = chat.title if chat is not None else str(chat_id)

        async def run() -> TelegramIndexBatch:
            client = await self._ensure_client()
            resources: list[TelegramResource] = []
            message_count = 0
            oldest_msg_id = 0
            kwargs: dict[str, object] = {"limit": limit}
            if offset_id > 0:
                kwargs["offset_id"] = int(offset_id)
            async for msg in client.iter_messages(int(chat_id), **kwargs):
                msg_id = int(getattr(msg, "id", 0) or 0)
                if msg_id > 0:
                    message_count += 1
                    oldest_msg_id = msg_id if oldest_msg_id <= 0 else min(oldest_msg_id, msg_id)
                resources.extend(
                    _resources_from_message(
                        msg,
                        chat_id=int(chat_id),
                        chat_title=chat_title,
                    )
                )
            self.repository.upsert_resources(resources)
            return TelegramIndexBatch(
                resource_count=len(resources),
                message_count=message_count,
                oldest_msg_id=oldest_msg_id,
            )

        return self._run(run())

    def index_chat_recent(self, chat_id: int, *, limit: int | None = 30) -> int:
        return int(self.index_chat_recent_page(chat_id, limit=limit).resource_count)

    def search(self, keyword: str, *, limit: int = 30, offset: int = 0) -> list[TelegramResource]:
        return self.repository.search(
            keyword,
            limit=limit,
            offset=offset,
            chat_ids=self.repository.search_enabled_private_chat_ids(),
        )

    def search_chat(
        self,
        chat_id: int,
        keyword: str,
        *,
        limit: int = 30,
        offset: int = 0,
    ) -> list[TelegramResource]:
        return self.repository.search(
            keyword,
            limit=limit,
            offset=offset,
            chat_ids=[int(chat_id)],
        )

    def browse_recent(self, *, limit: int = 30, offset: int = 0) -> list[TelegramResource]:
        return self.repository.recent(
            limit=limit,
            offset=offset,
            chat_ids=self.repository.browse_enabled_chat_ids(),
        )

    def browse_chat(self, chat_id: int, *, limit: int = 30, offset: int = 0) -> list[TelegramResource]:
        return self.repository.recent(limit=limit, offset=offset, chat_ids=[int(chat_id)])

    def get_media(self, chat_id: int, msg_id: int) -> TelegramResource | None:
        return self.repository.get_media(chat_id, msg_id)

    def get_media_thumbnail_bytes(self, chat_id: int, msg_id: int) -> bytes:
        async def run() -> bytes:
            client = await self._ensure_client()
            msg = await client.get_messages(int(chat_id), ids=int(msg_id))
            if msg is None or not getattr(msg, "media", None):
                return b""
            payload = await client.download_media(msg, file=bytes, thumb=-1)
            if isinstance(payload, bytes):
                return payload
            if isinstance(payload, bytearray):
                return bytes(payload)
            return b""

        return self._run(run())

    def iter_media_bytes(
        self,
        chat_id: int,
        msg_id: int,
        *,
        offset: int = 0,
        limit: int | None = None,
    ) -> Iterator[bytes]:
        item_queue: queue.Queue[bytes | BaseException | None] = queue.Queue(maxsize=8)
        stopped = threading.Event()

        def put_item(item: bytes | BaseException | None) -> bool:
            while not stopped.is_set():
                try:
                    item_queue.put(item, timeout=0.1)
                    return True
                except queue.Full:
                    continue
            return False

        async def produce() -> None:
            try:
                async for chunk in self._iter_media_bytes_async(
                    int(chat_id),
                    int(msg_id),
                    offset=int(offset),
                    limit=limit,
                ):
                    if not await asyncio.to_thread(put_item, chunk):
                        return
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                await asyncio.to_thread(put_item, exc)
            finally:
                await asyncio.to_thread(put_item, None)

        loop = self._ensure_loop()
        future = asyncio.run_coroutine_threadsafe(produce(), loop)
        try:
            while True:
                item = item_queue.get()
                if item is None:
                    break
                if isinstance(item, BaseException):
                    raise item
                yield item
        finally:
            if not future.done():
                stopped.set()
                future.cancel()
                try:
                    future.result(timeout=1.0)
                except (concurrent.futures.CancelledError, concurrent.futures.TimeoutError):
                    pass
                except Exception:
                    logger.debug("Telegram media download cleanup failed", exc_info=True)
            else:
                stopped.set()

    async def _iter_media_bytes_async(
        self,
        chat_id: int,
        msg_id: int,
        *,
        offset: int = 0,
        limit: int | None = None,
    ) -> AsyncIterator[bytes]:
        client = await self._ensure_client()
        msg = await client.get_messages(chat_id, ids=msg_id)
        if msg is None or not getattr(msg, "media", None):
            raise TelegramMediaError("Telegram media message not found")
        remaining = None if limit is None else max(0, int(limit))
        request_size = self._request_size
        requested_offset = max(0, int(offset))
        aligned_offset = (
            requested_offset
            if requested_offset < request_size
            else requested_offset - (requested_offset % request_size)
        )
        discard = requested_offset - aligned_offset
        async for chunk in client.iter_download(
            getattr(msg, "media"),
            offset=aligned_offset,
            request_size=request_size,
        ):
            payload = bytes(chunk or b"")
            if not payload:
                continue
            if discard:
                if len(payload) <= discard:
                    discard -= len(payload)
                    continue
                payload = payload[discard:]
                discard = 0
            if remaining is not None:
                if remaining <= 0:
                    break
                payload = payload[:remaining]
                remaining -= len(payload)
            yield payload
            if remaining == 0:
                break


def _user_info_from_telethon(user: Any) -> TelegramUserInfo:
    return TelegramUserInfo(
        id=int(getattr(user, "id", 0) or 0),
        username=str(getattr(user, "username", "") or ""),
        first_name=str(getattr(user, "first_name", "") or ""),
        last_name=str(getattr(user, "last_name", "") or ""),
        phone=str(getattr(user, "phone", "") or ""),
    )


def _message_text(msg: Any) -> str:
    return str(getattr(msg, "message", None) or getattr(msg, "text", None) or "")


def _message_date(msg: Any) -> int:
    date = getattr(msg, "date", None)
    timestamp = getattr(date, "timestamp", None)
    if callable(timestamp):
        try:
            return int(timestamp())
        except (TypeError, ValueError, OSError):
            return 0
    return 0


def _is_video_file(file_name: str, mime_type: str) -> bool:
    normalized_mime_type = str(mime_type or "").strip().casefold()
    if normalized_mime_type.startswith("video/"):
        return True
    return Path(str(file_name or "").strip()).suffix.casefold() in _VIDEO_FILE_EXTENSIONS


def _resources_from_message(msg: Any, *, chat_id: int, chat_title: str) -> list[TelegramResource]:
    msg_id = int(getattr(msg, "id", 0) or 0)
    text = _message_text(msg)
    date = _message_date(msg)
    resources: list[TelegramResource] = []
    file = getattr(msg, "file", None)
    if file is not None:
        file_name = str(getattr(file, "name", "") or f"telegram-{chat_id}-{msg_id}")
        size = int(getattr(file, "size", 0) or 0)
        mime_type = str(getattr(file, "mime_type", "") or "")
        if _is_video_file(file_name, mime_type):
            resources.append(
                TelegramResource(
                    key=f"media:{chat_id}:{msg_id}",
                    kind="media",
                    chat_id=chat_id,
                    msg_id=msg_id,
                    chat_title=chat_title,
                    title=_message_title(text) or file_name,
                    text=text,
                    file_name=file_name,
                    size=size,
                    mime_type=mime_type,
                    date=date,
                )
            )
    for index, link in enumerate(extract_drive_links(text)):
        resources.append(
            TelegramResource(
                key=f"drive:{chat_id}:{msg_id}:{index}",
                kind="drive",
                chat_id=chat_id,
                msg_id=msg_id,
                chat_title=chat_title,
                title=_link_title(text, link),
                text=text,
                url=link,
                date=date,
            )
        )
    for index, link in enumerate(extract_magnet_links(text)):
        resources.append(
            TelegramResource(
                key=f"magnet:{chat_id}:{msg_id}:{index}",
                kind="magnet",
                chat_id=chat_id,
                msg_id=msg_id,
                chat_title=chat_title,
                title=_link_title(text, link),
                text=text,
                url=link,
                date=date,
            )
        )
    for index, link in enumerate(extract_ed2k_links(text)):
        resources.append(
            TelegramResource(
                key=f"ed2k:{chat_id}:{msg_id}:{index}",
                kind="ed2k",
                chat_id=chat_id,
                msg_id=msg_id,
                chat_title=chat_title,
                title=_link_title(text, link),
                text=text,
                url=link,
                date=date,
            )
        )
    return resources


def _message_title(text: str) -> str:
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    for paragraph in re.split(r"\n\s*\n+", normalized):
        cleaned = " ".join(line.strip() for line in paragraph.splitlines()).strip()
        if not cleaned:
            continue
        for link in (
            *extract_drive_links(cleaned),
            *extract_magnet_links(cleaned),
            *extract_ed2k_links(cleaned),
        ):
            cleaned = cleaned.replace(link, "").strip(" -:：\t")
        if cleaned:
            return cleaned[:120]
    return ""


def _link_title(text: str, link: str) -> str:
    for line in (text or "").splitlines():
        if link in line:
            cleaned = line.replace(link, "").strip(" -:：\t")
            if cleaned:
                return cleaned[:120]
    return link
