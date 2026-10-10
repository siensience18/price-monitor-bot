from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL,
    url            TEXT    NOT NULL,
    title          TEXT,
    currency       TEXT,
    threshold      REAL    NOT NULL,
    last_price     REAL,
    notified_price REAL,
    error_count    INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT    NOT NULL,
    last_checked   TEXT
);
CREATE INDEX IF NOT EXISTS idx_items_user ON items(user_id);
"""


@dataclass
class Item:
    id: int
    user_id: int
    url: str
    title: str | None
    currency: str | None
    threshold: float
    last_price: float | None
    notified_price: float | None
    error_count: int
    created_at: str
    last_checked: str | None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str):
        self.path = path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("База данных не подключена")
        return self._conn

    async def add_item(
        self,
        user_id: int,
        url: str,
        threshold: float,
        title: str | None,
        currency: str | None,
        price: float | None,
    ) -> int:
        cur = await self.conn.execute(
            "INSERT INTO items (user_id, url, title, currency, threshold, last_price, created_at, last_checked)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, url, title, currency, threshold, price, _now(), _now()),
        )
        await self.conn.commit()
        return cur.lastrowid

    async def get_item(self, item_id: int, user_id: int) -> Item | None:
        async with self.conn.execute(
            "SELECT * FROM items WHERE id = ? AND user_id = ?", (item_id, user_id)
        ) as cur:
            row = await cur.fetchone()
        return Item(**dict(row)) if row else None

    async def find_by_url(self, user_id: int, url: str) -> Item | None:
        async with self.conn.execute(
            "SELECT * FROM items WHERE user_id = ? AND url = ?", (user_id, url)
        ) as cur:
            row = await cur.fetchone()
        return Item(**dict(row)) if row else None

    async def list_items(self, user_id: int) -> list[Item]:
        async with self.conn.execute(
            "SELECT * FROM items WHERE user_id = ? ORDER BY id", (user_id,)
        ) as cur:
            rows = await cur.fetchall()
        return [Item(**dict(r)) for r in rows]

    async def all_items(self) -> list[Item]:
        async with self.conn.execute("SELECT * FROM items ORDER BY id") as cur:
            rows = await cur.fetchall()
        return [Item(**dict(r)) for r in rows]

    async def count_items(self, user_id: int) -> int:
        async with self.conn.execute(
            "SELECT COUNT(*) FROM items WHERE user_id = ?", (user_id,)
        ) as cur:
            (count,) = await cur.fetchone()
        return count

    async def update_threshold(self, item_id: int, threshold: float) -> None:
        await self.conn.execute(
            "UPDATE items SET threshold = ?, notified_price = NULL WHERE id = ?",
            (threshold, item_id),
        )
        await self.conn.commit()

    async def update_check_result(
        self,
        item_id: int,
        price: float,
        title: str | None,
        currency: str | None,
        notified_price: float | None,
    ) -> None:
        await self.conn.execute(
            "UPDATE items SET last_price = ?, title = COALESCE(?, title), currency = COALESCE(?, currency),"
            " notified_price = ?, error_count = 0, last_checked = ? WHERE id = ?",
            (price, title, currency, notified_price, _now(), item_id),
        )
        await self.conn.commit()

    async def register_error(self, item_id: int) -> int:
        await self.conn.execute(
            "UPDATE items SET error_count = error_count + 1, last_checked = ? WHERE id = ?",
            (_now(), item_id),
        )
        await self.conn.commit()
        async with self.conn.execute("SELECT error_count FROM items WHERE id = ?", (item_id,)) as cur:
            row = await cur.fetchone()
        return row[0] if row else 0

    async def delete_item(self, item_id: int, user_id: int) -> bool:
        cur = await self.conn.execute(
            "DELETE FROM items WHERE id = ? AND user_id = ?", (item_id, user_id)
        )
        await self.conn.commit()
        return cur.rowcount > 0
