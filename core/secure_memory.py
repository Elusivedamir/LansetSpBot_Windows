"""Decide whether SQLCipher's locked-memory mode can actually run here.

``PRAGMA cipher_memory_security = ON`` routes every SQLCipher allocation
through a locked-page allocator so key material can never reach the page file.
On Windows that means ``VirtualLock``, whose capacity is bounded by the
process *minimum* working set.

When ``VirtualLock`` fails - Windows reports 1453, ``ERROR_WORKING_SET_QUOTA``
- SQLCipher 4.12.0 does not degrade to unlocked memory. Its failure path
re-enters the allocator and the C stack overflows, terminating the process with
``0xC00000FD`` before any Python exception can be raised. Reproduced on Windows
11 with Python 3.14 x64: keying an in-memory database, enabling the pragma and
running fifty ``CREATE TABLE`` statements kills the interpreter, while the same
script with the pragma off completes normally.

The mode is therefore enabled only where the OS has been shown to honour it.
The check asks Windows to raise the process minimum working set first, because
that is the quota ``VirtualLock`` draws from, and only then probes a lock large
enough to cover what SQLCipher will go on to allocate. A machine that refuses
gets a warning in the log and an unlocked - still fully encrypted - database,
which is the outcome the crashing path was trying to reach anyway.
"""

from __future__ import annotations

import logging
import sys
import threading

log = logging.getLogger(__name__)

# Enough headroom that SQLCipher's own locked allocations stay well inside the
# quota once the probe succeeds. Its secure heap holds page cache and key
# material for a handful of connections, far below this.
LOCK_PROBE_BYTES = 8 * 1024 * 1024
MINIMUM_WORKING_SET_BYTES = 64 * 1024 * 1024
MAXIMUM_WORKING_SET_BYTES = 256 * 1024 * 1024

ERROR_WORKING_SET_QUOTA = 1453
ERROR_NOT_ALL_ASSIGNED = 1300

_MEM_COMMIT = 0x1000
_MEM_RESERVE = 0x2000
_MEM_RELEASE = 0x8000
_PAGE_READWRITE = 0x04
_TOKEN_ADJUST_PRIVILEGES = 0x0020
_TOKEN_QUERY = 0x0008
_SE_PRIVILEGE_ENABLED = 0x00000002

_lock = threading.Lock()
_decision: bool | None = None


def _windows_probe(size: int) -> tuple[bool, int]:
    """Try to lock `size` bytes. Returns (locked, last_error)."""

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.VirtualAlloc.restype = wintypes.LPVOID
    kernel32.VirtualAlloc.argtypes = [
        wintypes.LPVOID,
        ctypes.c_size_t,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    kernel32.VirtualLock.restype = wintypes.BOOL
    kernel32.VirtualLock.argtypes = [wintypes.LPVOID, ctypes.c_size_t]
    kernel32.VirtualUnlock.restype = wintypes.BOOL
    kernel32.VirtualUnlock.argtypes = [wintypes.LPVOID, ctypes.c_size_t]
    kernel32.VirtualFree.restype = wintypes.BOOL
    kernel32.VirtualFree.argtypes = [wintypes.LPVOID, ctypes.c_size_t, wintypes.DWORD]

    address = kernel32.VirtualAlloc(
        None, size, _MEM_COMMIT | _MEM_RESERVE, _PAGE_READWRITE
    )
    if not address:
        return False, ctypes.get_last_error()
    try:
        if not kernel32.VirtualLock(address, size):
            return False, ctypes.get_last_error()
        kernel32.VirtualUnlock(address, size)
        return True, 0
    finally:
        kernel32.VirtualFree(address, 0, _MEM_RELEASE)


def _windows_enable_working_set_privilege() -> bool:
    """Enable SeIncreaseWorkingSetPrivilege for this process.

    The privilege is present but disabled in a standard user token, so no
    elevation is involved - the process is turning on something it already
    holds.
    """

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

    class LUID(ctypes.Structure):
        _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]

    class LUID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Luid", LUID), ("Attributes", wintypes.DWORD)]

    class TOKEN_PRIVILEGES(ctypes.Structure):
        _fields_ = [
            ("PrivilegeCount", wintypes.DWORD),
            ("Privileges", LUID_AND_ATTRIBUTES * 1),
        ]

    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.LookupPrivilegeValueW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        ctypes.POINTER(LUID),
    ]
    advapi32.AdjustTokenPrivileges.argtypes = [
        wintypes.HANDLE,
        wintypes.BOOL,
        ctypes.POINTER(TOKEN_PRIVILEGES),
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]

    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(),
        _TOKEN_ADJUST_PRIVILEGES | _TOKEN_QUERY,
        ctypes.byref(token),
    ):
        return False
    try:
        luid = LUID()
        if not advapi32.LookupPrivilegeValueW(
            None, "SeIncreaseWorkingSetPrivilege", ctypes.byref(luid)
        ):
            return False
        privileges = TOKEN_PRIVILEGES()
        privileges.PrivilegeCount = 1
        privileges.Privileges[0].Luid = luid
        privileges.Privileges[0].Attributes = _SE_PRIVILEGE_ENABLED
        if not advapi32.AdjustTokenPrivileges(
            token, False, ctypes.byref(privileges), 0, None, None
        ):
            return False
        # AdjustTokenPrivileges reports success even when nothing was granted.
        return int(ctypes.get_last_error()) != ERROR_NOT_ALL_ASSIGNED
    finally:
        kernel32.CloseHandle(token)


