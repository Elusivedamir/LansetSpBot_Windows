# Architecture foundations status

These modules are **experimental scaffolding** and are excluded from release-readiness claims until they are connected to real caller paths and covered by integration tests:

| Module | Status | Reason |
|---|---|---|
| `core/campaign_state.py` | experimental | Its enums and transition table are not the persisted runtime source of truth. |
| `storage/atomic_workflow.py` | experimental | Used only by architecture characterization tests, not production queue/database callers. |
| `services/telegram_request_policy.py` | experimental | Production Telegram RPC paths use the established runtime policy modules instead. |
| `services/application_facade.py` | experimental | GUI and workers still call the legacy `ServiceAPI` surface directly. |
| `core/startup_pipeline.py` | experimental | Real startup remains in `main.py` and `ApplicationContainer`. |
| `core/rpc_audit.py` | experimental | It is not wired into production transport calls. |

`experimental` means the module may be imported by tests or another experimental module, but it must not be cited as evidence that production behavior is integrated. Promotion to `production-integrated` requires a real caller, migration/compatibility analysis where persisted state is involved, and integration tests through that caller.
