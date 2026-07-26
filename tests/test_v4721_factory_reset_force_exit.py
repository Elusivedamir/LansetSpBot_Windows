from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import main as main_module
from gui.app import MarlenApp


def test_factory_reset_bypasses_shutdown_blocker_polling(monkeypatch):
    events: list[str] = []

    class SignalStub:
        def emit(self):
            events.append("quit-requested")

    fake = SimpleNamespace(
        _quitting=False,
        _factory_reset_pending=True,
        _show_shutdown_progress=lambda **_kwargs: events.append("progress"),
        centralWidget=lambda: SimpleNamespace(
            setEnabled=lambda value: events.append(f"central:{value}")
        ),
        quit_requested=SignalStub(),
        _keep_alive_timer=SimpleNamespace(stop=lambda: events.append("keepalive-stop")),
        adapter=SimpleNamespace(
            prepare_factory_reset=lambda: events.append("prepare-reset"),
            prepare_shutdown=lambda: events.append("prepare-shutdown"),
        ),
        _background_shutdown_blockers=lambda: (_ for _ in ()).throw(
            AssertionError("factory reset must not poll blockers")
        ),
        _complete_shutdown=lambda: events.append("complete"),
    )

    MarlenApp._begin_shutdown(fake, factory_reset=True)

    assert fake._quitting is True
    assert events == [
        "progress",
        "central:False",
        "quit-requested",
        "keepalive-stop",
        "prepare-reset",
        "complete",
    ]


def test_factory_reset_handoff_is_direct_and_does_not_submit_qthreadpool_job():
    scheduled = SimpleNamespace(
        scheduled=True, helper_pid=987, trace_path=Path("/tmp/x")
    )
    outcomes: list[object] = []
    fake = SimpleNamespace(
        _factory_reset_job=None,
        _factory_reset_executor=lambda: scheduled,
        _set_shutdown_progress_text=lambda _text: None,
        _shutdown_progress=None,
        _on_factory_reset_outcome=lambda outcome: outcomes.append(outcome),
    )

    MarlenApp._start_factory_reset_async(fake)

    assert outcomes == [(True, scheduled)]
    assert fake._factory_reset_job is None


def test_factory_reset_force_exit_flushes_logging_and_uses_immediate_process_exit(
    monkeypatch,
):
    events: list[object] = []
    monkeypatch.setattr(
        main_module.logging, "shutdown", lambda: events.append("logging")
    )
    monkeypatch.setattr(
        main_module.os, "_exit", lambda code: events.append(("exit", code))
    )

    main_module._terminate_after_factory_reset(999)

    assert events == ["logging", ("exit", 255)]


def test_main_finally_skips_blocking_container_shutdown_after_reset_handoff():
    source = Path(main_module.__file__).read_text(encoding="utf-8")
    finally_block = source.split("    finally:\n        reset_handoff = bool(", 1)[1]
    handoff_branch = finally_block.split("        if container is not None:", 1)[0]

    assert "_terminate_after_factory_reset(exit_code)" in handoff_branch
    assert "container.shutdown" not in handoff_branch
