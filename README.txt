LansetSpBot - Windows

Supported OS
  Windows 10 x64, Windows 11 x64

Python (source launch)
  Python 3.13 x64 or Python 3.14 x64

Install dependencies
  py -3.13-64 -m venv .venv-windows-x64
  .venv-windows-x64\Scripts\python.exe -m pip install --require-hashes -r requirements-runtime.lock
  .venv-windows-x64\Scripts\python.exe -m pip install -r requirements-openai.txt

Run
  1_RUN_LANSETSPBOT_WINDOWS.bat
    (creates/uses .venv-windows-x64 and starts the GUI)
  2_RUN_LANSETSPBOT_DIRECT_PY314.bat
    (direct Python 3.14 launch)

Build a Windows executable
  BUILD_WINDOWS_X64.cmd

Diagnose a failed start
  3_COLLECT_DIAGNOSTICS.cmd
    Writes lansetspbot-diagnostics.txt next to the program: environment,
    dependency versions, profile layout, SHA256SUMS verification, the output
    of the startup self-test and the redacted application log.
    Never collected: marlen.db, the sessions folder, .secrets.json and
    .master-key.dpapi. Send that one file when reporting a problem.

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
