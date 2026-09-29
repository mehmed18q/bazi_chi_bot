import importlib.util
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openpyxl import Workbook, load_workbook


def load_script():
    path = Path(__file__).parents[1] / "scripts/send_opt_in_invitations.py"
    spec = importlib.util.spec_from_file_location("send_opt_in_invitations", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_message_uses_single_full_name_column_and_has_generic_fallback():
    script = load_script()
    named = script.create_message("  محمد   رضایی  ", "https://t.me/GameBot")
    anonymous = script.create_message(None, "https://t.me/GameBot")

    assert named.startswith("سلام محمد رضایی عزیز 👋")
    assert anonymous.startswith("سلام 👋")
    assert "گل یا پوچ" in named and "حدس کلمه" in named
    assert "بازی‌های بیشتری" in named and "پایان هر ماه جایزه" in named
    assert "اسپانسرشدن" in named and "https://t.me/GameBot" in named
    assert "پرداخت" not in named


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0912 123 4567", "+989121234567"),
        (9121234567, "+989121234567"),
        ("۰۰۹۸۹۱۲۱۲۳۴۵۶۷", "+989121234567"),
        ("+98-912-123-4567", "+989121234567"),
    ],
)
def test_phone_normalization(raw, expected):
    assert load_script().normalize_phone(raw, "+98") == expected


def test_all_contacts_have_consent_and_terminal_statuses_are_recognized():
    script = load_script()
    assert script.CONSENT is True
    assert script.status_is_terminal("SENT | 2026-09-26T12:00:00+03:30")
    assert script.status_is_terminal("لغو")
    assert not script.status_is_terminal("ERROR | retryable")


async def test_run_reads_all_sheets_and_persists_results_in_sqlite(tmp_path, monkeypatch):
    script = load_script()
    input_path = tmp_path / "all_phones.xlsx"
    workbook = Workbook()
    first = workbook.active
    first.title = "phones_1"
    first.append(["name", "phone", "source"])
    first.append(["محمد رضایی", "09121234567", "basalam_user"])
    first.append([None, "+989121234568", "vendor_user"])
    second = workbook.create_sheet("phones_2")
    second.append(["name", "phone", "source"])
    second.append(["مخاطب سوم", "09121234569", "contacts_vcf"])
    workbook.save(input_path)

    class FakeClient:
        instances = []

        def __init__(self, *args, **kwargs):
            self.sent = []
            self.instances.append(self)

        async def start(self, **kwargs):
            return self

        async def get_me(self):
            return SimpleNamespace(id=99)

        async def __call__(self, request):
            phone = request.contacts[0].phone
            return SimpleNamespace(users=[SimpleNamespace(id=int(phone[-4:]))])

        async def send_message(self, user, message, **kwargs):
            self.sent.append((user, message, kwargs))

        async def disconnect(self):
            return None

    from telethon import errors
    from telethon.tl.functions.contacts import ImportContactsRequest
    from telethon.tl.types import InputPhoneContact

    monkeypatch.setattr(script, "INPUT_WORKBOOK", input_path)
    monkeypatch.setattr(
        script,
        "load_dependencies",
        lambda: (
            load_workbook,
            FakeClient,
            errors,
            (ImportContactsRequest, InputPhoneContact),
        ),
    )
    monkeypatch.setattr(script.asyncio, "sleep", AsyncMock())
    state_db = tmp_path / "state" / "invitations.sqlite3"
    args = SimpleNamespace(
        api_id=12345,
        api_hash="hash",
        phone="+989121111111",
        session=tmp_path / "session/account",
        state_db=state_db,
        bot_link="https://t.me/GameBot",
        default_country_code="+98",
        delay=30,
        max_per_day=20,
        dry_run=False,
    )

    assert await script.run(args) == 0
    assert len(FakeClient.instances[0].sent) == 3
    assert FakeClient.instances[0].sent[0][1].startswith("سلام محمد رضایی عزیز 👋")
    assert FakeClient.instances[0].sent[1][1].startswith("سلام 👋")
    assert FakeClient.instances[0].sent[2][1].startswith("سلام مخاطب سوم عزیز 👋")

    with sqlite3.connect(state_db) as connection:
        rows = connection.execute(
            "SELECT phone, source, status FROM invitation_status ORDER BY phone"
        ).fetchall()
    assert len(rows) == 3
    assert {row[1] for row in rows} == {"basalam_user", "vendor_user", "contacts_vcf"}
    assert all(row[2].startswith("SENT | ") for row in rows)

    unchanged = load_workbook(input_path, read_only=True)
    assert [cell.value for cell in next(unchanged["phones_1"].iter_rows())] == [
        "name",
        "phone",
        "source",
    ]
    unchanged.close()

    first_client_count = len(FakeClient.instances)
    assert await script.run(args) == 0
    assert len(FakeClient.instances) == first_client_count
