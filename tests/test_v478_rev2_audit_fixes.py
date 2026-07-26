from __future__ import annotations

import json

from storage.database import Database
from storage.db_common import json_dumps_safe


def test_daily_maintenance_closes_claim_transaction_before_prune(tmp_path):
    class ProbeDatabase(Database):
        def prune_old_data(self, **kwargs):
            with self.get_connection() as conn:
                assert conn.in_transaction is False
            return {"probe": 1}

    db = ProbeDatabase(tmp_path / "maintenance-boundary.db")

    assert db.run_daily_maintenance() == {"probe": 1}
    assert db.run_daily_maintenance() is None


def test_telegram_peer_id_preserves_zero_before_other_peer_fields():
    class Peer:
        user_id = 0
        chat_id = 123
        channel_id = 456

    assert json.loads(json_dumps_safe(Peer())) == 0


def test_duplicate_comment_delivery_reservation_stays_false(tmp_path):
    db = Database(tmp_path / "delivery-reservation.db")

    assert db.reserve_comment_delivery(10, 20, linked_chat_id=30, text="hello")
    assert not db.reserve_comment_delivery(10, 20, linked_chat_id=30, text="hello")
