from __future__ import annotations

from pathlib import Path


def test_only_account_page_owns_telegram_onboarding_surface():
    warmup = Path("gui/views/warmup_view.py").read_text(encoding="utf-8")
    parser = Path("gui/views/audience_parser_view.py").read_text(encoding="utf-8")

    assert "from gui.views.account_view import AccountView" not in warmup
    assert "onboarding_only=True" not in warmup
    assert "begin_onboarding" not in warmup
    assert "API Hash" not in warmup
    assert "Telegram-код" not in warmup
    assert "2FA" not in warmup

    assert "AccountView(" not in parser
    assert "onboarding_only=True" not in parser
    assert "begin_onboarding" not in parser
    assert "self.account_selector = QComboBox()" in parser
