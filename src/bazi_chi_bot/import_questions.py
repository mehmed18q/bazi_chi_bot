"""Validate and import a reviewed question catalog, preserving IDs and history."""

import argparse
import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .config import Settings
from .db import Database
from .models import FinalChoice
from .rules import _clean_final_message


def load_catalog(path: Path) -> list[tuple[str, str]]:
    entries = json.loads(path.read_text(encoding="utf-8"))["questions"]
    questions = [
        (FinalChoice(item["kind"]).value, _clean_final_message(item["text"])) for item in entries
    ]
    if not questions or len(set(questions)) != len(questions):
        raise ValueError("Catalog must be nonempty and contain no duplicate questions")
    return questions


async def import_catalog(database: Database, questions: list[tuple[str, str]]) -> int:
    await database.initialize()
    inserted = 0
    async with database.transaction() as connection:
        for kind, text in questions:
            cursor = await connection.execute(
                "INSERT INTO questions (kind, text, active) VALUES (?, ?, 1) "
                "ON CONFLICT(kind, text) DO NOTHING",
                (kind, text),
            )
            inserted += cursor.rowcount
            # This catalog is explicitly approved for the shared bank. Reuse any
            # matching manual-question ID so its non-repetition history survives.
            await connection.execute(
                "UPDATE questions SET active = 1 WHERE kind = ? AND text = ?",
                (kind, text),
            )
    return inserted


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("catalog", type=Path)
    parser.add_argument("--database", type=Path)
    args = parser.parse_args()
    questions = load_catalog(args.catalog)
    path = args.database if args.database is not None else Settings().database_path
    if path.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup_path = path.with_name(f"{path.name}.before-questions-{stamp}.bak")
        # SQLite backup includes committed WAL data, unlike copying the main file.
        source = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        target = sqlite3.connect(backup_path)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        print(f"Backup: {backup_path}")
    inserted = asyncio.run(import_catalog(Database(path), questions))
    print(f"Database: {path}; catalog: {len(questions)}; inserted: {inserted}")


if __name__ == "__main__":
    main()
