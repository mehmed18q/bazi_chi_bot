"""Persistence for mandatory sponsor channels and bots."""

from __future__ import annotations

import time
from dataclasses import dataclass

from ..db import Database


@dataclass(frozen=True, slots=True)
class Sponsor:
    id: int
    kind: str
    title: str
    chat_id: str | None
    username: str | None
    url: str
    active: bool


class SponsorRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def _map(row) -> Sponsor:
        return Sponsor(int(row["id"]), row["kind"], row["title"], row["chat_id"], row["username"], row["url"], bool(row["active"]))

    async def active(self) -> list[Sponsor]:
        async with self.database.connect() as db:
            rows = await (await db.execute("SELECT * FROM sponsors WHERE active = 1 ORDER BY id")).fetchall()
        return [self._map(row) for row in rows]

    async def all(self) -> list[Sponsor]:
        async with self.database.connect() as db:
            rows = await (await db.execute("SELECT * FROM sponsors ORDER BY id DESC")).fetchall()
        return [self._map(row) for row in rows]

    async def add(self, kind: str, title: str, url: str, *, chat_id: str | None = None, username: str | None = None) -> int:
        if kind not in {"channel", "bot"}:
            raise ValueError("kind must be channel or bot")
        async with self.database.connect() as db:
            cursor = await db.execute(
                "INSERT INTO sponsors(kind,title,chat_id,username,url,created_at) VALUES(?,?,?,?,?,?)",
                (kind, title.strip(), chat_id, username.lstrip("@") if username else None, url.strip(), int(time.time())),
            )
            await db.commit()
            return int(cursor.lastrowid)

    async def set_active(self, sponsor_id: int, active: bool) -> None:
        async with self.database.connect() as db:
            await db.execute("UPDATE sponsors SET active = ? WHERE id = ?", (int(active), sponsor_id))
            await db.commit()

    async def delete(self, sponsor_id: int) -> None:
        async with self.database.connect() as db:
            await db.execute("DELETE FROM sponsors WHERE id = ?", (sponsor_id,))
            await db.commit()
