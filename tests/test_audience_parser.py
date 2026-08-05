from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from services.audience_parser import (
    build_audience_export_filename,
    classify_audience_user,
    validate_audience_task_payload,
)


def test_audience_filter_skips_deleted_bots_and_missing_usernames():
    assert classify_audience_user(SimpleNamespace(deleted=True)) == ("deleted", None)
    assert classify_audience_user(SimpleNamespace(deleted=False, bot=True)) == ("bot", None)
    assert classify_audience_user(
        SimpleNamespace(deleted=False, bot=False, username=None)
    ) == ("missing_username", None)
    assert classify_audience_user(
        SimpleNamespace(deleted=False, bot=False, username="ExampleUser")
    ) == ("accepted", "@ExampleUser")


def test_audience_payload_accepts_exactly_one_group_source(tmp_path):
    path = tmp_path / "audience.txt"
    selected = validate_audience_task_payload(
        {
            "source": {
                "peer_id": -100123,
                "peer_type": "channel",
                "access_hash": "456",
                "title": "Group",
            },
            "output_path": str(path),
        }
    )
    assert selected["source"]["access_hash"] == 456

    linked = validate_audience_task_payload(
        {"source": {"link": "https://t.me/example"}, "output_path": str(path)}
    )
    assert linked["source"] == {"link": "https://t.me/example"}

    with pytest.raises(ValueError):
        validate_audience_task_payload(
            {
                "source": {"link": "@example", "peer_id": -100123},
                "output_path": str(path),
            }
        )


def test_audience_export_filename_is_windows_safe():
    name = build_audience_export_filename(
        'Группа: продажи / Москва?', when=datetime(2026, 8, 5, 9, 2)
    )
    assert name == "audience_Группа_продажи_Москва_2026-08-05_0902.txt"
