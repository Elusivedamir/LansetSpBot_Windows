"""Regression tests for confirmed defects found during the V4.8.0 release audit.

Every test here reproduces a defect that was observed on the delivered tree
before the fix, so a future regression fails the suite instead of reaching a
Windows release.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from core.crypto_vault import EncryptedBlobCodec, StaticMasterKeyProvider
from services.encrypted_telethon_session import EncryptedSQLiteSession

ROOT = Path(__file__).resolve().parents[1]


def _codec(seed: int) -> EncryptedBlobCodec:
    return EncryptedBlobCodec(StaticMasterKeyProvider(bytes([seed]) * 32))


# --------------------------------------------------------------------------
# CRITICAL: EncryptedSQLiteSession(Path) raised AttributeError inside Telethon.
# Both production call sites (TelegramService and AuthWorker) build the session
# base as ``session_dir / "main"``, i.e. a pathlib.Path.  Telethon's
# SQLiteSession stores the identifier as a plain str and calls ``.endswith`` on
# it, so every Telegram client construction failed before the fix.
# --------------------------------------------------------------------------


def test_encrypted_session_accepts_a_path_session_base(tmp_path: Path) -> None:
    session = EncryptedSQLiteSession(tmp_path / "main", codec=_codec(7))
    try:
        assert isinstance(session.filename, str)
        assert session.filename == str(tmp_path / "main.session")
        assert (tmp_path / "main.session").is_file()
    finally:
        session.close()


def test_encrypted_session_still_accepts_a_string_session_base(tmp_path: Path) -> None:
    session = EncryptedSQLiteSession(str(tmp_path / "main"), codec=_codec(7))
    try:
        assert session.filename == str(tmp_path / "main.session")
    finally:
        session.close()


def test_encrypted_session_without_an_id_stays_in_memory() -> None:
    session = EncryptedSQLiteSession(None, codec=_codec(7))
    try:
        assert session.filename == ":memory:"
    finally:
        session.close()


def test_production_call_sites_build_the_session_base_from_a_path() -> None:
    """Guard the exact construction shape the CRITICAL defect came from."""

    service = (ROOT / "services" / "telegram_service.py").read_text(encoding="utf-8")
    worker = (ROOT / "gui" / "auth_worker.py").read_text(encoding="utf-8")
    assert 'session_base = settings.session_dir / "main"' in service
    assert "telegram_session_base = session_base" in service
    assert "EncryptedSQLiteSession(telegram_session_base)" in service
    assert "return session_base(self.session_dir, self.session_name)" in worker
    assert "EncryptedSQLiteSession(self._session_base())" in worker


# --------------------------------------------------------------------------
# HIGH: the PyInstaller spec and main.py referenced
# build/assets/LansetSpBot-1024.png, which is not part of the delivered tree.
# PyInstaller aborts with "Unable to find ... when adding binary and data
# files", so no Windows executable could be produced.
# --------------------------------------------------------------------------


def _spec_data_sources() -> list[Path]:
    spec = (ROOT / "build" / "LansetSpBot.windows.spec").read_text(encoding="utf-8")
    tree = ast.parse(spec)
    sources: list[Path] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Tuple) or len(node.elts) != 2:
            continue
        joined = ast.unparse(node.elts[0])
        if "project_root" not in joined:
            continue
        parts = re.findall(r"'([^']+)'|\"([^\"]+)\"", joined)
        relative = [left or right for left, right in parts]
        if relative:
            sources.append(ROOT.joinpath(*relative))
    return sources


def test_every_pyinstaller_data_source_exists() -> None:
    sources = _spec_data_sources()
    assert sources, "spec data entries could not be parsed"
    missing = [str(path) for path in sources if not path.exists()]
    assert missing == [], f"PyInstaller data sources do not exist: {missing}"


def test_pyinstaller_icon_and_version_resources_exist() -> None:
    assert (ROOT / "build" / "assets" / "LansetSpBot.ico").is_file()
    assert (ROOT / "build" / "windows_version_info.txt").is_file()


def test_runtime_application_icon_resource_exists() -> None:
    """main.py must point at a bundled file, otherwise the icon silently vanishes."""

    source = (ROOT / "main.py").read_text(encoding="utf-8")
    match = re.search(r"_resource_path\(\s*\"([^\"]+)\"\s*\)", source)
    assert match is not None, "main.py no longer resolves an application icon"
    assert (ROOT / match.group(1)).is_file()


# --------------------------------------------------------------------------
# Windows-only delivery: no macOS bundle identity or macOS-only user guidance.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "relative",
    ["core/version.py", "gui/views/instructions_view.py", "main.py"],
)
def test_no_macos_bundle_identity_in_windows_sources(relative: str) -> None:
    text = (ROOT / relative).read_text(encoding="utf-8")
    assert ".app" not in text.replace("gui.app", "").replace(
        "from gui import app", ""
    ), f"{relative} still contains a macOS bundle reference"


def test_instructions_do_not_mention_macos_only_concepts() -> None:
    from gui.views.instructions_view import InstructionsView

    joined = "\n".join(step[0] + "\n" + step[2] for step in InstructionsView.STEPS)
    for forbidden in ("Dock", ".app", "macOS", "Finder"):
        assert forbidden not in joined, f"instructions still mention {forbidden}"


# --------------------------------------------------------------------------
# MEDIUM: a failed Database() construction left a zero-byte marlen.db behind.
# prepare_encrypted_database() refuses to initialize an existing empty file, so
# the leftover permanently blocked every later startup with
# "Existing marlen.db is empty" even though it contained no data at all.
# --------------------------------------------------------------------------


def test_failed_construction_does_not_leave_a_poisoned_empty_database(
    tmp_path: Path,
) -> None:
    from storage.database import Database, DatabaseError

    path = tmp_path / "marlen.db"

    with pytest.raises(DatabaseError, match="requires bootstrap"):
        Database(path, bootstrap=False)

    assert not path.exists(), "a failed construction left an empty database behind"

    database = Database(path)
    try:
        assert database.get_version() == Database.SCHEMA_VERSION
    finally:
        database.close_thread_connection()


def test_a_preexisting_empty_database_is_still_refused_and_preserved(
    tmp_path: Path,
) -> None:
    """The destructive-data guard must not be weakened by the cleanup above."""

    from storage.sqlcipher_driver import SQLCipherError
    from storage.database import Database

    path = tmp_path / "marlen.db"
    path.touch()

    with pytest.raises(SQLCipherError, match="empty"):
        Database(path)
    assert path.exists(), "a pre-existing file must never be deleted automatically"


def test_every_instruction_step_has_an_existing_image() -> None:
    from gui.views.instructions_view import InstructionsView

    for _title, image, _body in InstructionsView.STEPS:
        assert (ROOT / "gui" / "assets" / "instructions" / image).is_file()


# --------------------------------------------------------------------------
# MEDIUM: an enabled quiet-hours window with quiet_start == quiet_end has no
# active period at all. Every dispatch was deferred by another 24 hours
# forever and the user only saw a moving "отложено до ..." time, so a campaign
# stalled permanently without any explanation.
# --------------------------------------------------------------------------


def _schedule_api(tmp_path: Path):
    from core.secret_store import SecretStore
    from services.api import ServiceAPI
    from storage.database import Database

    database = Database(tmp_path / "schedule.db")
    api = ServiceAPI(database, secret_store=SecretStore(tmp_path / ".secrets.json"))
    api._campaign_timer.stop()  # noqa: SLF001 - deterministic unit under test
    return api, database


def test_identical_quiet_hours_are_rejected_while_the_schedule_is_enabled(
    tmp_path: Path,
) -> None:
    api, database = _schedule_api(tmp_path)
    try:
        with pytest.raises(ValueError, match="совпадают"):
            api.save_settings(
                {
                    "automation.schedule_enabled": "1",
                    "automation.quiet_start": "09:00",
                    "automation.quiet_end": "09:00",
                }
            )
    finally:
        api.prepare_shutdown()
        database.close_thread_connection()


def test_identical_quiet_hours_are_rejected_against_already_stored_values(
    tmp_path: Path,
) -> None:
    """Changing only one field must not be able to close the window either."""

    api, database = _schedule_api(tmp_path)
    try:
        api.save_settings(
            {
                "automation.schedule_enabled": "1",
                "automation.quiet_start": "22:00",
                "automation.quiet_end": "07:00",
            }
        )
        with pytest.raises(ValueError, match="совпадают"):
            api.save_settings({"automation.quiet_end": "22:00"})
    finally:
        api.prepare_shutdown()
        database.close_thread_connection()


def test_identical_quiet_hours_are_allowed_while_the_schedule_is_disabled(
    tmp_path: Path,
) -> None:
    """A disabled schedule never defers anything, so the values are harmless."""

    api, database = _schedule_api(tmp_path)
    try:
        api.save_settings(
            {
                "automation.schedule_enabled": "0",
                "automation.quiet_start": "09:00",
                "automation.quiet_end": "09:00",
            }
        )
        assert database.get_setting("automation.quiet_start") == "09:00"
    finally:
        api.prepare_shutdown()
        database.close_thread_connection()


def test_a_normal_overnight_window_is_still_accepted(tmp_path: Path) -> None:
    api, database = _schedule_api(tmp_path)
    try:
        api.save_settings(
            {
                "automation.schedule_enabled": "1",
                "automation.quiet_start": "22:00",
                "automation.quiet_end": "07:00",
            }
        )
        assert database.get_setting("automation.quiet_end") == "07:00"
    finally:
        api.prepare_shutdown()
        database.close_thread_connection()


# --------------------------------------------------------------------------
# LOW: `assert` was used as a production invariant check in five modules.
# `python -O` strips assert statements, so a violated invariant degraded into
# `raise None` (TypeError) or an AttributeError far from the real cause.
# --------------------------------------------------------------------------


PRODUCTION_PACKAGES = ("core", "services", "storage", "workers", "gui")


def test_production_code_does_not_use_assert_for_runtime_checks() -> None:
    offenders: list[str] = []
    roots = [ROOT / name for name in PRODUCTION_PACKAGES] + [ROOT / "main.py"]
    for root in roots:
        files = [root] if root.is_file() else sorted(root.rglob("*.py"))
        for path in files:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Assert):
                    offenders.append(
                        f"{path.relative_to(ROOT)}:{node.lineno}"
                    )
    assert offenders == [], (
        "assert is stripped by python -O and must not guard production "
        f"invariants: {offenders}"
    )


# --------------------------------------------------------------------------
# HIGH (supply chain): the pinned cryptography 46.0.4 carried six known
# advisories, including a statically linked OpenSSL inside the published
# wheels that ships straight into the packaged Windows application.
# --------------------------------------------------------------------------


def _pinned_version(text: str, package: str) -> str:
    match = re.search(rf"(?mi)^{re.escape(package)}==([0-9][^\s\\]*)", text)
    assert match is not None, f"{package} is not pinned"
    return match.group(1)


def test_cryptography_is_pinned_above_the_known_advisories() -> None:
    """46.0.4 is vulnerable; 48.0.1 is the first release fixing all six."""

    from packaging.version import Version

    declared = _pinned_version(
        (ROOT / "requirements-runtime.in").read_text(encoding="utf-8"), "cryptography"
    )
    locked = _pinned_version(
        (ROOT / "requirements-runtime.lock").read_text(encoding="utf-8"), "cryptography"
    )
    assert declared == locked, "the .in file and the lock disagree on cryptography"
    assert Version(declared) >= Version("48.0.1"), (
        f"cryptography {declared} is affected by PYSEC-2026-35/36, PYSEC-2026-2141 "
        "and GHSA-537c-gmf6-5ccf (bundled OpenSSL)"
    )


def test_every_runtime_requirement_is_pinned_and_hash_locked() -> None:
    """requirements-runtime.in must describe exactly what the lock enforces."""

    lock = (ROOT / "requirements-runtime.lock").read_text(encoding="utf-8")
    declared = (ROOT / "requirements-runtime.in").read_text(encoding="utf-8")
    for line in declared.splitlines():
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        name = re.split(r"[=<>!~\[]", entry, maxsplit=1)[0].strip()
        assert re.search(rf"(?mi)^{re.escape(name)}==", lock), (
            f"{name} is declared in requirements-runtime.in but is missing from "
            "the hash-locked graph; either lock it or move it to its own file"
        )
    for block in re.findall(r"(?m)^[A-Za-z].*?==.*?(?=\n[A-Za-z]|\Z)", lock, re.S):
        head = block.splitlines()[0]
        assert "--hash=sha256:" in block, f"{head} has no pinned artifact hash"


def test_the_unhashed_openai_graph_is_declared_separately_and_flagged() -> None:
    """The one knowingly unpinned dependency must stay visible, not silent."""

    runtime_in = (ROOT / "requirements-runtime.in").read_text(encoding="utf-8")
    openai_txt = (ROOT / "requirements-openai.txt").read_text(encoding="utf-8")
    lock = (ROOT / "requirements-runtime.lock").read_text(encoding="utf-8")

    assert not re.search(r"(?mi)^openai==", runtime_in)
    assert not re.search(r"(?mi)^openai==", lock)
    assert re.search(r"(?mi)^openai==", openai_txt)
    # The trade-off must remain documented where a maintainer will see it.
    assert "NOT hash-locked" in openai_txt
