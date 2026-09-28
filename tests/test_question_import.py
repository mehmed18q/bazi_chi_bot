from pathlib import Path

from bazi_chi_bot.import_questions import import_catalog, load_catalog


def test_expansion_catalog_has_exact_unique_kind_counts_and_no_base_overlap():
    content = Path(__file__).parents[1] / "content"
    base = load_catalog(content / "questions.fa.json")
    expansion = load_catalog(content / "questions.batch-20260926.fa.json")

    assert len(expansion) == len(set(expansion)) == 500
    assert sum(kind == "truth" for kind, _ in expansion) == 250
    assert sum(kind == "dare" for kind, _ in expansion) == 250
    assert set(base).isdisjoint(expansion)


async def test_catalog_import_is_repeatable_and_preserves_question_ids(database):
    catalog = load_catalog(Path(__file__).parents[1] / "content/questions.fa.json")
    assert sum(kind == "truth" for kind, _ in catalog) == 158
    assert sum(kind == "dare" for kind, _ in catalog) == 29
    async with database.transaction() as connection:
        await connection.execute(
            "INSERT INTO questions (kind, text, active) VALUES (?, ?, 0)", catalog[0]
        )
    assert await import_catalog(database, catalog) == 186
    assert await import_catalog(database, catalog) == 0
    async with database.connect() as connection:
        rows = await (await connection.execute("SELECT * FROM questions ORDER BY id")).fetchall()
    assert len(rows) == 187
    assert rows[0]["id"] == 1 and rows[0]["text"] == catalog[0][1]
    assert all(row["active"] == 1 for row in rows)
