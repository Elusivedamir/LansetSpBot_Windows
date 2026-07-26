from __future__ import annotations

import pytest

from storage.db_common import _telegram_id


def test_telegram_id_scalar_zero_stays_zero():
    assert _telegram_id(0) == 0


def test_telegram_id_rejects_unsupported_object():
    class Unsupported:
        pass

    with pytest.raises(TypeError, match="Unsupported non-serializable payload type"):
        _telegram_id(Unsupported())
