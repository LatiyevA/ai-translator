"""Launch a provider in a Windows job that closes with its supervisor."""

import ctypes
import os
import subprocess
import sys
from contextlib import contextmanager
from ctypes import wintypes
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread

_JOB_UNAVAILABLE = "Windows не разрешила создать управляемую группу для процессов провайдера."


def taskkill_command(pid: int) -> list[str]:
    tool = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
    return [str(tool), "/PID", str(pid), "/T", "/F"]


@contextmanager
def _parent_handle():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL

    handle = kernel.OpenProcess(0x00100000, True, os.getpid())  # SYNCHRONIZE, inheritable
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        yield handle
    finally:
        kernel.CloseHandle(handle)


def run_supervised(command: list[str], execute):
    with _parent_handle() as parent, TemporaryDirectory(prefix="ai-translator-job-") as directory:
        rejected = Path(directory, "rejected")
        supervisor = Path(__file__).with_name("windows_job.py")
        launcher = [sys.executable, "-I", str(supervisor), str(parent), str(rejected), *command]
        startup = subprocess.STARTUPINFO(lpAttributeList={"handle_list": [parent]})
        for breakaway in (0, subprocess.CREATE_BREAKAWAY_FROM_JOB):
            try:
                result = execute(launcher, {
                    "startupinfo": startup,
                    "close_fds": True,
                    "creationflags": subprocess.CREATE_NO_WINDOW | breakaway,
                })
            except OSError as exc:
                if breakaway and getattr(exc, "winerror", None) == 5:
                    raise OSError(_JOB_UNAVAILABLE) from exc
                raise
            if not rejected.is_file():
                return result
            # The supervisor writes this marker before the provider can start.
            rejected.unlink()
    raise OSError(_JOB_UNAVAILABLE)


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
        )
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimits),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _attach_current_process() -> None:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.GetCurrentProcess.argtypes = []
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL

    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    limits = _ExtendedLimits()
    limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    try:
        if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not kernel.AssignProcessToJobObject(job, kernel.GetCurrentProcess()):
            raise ctypes.WinError(ctypes.get_last_error())
    except OSError:
        kernel.CloseHandle(job)
        raise
    # The non-inheritable handle must stay open until this supervisor exits.


def _watch_parent(handle: int) -> bool:
    os.set_handle_inheritable(handle, False)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    if kernel.WaitForSingleObject(handle, 0) != 258:  # WAIT_TIMEOUT means the worker is still running.
        return False

    def monitor():
        kernel.WaitForSingleObject(handle, 0xFFFFFFFF)  # INFINITE
        os._exit(130)  # Process exit closes the job and terminates the provider tree.

    Thread(target=monitor, daemon=True).start()
    return True


def main(parent: int, rejected: Path, command: list[str]) -> int:
    try:
        _attach_current_process()
    except OSError:
        rejected.touch()
        return 125
    if not _watch_parent(parent):
        return 130
    return subprocess.call(command, stdin=0, stdout=1, stderr=2,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


if __name__ == "__main__":
    raise SystemExit(main(int(sys.argv[1]), Path(sys.argv[2]), sys.argv[3:]))
