from __future__ import annotations

import argparse
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
VERSION_NAMESPACE: dict[str, object] = {}
exec(
    (PROJECT_ROOT / "core" / "version.py").read_text(encoding="utf-8"),
    VERSION_NAMESPACE,
)
APP_NAME = str(VERSION_NAMESPACE["APP_NAME"])
__version__ = str(VERSION_NAMESPACE["__version__"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).with_name("windows_version_info.txt"),
    )
    arguments = parser.parse_args()
    parts = [int(part) for part in __version__.split(".")]
    while len(parts) < 4:
        parts.append(0)
    version_tuple = tuple(parts[:4])
    output = arguments.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={version_tuple},
    prodvers={version_tuple},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '040904B0',
        [
          StringStruct('CompanyName', 'LansetSpBot'),
          StringStruct('FileDescription', '{APP_NAME}'),
          StringStruct('FileVersion', '{__version__}'),
          StringStruct('InternalName', 'LansetSpBot'),
          StringStruct('OriginalFilename', '{APP_NAME}.exe'),
          StringStruct('ProductName', '{APP_NAME}'),
          StringStruct('ProductVersion', '{__version__}')
        ]
      )
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""",
        encoding="utf-8",
    )
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
