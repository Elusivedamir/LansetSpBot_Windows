import csv
from pathlib import Path

import pytest

from services.api import ServiceAPI
from services.import_service import ImportService, ImportValidationError
from storage.database import Database


def write_csv(path: Path, fieldnames, rows):
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def test_service_api_rejects_unknown_task_type(tmp_path):
    api = ServiceAPI(Database(tmp_path / "app.db"))
    with pytest.raises(ValueError, match="Unsupported task type"):
        api.create_task("anything", {})


def test_service_api_rejects_empty_comment_payload(tmp_path):
    api = ServiceAPI(Database(tmp_path / "app.db"))
    with pytest.raises(ValueError, match="comment requires"):
        api.create_task("comment", {})


def test_import_validates_required_columns_before_write(tmp_path):
    db = Database(tmp_path / "app.db")
    path = tmp_path / "channels.csv"
    write_csv(path, ["title"], [{"title": "Missing ID"}])
    service = ImportService(db)
    with pytest.raises(ImportValidationError, match="channel_id"):
        service.import_file("channels", path)
    assert db.get_channels() == []


def test_import_is_atomic_on_database_failure(tmp_path):
    db = Database(tmp_path / "app.db")
    service = ImportService(db)
    rows = [
        {"channel_id": "100", "title": "ok"},
        {"channel_id": "100", "title": "updated"},
    ]
    path = tmp_path / "channels.csv"
    write_csv(path, ["channel_id", "title"], rows)
    assert service.import_file("channels", path) == 2
    channels = db.get_channels()
    assert len(channels) == 1
    assert channels[0]["title"] == "updated"


def test_import_task_payload_is_accepted(tmp_path):
    database = Database(tmp_path / "app.db")
    database.register_telegram_account(
        telegram_account_id=101,
        session_name="account_101",
        display_name="Import account",
        authorized=True,
    )
    database.select_telegram_account(101)
    api = ServiceAPI(database)
    created = api.create_task("import", {"kind": "channels", "path": "channels.csv"})
    assert created["type"] == "import"
    assert created["payload"]["account_id"] == 101


def test_service_api_rejects_comment_with_only_linked_chat_id(tmp_path):
    api = ServiceAPI(Database(tmp_path / "app.db"))
    with pytest.raises(ValueError, match="post_id and channel_id"):
        api.create_task(
            "comment",
            {"linked_chat_id": 777, "post_id": 55, "text": "hello"},
        )


def test_service_api_accepts_comment_with_channel_id(tmp_path):
    database = Database(tmp_path / "app.db")
    database.register_telegram_account(
        telegram_account_id=101,
        session_name="account_101",
        display_name="Test Account",
        authorized=True,
    )
    database.select_telegram_account(101)
    api = ServiceAPI(database)
    task = api.create_task(
        "comment",
        {"channel_id": 123, "linked_chat_id": 777, "post_id": 55, "text": "hello"},
    )
    assert task["type"] == "comment"
    assert task["payload"]["channel_id"] == 123


def test_legacy_task_service_is_not_part_of_active_services():
    import importlib.util

    assert importlib.util.find_spec("services.task_service") is None


def test_service_api_uses_one_configured_commenting_limit(tmp_path):
    database = Database(tmp_path / "batch.db")
    for channel_id in range(1, 6):
        database.insert_channel(
            {
                "channel_id": channel_id,
                "title": f"Channel {channel_id}",
                "linked_chat_id": 1000 + channel_id,
                "link_status": "linked",
            }
        )
    api = ServiceAPI(database, max_channels_per_run=3)
    assert api.get_max_channels_per_run() == 3
    assert len(api.get_commenting_channels()) == 3
