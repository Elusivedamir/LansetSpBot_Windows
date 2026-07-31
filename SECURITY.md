# Security policy

## Reporting a vulnerability

Do not publish credentials, session files, database contents, proxy passwords, API keys, or a working exploit in a public issue.

Use GitHub's private security-advisory reporting for this repository. Include the affected commit, Windows/Python version, a minimal reproduction, impact, and whether Telegram, SQLCipher, DPAPI, account isolation, or release provenance is involved.

## Supported security baseline

Only the current `main` branch is evaluated. A release is not considered security-proven unless its Windows proof bundle is tied to the exact commit, the checkout remains clean, dependency audit is green, and packaged/relocated self-tests pass.

Known vulnerabilities are release-blocking. Any temporary exception must be documented with the advisory identifier, affected surface, compensating control, owner, and expiry date.
