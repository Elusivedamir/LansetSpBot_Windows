from __future__ import annotations

import ast
from pathlib import Path


def test_target_audience_view_contract() -> None:
    root = Path(__file__).resolve().parents[1]
    view_path = root / "gui" / "views" / "target_audience_view.py"
    main_path = root / "gui" / "main_window.py"

    view_text = view_path.read_text(encoding="utf-8")
    main_text = main_path.read_text(encoding="utf-8")

    ast.parse(view_text)
    ast.parse(main_text)
    assert "https://t.me/TargetAudienceCommentBot" in view_text
    assert "class TargetAudienceView" in view_text
    assert "Режим поиска ЦА" in main_text
    assert "self.target_audience_view = TargetAudienceView()" in main_text
