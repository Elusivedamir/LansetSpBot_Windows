from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.campaign_schedule import to_db_time, utc_now
from services.multiaccount_scheduler import AccountCampaignDatabaseView
from storage.database import Database

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Fix 1: maintenance PID liveness must never terminate the probed process.
# ---------------------------------------------------------------------------


def test_maintenance_liveness_probe_never_kills_the_probed_pid(
    monkeypatch,
):
    if os.name != "nt":
        pytest.skip("Windows TerminateProcess semantics are the defect")

    from storage import db_settings

    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
        ]
    )
    try:
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and child.poll() is not None:
            time.sleep(0.05)
        assert child.poll() is None

        def fail_kill(pid, sig):
            raise AssertionError(
                "os.kill must not be used for liveness probing on Windows"
            )

        monkeypatch.setattr(db_settings.os, "kill", fail_kill)
        assert db_settings._maintenance_process_alive(child.pid) is True
        assert child.poll() is None, "liveness probe must not kill the process"
    finally:
        child.terminate()
        child.wait(timeout=10)


def test_maintenance_liveness_reports_dead_pid():
    from storage import db_settings

    child = subprocess.Popen([sys.executable, "-c", ""])
    child.wait(timeout=10)
    assert db_settings._maintenance_process_alive(child.pid) is False
    assert db_settings._maintenance_process_alive(0) is False
    assert db_settings._maintenance_process_alive(-5) is False
    assert db_settings._maintenance_process_alive(os.getpid()) is True


# ---------------------------------------------------------------------------
# Fix 2: the self-test must survive a pre-set LANSETSPBOT_DATA_DIR.
# ---------------------------------------------------------------------------


