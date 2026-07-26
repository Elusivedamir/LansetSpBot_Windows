"""Boot-aware monotonic clock helpers for persisted safety embargoes."""

from __future__ import annotations

import ctypes
import hashlib
import platform
import time
from functools import lru_cache
from pathlib import Path


def steady_time() -> float:
    """Return a process-independent monotonic value for the current OS boot."""

    return time.monotonic()


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:32]


def _linux_boot_identity() -> str:
    path = Path("/proc/sys/kernel/random/boot_id")
    try:
        value = path.read_text(encoding="ascii").strip()
    except OSError:
        return ""
    return _digest(f"linux:{value}") if value else ""


def _windows_uptime_seconds() -> float | None:
    try:
        loader = getattr(ctypes, "WinDLL", None)
        if loader is None:
            return None
        kernel32 = loader("kernel32", use_last_error=True)
        getter = kernel32.GetTickCount64
        getter.argtypes = []
        getter.restype = ctypes.c_ulonglong
        return float(getter()) / 1000.0
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def _estimated_boot_identity(prefix: str, uptime_seconds: float) -> str:
    # Quantization absorbs small scheduling/clock-read differences between
    # processes. A material wall-clock correction changes the bucket and forces
    # the conservative re-anchor path instead of shortening a Telegram wait.
    boot_epoch_bucket = int((time.time() - max(0.0, uptime_seconds)) // 30.0)
    return _digest(f"{prefix}:{boot_epoch_bucket}")


@lru_cache(maxsize=1)
def current_boot_identity() -> str:
    """Return a stable identifier for the current boot where possible.

    Windows uses GetTickCount64 and a
    quantized boot-epoch estimate; a system clock jump intentionally changes the
    identity, which causes a full conservative cooldown re-anchor.
    """

    system = platform.system().lower()
    if system == "linux":
        native = _linux_boot_identity()
        if native:
            return native
    elif system == "windows":
        uptime = _windows_uptime_seconds()
        if uptime is not None:
            return _estimated_boot_identity("windows", uptime)

    # time.monotonic() is normally system-wide and boot-relative. This fallback
    # remains conservative: a wall-clock correction changes the identity and
    # restarts the persisted fallback wait rather than allowing an early RPC.
    return _estimated_boot_identity(f"fallback:{system or 'unknown'}", steady_time())
