# Experimental account activity runner

This is a separate one-shot runner for Telegram accounts owned and controlled by the operator. It does not alter the main GUI, campaigns, or queue worker.

## Boundaries

- Close LansetSpBot before running it. The runner uses the same single-instance lock and encrypted Telethon session.
- Every private dialog, existing group, and join target must be explicitly listed in `account_activity.json`.
- Messages are operator-written templates. The runner does not generate text, discover recipients, or message bots.
- Private messages are limited to 0–2 per run and one per target per configured cooldown.
- Reactions are limited to 0–3 per run, only on the latest fetched incoming messages, and are never automatically replayed after an ambiguous network result or process crash.
- Group reads are limited to 0–5 explicit groups per run.
- JOIN attempts are limited to 7–20 in a rolling seven-day window, 0–3 per run, with at least 4–48 hours between attempts. A target is reserved before dispatch, so a pending or unknown result cannot bypass the weekly or spacing limits. Pending requests are still not reported as confirmed membership.
- FloodWait, PeerFlood, authorization loss, account restriction, an unknown mutating result, or detected loss of the durable warmup lease stops and cancels the session. FloodWait is persisted in the project-wide account RPC cooldown; critical Telegram restrictions are persisted in the normal RESTRICTED state before the lease is released.
- The rolling ledger is stored in the account-scoped SQLCipher setting `automation.account_activity.ledger.v1`.

Automation does not guarantee that Telegram will not restrict an account. Keep targets and messages legitimate and follow Telegram rules and recipient consent.

## First run

1. Run `4_RUN_ACCOUNT_ACTIVITY_EXPERIMENTAL.cmd` once. It creates `account_activity.json` from the example and exits.
2. Edit `account_id`, existing dialog/group allowlists, operator messages, and explicit join targets.
3. Run the CMD again. This is local validation only: it checks the JSON, account record and saved configuration without opening Telegram, scanning dialogs or sending RPC requests.
4. For a real one-shot session, run from Command Prompt:

```bat
4_RUN_ACCOUNT_ACTIVITY_EXPERIMENTAL.cmd --execute
```

The runner is intentionally not a daemon or hidden background process. A later integration can schedule the same bounded session through the existing queue after this experiment is reviewed.

## Взаимная блокировка с кампаниями

При реальном запуске (`--execute`) runner получает для выбранного аккаунта
временную lease-блокировку в SQLCipher-базе. Пока она активна:

- кампания комментирования не создаётся;
- кампания вступлений не создаётся;
- GUI получает ошибку и показывает окно: «Аккаунт сейчас находится на прогреве. Попробуйте запустить кампанию после окончания прогрева.»;
- второй процесс прогрева того же аккаунта не запускается.

В обратную сторону действует тот же запрет: прогрев не начинается, если у
аккаунта есть активная, приостановленная или ожидающая сеть кампания.

При нормальном завершении блокировка удаляется сразу. Runner обновляет её
heartbeat во время работы; после аварийного завершения она автоматически
истекает, поэтому аккаунт не остаётся заблокированным навсегда.

## Применение патча и SHA256SUMS

Проект проверяет строгий `SHA256SUMS.txt`. Применяйте исправленный патч через
`APPLY_ACCOUNT_ACTIVITY_V5.ps1`: скрипт использует `git apply --index`, затем
пересобирает manifest штатным `tools/generate_manifest.py`. Кэш pytest, pyc и
другие локальные артефакты в пакет не входят.

## Ограничение внешнего runner

Runner сохраняет общий `SingleInstance`, потому что параллельно открывать одну
Telethon-сессию из GUI и второго процесса небезопасно. Поэтому при текущем
экспериментальном внешнем запуске основное окно закрыто. Backend-блокировка уже
защищает API и базу, но увидеть popup непосредственно во время внешнего прогрева
можно будет только после переноса runner в штатную очередь GUI.

Этот экспериментальный runner предназначен для запуска из полного исходного
проекта с установленным Python-окружением. Он не является отдельной функцией
собранного PyInstaller EXE.
