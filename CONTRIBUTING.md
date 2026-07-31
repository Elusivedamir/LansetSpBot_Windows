# Contributing

Keep changes small and preserve the documented Telegram safety contracts: no automatic replay after an ambiguous mutating RPC, ordinary-group messages only through the durable campaign route, confirmed membership before channel commenting, confirmed message id plus durable receipt for success, and account-scoped state.

Before proposing a change, run the manifest and lock checks, compileall, Ruff, Mypy, the full pytest suite under coverage, and the relevant Windows source or release proof. Do not include sessions, databases, API keys, proxy credentials, screenshots containing user data, generated release output, or local logs.

Release, signing, publishing, push, merge, database migration, and secret rotation require separate authorization.