def test_self_test_survives_predefined_canonical_data_dir(tmp_path):
    foreign_root = tmp_path / "foreign-profile"
    foreign_root.mkdir()
    environment = os.environ.copy()
    environment["LANSETSPBOT_DATA_DIR"] = str(foreign_root)
    environment["QT_QPA_PLATFORM"] = "offscreen"

    completed = subprocess.run(
        [sys.executable, str(ROOT / "main.py"), "--self-test"],
        cwd=str(ROOT),
        env=environment,
        capture_output=True,
        text=True,
        timeout=180,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "MARLEN_SELF_TEST_OK" in completed.stdout
    assert not any(foreign_root.iterdir()), (
        "self-test must not write into a foreign profile root"
    )


# ---------------------------------------------------------------------------
# Fix 3: instruction screenshots must select the real Instructions page.
# ---------------------------------------------------------------------------

_SCREENSHOT_MENU_LABELS = {
    "01_account.png": "Аккаунт",
    "02_channels.png": "Каналы",
    "03_links.png": "Связки",
    "04_comments.png": "Комментирование",
    "05_instructions.png": "Инструкция",
}


def _menu_full_order() -> list[str]:
    source = (ROOT / "gui" / "main_window.py").read_text(encoding="utf-8")
    match = re.search(r"_menu_full = \[(.*?)\]", source, re.DOTALL)
    assert match is not None, "MainWindow no longer declares _menu_full"
    return re.findall(r'"([^"]+)"', match.group(1))


def _capture_pages() -> dict[str, int]:
    source = (
        ROOT / "tools" / "capture_instruction_screenshots.py"
    ).read_text(encoding="utf-8")
    entries = re.findall(r"\((\d+), \"([0-9_a-z]+\.png)\"\)", source)
    return {name: int(index) for index, name in entries}


def test_instruction_screenshot_indices_match_menu_order():
    menu = _menu_full_order()
    pages = _capture_pages()

    for filename, label in _SCREENSHOT_MENU_LABELS.items():
        assert filename in pages, f"{filename} missing from capture PAGES"
        assert label in menu, f"menu no longer contains {label!r}"
        assert pages[filename] == menu.index(label), (
            f"{filename} captures stack index {pages[filename]} but "
            f"{label!r} lives at index {menu.index(label)}"
        )


# ---------------------------------------------------------------------------
# Fix 4: the Windows dev lock must cover every win32 pytest dependency.
# ---------------------------------------------------------------------------


def test_dev_lock_pins_every_windows_runtime_dependency_of_its_packages():
    import importlib.metadata as metadata

    lock_text = (
        ROOT / "requirements-dev-windows-x64.lock"
    ).read_text(encoding="utf-8")
    pinned = set(re.findall(r"^([A-Za-z0-9_.-]+)==", lock_text, re.MULTILINE))
    normalized_pinned = {name.replace("_", "-").lower() for name in pinned}

    direct = ["pytest", "pytest-asyncio", "mypy", "coverage", "ruff"]
    for distribution in direct:
        installed = metadata.version(distribution)
        requires = metadata.requires(distribution) or []
        for requirement in requires:
            parsed = requirement.split(";")[0].strip()
            markers = requirement.split(";", 1)[1] if ";" in requirement else ""
            if "extra ==" in markers or "extra==" in markers:
                continue
            if "sys_platform" in markers and '"win32"' not in markers:
                continue
            if "python_version" in markers:
                continue
            name = re.split(r"[<>=!~\s\[]", parsed)[0]
            assert name.replace("_", "-").lower() in normalized_pinned, (
                f"{distribution} {installed} requires {name!r} on this "
                "platform but the Windows dev lock does not pin it"
            )


# ---------------------------------------------------------------------------
# Fix 5: join slots are queued per account, like comment slots.
# ---------------------------------------------------------------------------


def _register_account(database: Database, account_id: int) -> None:
    database.register_telegram_account(
        telegram_account_id=account_id,
        session_name=f"account_{account_id}",
        display_name=f"Account {account_id}",
        username=f"account_{account_id}",
        authorized=True,
    )


def _create_due_join_slot(
    database: Database, account_id: int, *, peer_id: int, now
) -> int:
    dialog_id = database.upsert_saved_dialog(
        {
            "peer_id": peer_id,
            "username": f"target_{peer_id}",
            "title": f"Target {peer_id}",
            "kind": "channel",
        },
        account_id=account_id,
    )
    database.set_saved_dialog_membership(dialog_id, account_id, "left")
    campaign = database.create_join_campaign(
        account_id,
        max_per_hour=40,
        start_at=now,
    )
    with database.get_connection() as connection:
        connection.execute(
            "UPDATE join_schedule SET scheduled_at=? WHERE campaign_id=?",
            (to_db_time(now - timedelta(seconds=1)), int(campaign["id"])),
        )
    return int(campaign["id"])


def test_join_slot_of_second_account_is_not_starved_by_first(tmp_path):
    database = Database(tmp_path / "join-multiaccount.db")
    now = utc_now()
    try:
        _register_account(database, 301)
        _register_account(database, 302)
        _create_due_join_slot(database, 301, peer_id=7001, now=now)
        _create_due_join_slot(database, 302, peer_id=7002, now=now)

        first = database.queue_due_join_slot(now=now)
        assert first is not None
        assert int(first["account_id"]) == 301

        second = database.queue_due_join_slot(now=now)
        assert second is not None, (
            "an active join slot of one account must not starve another account"
        )
        assert int(second["account_id"]) == 302

        assert database.queue_due_join_slot(now=now) is None
    finally:
        database.close_thread_connection()


def test_join_slot_of_same_account_is_still_serialized(tmp_path):
    database = Database(tmp_path / "join-single-account.db")
    now = utc_now()
    try:
        _register_account(database, 401)
        for peer_id in (7101, 7102):
            dialog_id = database.upsert_saved_dialog(
                {
                    "peer_id": peer_id,
                    "username": f"target_{peer_id}",
                    "title": f"Target {peer_id}",
                    "kind": "channel",
                },
                account_id=401,
            )
            database.set_saved_dialog_membership(dialog_id, 401, "left")
        campaign = database.create_join_campaign(
            401,
            max_per_hour=40,
            start_at=now,
        )
        with database.get_connection() as connection:
            connection.execute(
                "UPDATE join_schedule SET scheduled_at=? WHERE campaign_id=?",
                (to_db_time(now - timedelta(seconds=1)), int(campaign["id"])),
            )

        first = database.queue_due_join_slot(now=now)
        assert first is not None
        assert int(first["account_id"]) == 401
        assert database.queue_due_join_slot(now=now) is None
    finally:
        database.close_thread_connection()


# ---------------------------------------------------------------------------
# Fix 7: cancelling the paced request wrapper must cancel the inner task.
# ---------------------------------------------------------------------------


def test_cancelled_paced_request_does_not_leak_the_inner_task(monkeypatch):
    from telethon import TelegramClient

    from services.paced_telegram_client import PacedTelegramClient

    started = asyncio.Event()

    def fake_call(self, request, *, ordered, flood_sleep_threshold):
        async def operation():
            started.set()
            await asyncio.sleep(30)

        return operation()

    monkeypatch.setattr(TelegramClient, "__call__", fake_call)

    client = object.__new__(PacedTelegramClient)
    client._marlen_request_limiter = type(
        "Limiter", (), {"request_slot": staticmethod(lambda *a, **k: _null_slot())}
    )()
    client._marlen_request_safety_gate = None
    client._marlen_request_timeout = 5.0

    async def scenario() -> None:
        outer = asyncio.create_task(
            client._call_one(
                SimpleNamespace(), ordered=True, flood_sleep_threshold=None
            )
        )
        await asyncio.wait_for(started.wait(), timeout=5)
        outer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await outer
        await asyncio.sleep(0.1)
        strays = [
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task() and not task.done()
        ]
        assert strays == [], "inner MTProto task outlived the cancelled request"

    asyncio.run(scenario())


class _NullSlot:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _null_slot():
    return _NullSlot()


# ---------------------------------------------------------------------------
# Fix 8: activity buckets accept users seen inside the requested window.
# ---------------------------------------------------------------------------


def test_activity_filter_buckets_match_their_advertised_windows():
    from services.audience_parser import _activity_is_recent

    class UserLastWeek:
        pass

    class UserLastMonth:
        pass

    last_week = SimpleNamespace(status=UserLastWeek())
    last_month = SimpleNamespace(status=UserLastMonth())

    assert _activity_is_recent(last_week, 7) is True
    assert _activity_is_recent(last_week, 30) is True
    assert _activity_is_recent(last_week, 90) is True
    assert _activity_is_recent(last_week, 3) is False

    assert _activity_is_recent(last_month, 30) is True
    assert _activity_is_recent(last_month, 90) is True
    assert _activity_is_recent(last_month, 7) is False


def test_account_campaign_view_still_binds_join_slots_to_one_account(tmp_path):
    database = Database(tmp_path / "join-view.db")
    now = utc_now()
    try:
        _register_account(database, 501)
        _create_due_join_slot(database, 501, peer_id=7201, now=now)

        view = AccountCampaignDatabaseView(database, 501)
        queued = view.queue_due_join_slot(now=now)
        assert queued is not None
        assert int(queued["account_id"]) == 501
        assert view.queue_due_join_slot(now=now) is None
    finally:
        database.close_thread_connection()
