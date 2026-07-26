from __future__ import annotations

import inspect
from typing import Any, Callable

# Modules that own the real MTProto boundary. A callable defined here is a
# production facade: if a safety barrier cannot be delivered to it, that is a
# defect and must fail closed rather than silently dispatch an unguarded
# mutating request.
_PRODUCTION_FACADE_PREFIXES = ("services.",)


class DispatchBarrierUndeliverableError(RuntimeError):
    """A production facade cannot accept the Stop/local-ban dispatch barrier."""


def _accepts_barrier(callback: Callable[..., Any]) -> bool | None:
    """Return True/False, or None when the signature cannot be inspected."""

    try:
        parameters = inspect.signature(callback).parameters.values()
    except (TypeError, ValueError):
        return None
    if any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters):
        return True
    return any(parameter.name == "dispatch_barrier" for parameter in parameters)


def _is_production_facade(callback: Callable[..., Any]) -> bool:
    module = str(getattr(callback, "__module__", "") or "")
    return module.startswith(_PRODUCTION_FACADE_PREFIXES)


def dispatch_barrier_kwargs(
    callback: Callable[..., Any], barrier: Any
) -> dict[str, Any]:
    """Return a compatible ``dispatch_barrier`` keyword for one async facade.

    Production Telegram facades expose the keyword and are required to: the
    barrier is what blocks a mutating RPC after Stop, a local ban or a
    RESTRICTED account, so quietly dropping it would fail open. When a
    production facade cannot accept it, this raises instead.

    A number of intentionally tiny audit/test doubles predate the keyword.
    Those are not part of the MTProto boundary, so they keep the permissive
    path without swallowing a TypeError raised *inside* the callback.
    """

    if barrier is None:
        return {}
    accepts = _accepts_barrier(callback)
    if accepts:
        return {"dispatch_barrier": barrier}
    if _is_production_facade(callback):
        raise DispatchBarrierUndeliverableError(
            "Telegram facade "
            f"{getattr(callback, '__qualname__', callback)!r} cannot accept "
            "dispatch_barrier; refusing to dispatch an unguarded request"
        )
    return {}