def _windows_raise_working_set() -> bool:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.SetProcessWorkingSetSize.restype = wintypes.BOOL
    kernel32.SetProcessWorkingSetSize.argtypes = [
        wintypes.HANDLE,
        ctypes.c_size_t,
        ctypes.c_size_t,
    ]
    return bool(
        kernel32.SetProcessWorkingSetSize(
            kernel32.GetCurrentProcess(),
            MINIMUM_WORKING_SET_BYTES,
            MAXIMUM_WORKING_SET_BYTES,
        )
    )


def _evaluate_windows() -> bool:
    locked, error = _windows_probe(LOCK_PROBE_BYTES)
    if locked:
        return True
    log.info(
        "VirtualLock refused %d bytes (error %d); raising the process working set",
        LOCK_PROBE_BYTES,
        error,
    )
    _windows_enable_working_set_privilege()
    if not _windows_raise_working_set():
        log.warning(
            "Windows refused to raise the process working set; SQLCipher locked "
            "memory stays off. The database remains fully encrypted, but key "
            "pages may be written to the page file."
        )
        return False
    locked, error = _windows_probe(LOCK_PROBE_BYTES)
    if not locked:
        log.warning(
            "Windows still refuses to lock %d bytes (error %d); SQLCipher locked "
            "memory stays off. The database remains fully encrypted, but key "
            "pages may be written to the page file.",
            LOCK_PROBE_BYTES,
            error,
        )
        return False
    log.info("Process working set raised; SQLCipher locked memory is available")
    return True


def secure_memory_available() -> bool:
    """Whether ``PRAGMA cipher_memory_security = ON`` is safe on this machine.

    The answer is computed once and reused; probing allocates and locks several
    megabytes, and the OS state it depends on does not change under us.
    """

    global _decision
    with _lock:
        if _decision is not None:
            return _decision
        if sys.platform != "win32":
            # POSIX mlock is what SQLCipher uses there, and it degrades to a
            # warning instead of recursing. No probe needed.
            _decision = True
            return _decision
        try:
            _decision = _evaluate_windows()
        except Exception:
            log.warning(
                "Could not determine whether memory can be locked; SQLCipher "
                "locked memory stays off",
                exc_info=True,
            )
            _decision = False
        return _decision


def reset_cached_decision() -> None:
    """Forget the probe result. Tests use this; production calls it never."""

    global _decision
    with _lock:
        _decision = None
