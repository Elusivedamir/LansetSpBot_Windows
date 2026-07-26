# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

project_root = Path(SPECPATH).parent
version_namespace = {}
exec((project_root / "core" / "version.py").read_text(encoding="utf-8"), version_namespace)
app_name = version_namespace["APP_NAME"]

hiddenimports = (
    collect_submodules("telethon")
    + collect_submodules("sqlcipher3")
    + collect_submodules("openai")
    + collect_submodules("cryptography")
    + [
        "PySide6.QtCore",
        "PySide6.QtGui",
        "PySide6.QtWidgets",
        "PySide6.QtNetwork",
        "socks",
        "tzdata",
        "tzdata.zoneinfo",
    ]
)
try:
    import cryptg  # noqa: F401
except ImportError:
    pass
else:
    hiddenimports.append("cryptg")

analysis = Analysis(
    [str(project_root / "main.py")],
    pathex=[str(project_root)],
    binaries=[],
    datas=collect_data_files("tzdata") + collect_data_files("openai") + [
        (
            str(project_root / "gui" / "assets" / "instructions"),
            "gui/assets/instructions",
        ),
        # The sidebar/menu SVG icons and the window icon are loaded at runtime
        # from gui/assets. Without these entries the packaged application shows
        # an icon-less navigation rail and a default window icon.
        (
            str(project_root / "gui" / "assets" / "icons"),
            "gui/assets/icons",
        ),
        (
            str(project_root / "gui" / "assets" / "lansetspbot.png"),
            "gui/assets",
        ),
    ],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(analysis.pure)
exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name=app_name,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=str(project_root / "build" / "assets" / "LansetSpBot.ico"),
    version=str(project_root / "build" / "windows_version_info.txt"),
    uac_admin=False,
)
coll = COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name=app_name,
)
