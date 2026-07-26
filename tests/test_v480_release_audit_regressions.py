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
    assert 'EncryptedSQLiteSession(self.session_dir / "main")' in worker


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
