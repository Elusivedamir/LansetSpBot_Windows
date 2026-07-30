# LansetSpBot — combined architecture stages



This patch is intentionally additive and backwards compatible. It introduces

the reusable boundaries for all five architecture stages without rewriting the

working Windows GUI, SQLCipher schema, or Telegram runtime in one unsafe jump.



## Stage 1 — state contracts



Implemented in `core/campaign_state.py` by the previous patch:



- typed campaign, task, and delivery states;

- legal transition map;

- terminal-state protection;

- fail-closed transition validation.



## Stage 2 — atomic campaign/task/delivery operations



`storage/atomic_workflow.py` adds:



- one outer transaction boundary over existing repositories;

- reserve + enqueue atomic workflow;

- durable success finalization workflow;

- validated compare-and-set campaign transitions;

- explicit concurrent-state failure.



Production repositories can migrate operation-by-operation instead of a broad

database rewrite.



## Stage 3 — centralized Telegram request policy



`services/telegram_request_policy.py` adds one Telethon-independent policy for:



- FloodWait defer decisions and buffer;

- uncertain SEND result after dispatch started;

- retryable transport failures;

- non-retryable permission/session restrictions;

- fail-closed unknown failures.



## Stage 4 — thinner ServiceAPI and explicit startup phases



`services/application_facade.py` provides typed account, comment, join, and

runtime namespaces over the existing ServiceAPI. Existing GUI code remains

compatible while new code can stop depending on the full API surface.



`core/startup_pipeline.py` provides explicit, fail-closed startup phases so

recovery can later move out of `ApplicationContainer.__init__` incrementally.



## Stage 5 — Telegram RPC performance audit



`core/rpc_audit.py` provides secret-free process-local counters:



- calls by operation;

- calls by account;

- minimum-interval violations;

- likely immediate duplicate calls.



It deliberately records no payloads, message text, credentials, phone numbers,

usernames, proxy values, or tokens.



## Integration order after Windows CI



1. Wrap comment reserve/enqueue and success finalization with `AtomicWorkflow`.

2. Route worker RPC exception classification through `TelegramRequestPolicy`.

3. Expose `ApplicationFacade` from `ApplicationContainer`.

4. Move existing recovery calls into `StartupPipeline`, preserving order.

5. Add `RPCAudit.record()` at the single paced Telegram client boundary.



Each integration should be a small commit with full Windows CI evidence.

