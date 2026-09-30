"""Read-only sanity check for the database question bank."""

import sqlite3
from pathlib import Path


def test_question_bank_has_nonempty_active_questions():
    database_path = Path(__file__).resolve().parents[1] / "data/bazi_chi_bot.sqlite3"
    assert database_path.is_file(), "Question database is missing"

    connection = sqlite3.connect(f"{database_path.as_uri()}?mode=ro", uri=True)
    try:
        counts = dict(
            connection.execute(
                "SELECT kind, COUNT(*) FROM questions WHERE active = 1 GROUP BY kind"
            ).fetchall()
        )
        empty_count = connection.execute(
            "SELECT COUNT(*) FROM questions WHERE text IS NULL OR trim(text) = ''"
        ).fetchone()[0]
    finally:
        connection.close()

    assert counts.get("truth", 0) > 0
    assert counts.get("dare", 0) > 0
    assert empty_count == 0
