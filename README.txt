LansetSpBot - Windows

Supported OS
  Windows 10 x64, Windows 11 x64

Python (source launch)
  Python 3.13 x64 or Python 3.14 x64
  Both are covered by requirements-runtime.lock; the release build proves it
  with tools/check_lock_coverage.py.

Run
  1_RUN_LANSETSPBOT_WINDOWS.bat
    Creates or reuses .venv-windows-x64, installs the locked dependencies and
    starts the interface. The first run takes a few minutes.
  2_RUN_LANSETSPBOT_DIRECT_PY314.bat
    Direct Python 3.14 launch for an environment that already exists.

Diagnose a failed start
  3_COLLECT_DIAGNOSTICS.cmd
    Writes lansetspbot-diagnostics.txt next to the program: environment,
    dependency versions, profile layout, whether the database is encrypted,
    whether key pages can be locked in memory, SHA256SUMS verification, the
    startup self-test with its full traceback and the redacted application log.
    Never collected: marlen.db, the sessions folder, .secrets.json and
    .master-key.dpapi. Send that one file when reporting a problem.

Install dependencies manually (optional)
  py -3.13-64 -m venv .venv-windows-x64
  .venv-windows-x64\Scripts\python.exe -m pip install --require-hashes -r requirements-runtime.lock
  .venv-windows-x64\Scripts\python.exe -m pip install -r requirements-openai.txt

Where your data lives
  %APPDATA%\Marlen
    marlen.db          SQLCipher-encrypted database, bound to this Windows account
    sessions\          the live Telegram session
    logs\marlen.log    application log
  The program keeps no backups. A copy of a session file is a second working
  key to the Telegram account, and a profile archive is one more thing to
  guard, so neither is created. Closing the program does not sign you out; the
  live session stays. "Заводской сброс" on the account page deletes the local
  profile permanently.

Build a Windows executable
  BUILD_WINDOWS_X64.cmd
    Runs the test suite under coverage, the critical-coverage gate, compileall,
    ruff, mypy, the startup self-test and the lock-coverage check before
    packaging. Any failure stops the build.

Maintenance tools
  tools\generate_manifest.py                rebuild SHA256SUMS.txt (--check verifies)
  tools\check_lock_coverage.py              verify the lock covers every supported Python
  tools\capture_instruction_screenshots.py  re-render the in-app instruction images
  tools\check_critical_coverage.py          per-module coverage minimums

Environment variables
  MARLEN_DATA_DIR
    Optional. Overrides the profile directory.
    Default: %APPDATA%\Marlen
  LANSETSPBOT_ALLOW_PLAINTEXT_TEST_DB
  LANSETSPBOT_ALLOW_TEST_MASTER_KEY
  LANSETSPBOT_TEST_MASTER_KEY_B64
    Test-suite only. Ignored outside a live pytest process and in frozen builds.

Tests
  .venv-windows-x64\Scripts\python.exe -m pytest -q
