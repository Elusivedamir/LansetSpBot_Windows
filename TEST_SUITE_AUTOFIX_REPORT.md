# LansetSpBot Test Suite Mass Autofix

- Applied: **7**
- Already current: **10**
- Unmatched/ambiguous: **0**

## Applied

- QueueWorker: separate claim failure counter
- QueueWorker: deterministic claim failure budget
- QueueWorker: fail closed after fifth claim error
- QueueWorker exact safety budget 5 [tests/test_v477_current_fixes.py]
- Mass Windows skip for 1 POSIX test(s) [tests/test_encrypted_storage_hardening.py]
- Mass Windows skip for 1 POSIX test(s) [tests/test_v4721_security_privacy_performance_audit.py]
- Mass source-contract normalization [tests/test_v511_windows_pytest_diagnostics.py]

## Already current

- ALREADY: QueueWorker exact safety budget 5 [tests/test_v477_current_fixes.py]
- ALREADY: Current insert_channel selected-account API [tests/test_v475_hardening.py]
- ALREADY: Current account-scoped secret migration [tests/test_v478_final_audit.py]
- ALREADY: Avoid redundant standalone-group classification write [tests/test_v478_group_commenting.py]
- ALREADY: Current auth session construction [tests/test_v480_release_audit_regressions.py]
- ALREADY: Ignore intentional compact LSB badge [tests/test_v484_account_page_rendering.py]
- ALREADY: Current core CI timeout [tests/test_v511_windows_pytest_diagnostics.py]
- ALREADY: Current watchdog constants [tests/test_v511_windows_pytest_diagnostics.py]
- ALREADY: Current ordinary-group status [tests/test_v4721_rpc_optimization_v10.py]
- ALREADY: Current account-scoped secret key [tests/test_v478_final_audit.py]

## Unmatched or manual

