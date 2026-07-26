from __future__ import annotations

from gui.views.instructions_view import InstructionsView


def test_v41_guide_describes_v40_runtime_behaviour() -> None:
    assert len(InstructionsView.STEPS) == 9
    joined = "\n".join(step[0] + "\n" + step[2] for step in InstructionsView.STEPS)
    assert "каждую секунду" in joined
    assert "не создаёт вторую кампанию" in joined
    assert "личные переписки" in joined
    assert "Выполнено" in joined and "Отправлено" in joined
    assert "Fake TLS EE" in joined
    assert "LansetSpBot.app" in joined
