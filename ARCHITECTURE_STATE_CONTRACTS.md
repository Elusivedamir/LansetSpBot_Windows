# LansetSpBot state contracts — phase 1

This file records the architectural contract introduced by the first safe
refactoring phase. It does not change the existing runtime behavior yet.

## Sources of truth

- **Campaign** stores business lifecycle state.
- **Task** stores the technical queue execution state.
- **Delivery** stores the outcome of one concrete Telegram send attempt.
- A queue task must not be treated as the sole source of campaign state.
- A timed-out send with an unknown remote outcome must become `uncertain` and
  must not be retried automatically.

## Campaign lifecycle

Canonical states:

- `draft`
- `planned`
- `running`
- `paused_user`
- `paused_floodwait`
- `paused_restriction`
- `completed`
- `cancelled`
- `failed`

Terminal states are `completed`, `cancelled`, and `failed`.

The transition validator is implemented in `core/campaign_state.py`. During
later phases, every database status update should become a compare-and-set
operation that validates its expected current state before committing.

## Atomicity boundary planned for phase 2

The following groups must eventually be committed in one database transaction:

1. Reserve delivery + validate campaign/account + enqueue task.
2. Store Telegram message id + mark delivery succeeded + increment successful
   counter + consume quota.
3. Store FloodWait deadline + pause account/campaign + defer pending work.
4. Apply account restriction + block new JOIN/SEND + finalize in-flight work.
5. Cancel campaign + cancel undispatched tasks + preserve final ledger.

## Compatibility rule

Enum values are lowercase strings matching the existing persistence/API style.
This phase adds types and tests only; it deliberately avoids a schema migration
or broad replacement of existing string statuses.
