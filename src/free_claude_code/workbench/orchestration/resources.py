"""Cap concurrent sub-agents by available physical memory (stdlib only).

Each Claude Code node process takes ~400 MB; dispatch keeps >= 1.5 GB free.
"""

import ctypes
import os
import sys

AGENT_BYTES = 400 * 1024**2
HEADROOM_BYTES = 1536 * 1024**2


def available_memory_bytes() -> int | None:
    """Available physical memory, or None when the platform does not say."""

    try:
        if sys.platform == "win32":

            class _MemoryStatusEx(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = _MemoryStatusEx()
            status.dwLength = ctypes.sizeof(_MemoryStatusEx)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return None
            return int(status.ullAvailPhys)
        try:
            with open("/proc/meminfo", encoding="ascii") as meminfo:
                for line in meminfo:
                    if line.startswith("MemAvailable:"):
                        return int(line.split()[1]) * 1024
        except OSError:
            pass
        return os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    except OSError, ValueError, AttributeError:
        return None  # e.g. macOS has no SC_AVPHYS_PAGES


def memory_parallel_cap(requested: int, available: int | None = None) -> int:
    """``requested`` lowered so each agent fits in memory above the headroom; >= 1.

    ponytail: sampled once per plan, not per dispatch; re-sample in the scheduler
    if long runs see memory swing.
    """

    free = available_memory_bytes() if available is None else available
    if free is None:
        return requested
    fits = (free - HEADROOM_BYTES) // AGENT_BYTES
    return max(1, min(requested, fits))
