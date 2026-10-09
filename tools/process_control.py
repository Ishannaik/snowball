#!/usr/bin/env python3
"""Owned process trees that cannot orphan grandchildren.

The ownership boundary is installed before the child executes:
  Windows — suspended CreateProcess, assign to a kill-on-close Job Object, then NtResumeProcess.
  POSIX   — start_new_session so the child is a process-group leader (pgid == pid).

stop_owned_process_tree signals only that job or process group. It never kills by image name.
"""

from __future__ import annotations

import contextlib
import ctypes
import os
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import IO

__all__ = [
    "OwnedProcess",
    "launch_owned_process",
    "run_owned_process_tree",
    "stop_owned_process_tree",
]

# Win32: JobObjectExtendedLimitInformation = 9, KILL_ON_JOB_CLOSE, CREATE_SUSPENDED.
JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
CREATE_SUSPENDED = 0x00000004
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
_ERROR_ACCESS_DENIED = 5

kernel32 = None
ntdll = None

if os.name == "nt":
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll", use_last_error=True)

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", ctypes.c_void_p),
            ("bInheritHandle", wintypes.BOOL),
        ]

    class JOBOBJECT_BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("TotalUserTime", ctypes.c_int64),
            ("TotalKernelTime", ctypes.c_int64),
            ("ThisPeriodTotalUserTime", ctypes.c_int64),
            ("ThisPeriodTotalKernelTime", ctypes.c_int64),
            ("TotalPageFaultCount", wintypes.DWORD),
            ("TotalProcesses", wintypes.DWORD),
            ("ActiveProcesses", wintypes.DWORD),
            ("TotalTerminatedProcesses", wintypes.DWORD),
        ]

    if ctypes.sizeof(ctypes.c_void_p) == 8:
        # LLP64 layout used by the Win32 headers. A wrong size makes SetInformationJobObject fail
        # or, worse, write past the struct.
        if ctypes.sizeof(JOBOBJECT_BASIC_LIMIT_INFORMATION) != 64:
            raise RuntimeError(
                f"JOBOBJECT_BASIC_LIMIT_INFORMATION size {ctypes.sizeof(JOBOBJECT_BASIC_LIMIT_INFORMATION)} != 64"
            )
        if ctypes.sizeof(IO_COUNTERS) != 48:
            raise RuntimeError(f"IO_COUNTERS size {ctypes.sizeof(IO_COUNTERS)} != 48")
        if ctypes.sizeof(JOBOBJECT_BASIC_ACCOUNTING_INFORMATION) != 48:
            raise RuntimeError(
                "JOBOBJECT_BASIC_ACCOUNTING_INFORMATION size "
                f"{ctypes.sizeof(JOBOBJECT_BASIC_ACCOUNTING_INFORMATION)} != 48"
            )
        if ctypes.sizeof(JOBOBJECT_EXTENDED_LIMIT_INFORMATION) != 144:
            raise RuntimeError(
                "JOBOBJECT_EXTENDED_LIMIT_INFORMATION size "
                f"{ctypes.sizeof(JOBOBJECT_EXTENDED_LIMIT_INFORMATION)} != 144"
            )

    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateJobObject.restype = wintypes.BOOL
    kernel32.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    kernel32.QueryInformationJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(SECURITY_ATTRIBUTES),
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.SetHandleInformation.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD]
    kernel32.SetHandleInformation.restype = wintypes.BOOL
    # NTSTATUS. Signed so a failure such as 0xC0000005 is negative, not a huge unsigned value.
    ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
    ntdll.NtResumeProcess.restype = ctypes.c_long


class OwnedProcess:
    """One launched process plus the handle that owns its descendants."""

    def __init__(
        self,
        proc: subprocess.Popen,
        pid: int,
        stdout_path: Path | None,
        stdout_file: IO[bytes] | None,
        job_handle: int | None,
        pgid: int | None,
    ) -> None:
        self.proc = proc
        self.pid = pid
        self.stdout_path = stdout_path
        self.stdout_file = stdout_file
        self.job_handle = job_handle
        self.pgid = pgid
        self._stopped = False

    def poll(self) -> int | None:
        return self.proc.poll()

    def wait(self, timeout: float | None = None) -> int:
        return self.proc.wait(timeout=timeout)

    @property
    def returncode(self) -> int | None:
        return self.proc.returncode

    def __enter__(self) -> OwnedProcess:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        stop_owned_process_tree(self)
        return False

    def __repr__(self) -> str:
        return (
            f"OwnedProcess(pid={self.pid}, returncode={self.returncode}, "
            f"pgid={self.pgid}, job_handle={self.job_handle})"
        )


def launch_owned_process(
    argv: list[str],
    cwd: str | Path | None = None,
    stdout_path: str | Path | None = None,
    env: dict[str, str] | None = None,
) -> OwnedProcess:
    """Start argv inside an owned tree. Descendants cannot appear before the boundary exists."""
    args = _argv(argv)
    cwd_s = None if cwd is None else os.fspath(cwd)
    out_path = None if stdout_path is None else Path(stdout_path)
    if env is not None:
        env = {str(key): str(value) for key, value in env.items()}

    stdout_file: IO[bytes] | None = None
    owned: OwnedProcess | None = None
    try:
        stdout_file, sink, stderr_sink = _open_stdout(out_path)
        if os.name == "nt":
            proc, job = _launch_windows(args, cwd_s, env, sink, stderr_sink)
            pgid = None
        else:
            proc = _launch_posix(args, cwd_s, env, sink, stderr_sink)
            job = None
            pgid = proc.pid
        owned = OwnedProcess(proc, proc.pid, out_path, stdout_file, job, pgid)
        return owned
    except BaseException:
        if owned is not None:
            stop_owned_process_tree(owned)
        else:
            _close_stream(stdout_file)
        raise


def stop_owned_process_tree(proc: OwnedProcess, grace_seconds: float = 5.0) -> None:
    """Stop this tree only. Flush stdout and leave the file on disk."""
    if proc._stopped:
        return
    proc._stopped = True
    try:
        if os.name == "nt":
            _stop_windows(proc, grace_seconds)
        else:
            _stop_posix(proc, grace_seconds)
    finally:
        _close_stdout(proc)
        _close_job(proc)


def run_owned_process_tree(
    argv: list[str],
    cwd: str | Path | None = None,
    stdout_path: str | Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
    check_output_fn: Callable[[str], bool] | None = None,
    poll_interval: float = 0.5,
) -> tuple[int | None, str]:
    """Run a tree until exit, matched output, timeout, or an exception. Always stops the tree."""
    temporary: Path | None = None
    path: Path | None
    if stdout_path is None:
        fd, name = tempfile.mkstemp(prefix="snowball-owned-", suffix=".log")
        os.close(fd)
        temporary = Path(name)
        path = temporary
    else:
        path = Path(stdout_path)

    owned: OwnedProcess | None = None
    timed_out = False
    text = ""
    try:
        owned = launch_owned_process(argv, cwd=cwd, stdout_path=path, env=env)
        deadline = None if timeout is None else time.monotonic() + float(timeout)
        interval = poll_interval if poll_interval > 0 else 0.05
        while True:
            if _output_matched(owned, check_output_fn) or owned.poll() is not None:
                break
            if deadline is not None and time.monotonic() >= deadline:
                timed_out = True
                break
            _sleep_chunk(deadline, interval)
    finally:
        if owned is not None:
            stop_owned_process_tree(owned)
        text = _read_path(path)
        if temporary is not None:
            with contextlib.suppress(OSError):
                temporary.unlink()
    if timed_out:
        raise TimeoutError(f"owned process exceeded {timeout}s")
    return owned.returncode if owned is not None else None, text


def _argv(argv: list[str]) -> list[str]:
    if isinstance(argv, (str, bytes)):
        raise TypeError("argv must be a sequence of arguments, not a string")
    args = [os.fspath(part) for part in argv]
    if not args or any(not part or isinstance(part, bytes) for part in args):
        raise ValueError("argv must be a non-empty list of text arguments")
    return list(args)


def _open_stdout(path: Path | None) -> tuple[IO[bytes] | None, IO[bytes] | int, IO[bytes] | int]:
    if path is None:
        return None, subprocess.DEVNULL, subprocess.DEVNULL
    path.parent.mkdir(parents=True, exist_ok=True)
    # Binary and unbuffered so the parent does not translate or buffer bytes the child writes.
    fh = _open_shared_stdout(path) if os.name == "nt" else open(path, "wb", buffering=0)  # noqa: SIM115
    return fh, fh, subprocess.STDOUT


def _open_shared_stdout(path: Path) -> IO[bytes]:
    """Inheritable write handle that still allows a concurrent reader."""
    assert kernel32 is not None
    generic_write = 0x40000000
    share_all = 0x00000001 | 0x00000002 | 0x00000004  # READ | WRITE | DELETE
    create_always = 2
    file_attribute_normal = 0x80
    inherit = 0x00000001
    sa = SECURITY_ATTRIBUTES()
    sa.nLength = ctypes.sizeof(SECURITY_ATTRIBUTES)
    sa.lpSecurityDescriptor = None
    sa.bInheritHandle = True
    handle = kernel32.CreateFileW(
        str(path),
        generic_write,
        share_all,
        ctypes.byref(sa),
        create_always,
        file_attribute_normal,
        None,
    )
    if _invalid_handle(handle):
        _raise_win(f"CreateFileW failed for {path}")
    if not kernel32.SetHandleInformation(int(handle), inherit, inherit):
        err = ctypes.get_last_error()
        _close_raw_handle(int(handle))
        raise ctypes.WinError(err, f"SetHandleInformation failed for {path}")
    try:
        # No _O_TEXT: the CRT fd stays binary, matching open(..., "wb").
        fd = msvcrt.open_osfhandle(int(handle), 0)
    except OSError:
        _close_raw_handle(int(handle))
        raise
    try:
        return open(fd, "wb", buffering=0, closefd=True)  # noqa: SIM115
    except BaseException:
        os.close(fd)
        raise


def _launch_posix(
    argv: list[str],
    cwd: str | None,
    env: dict[str, str] | None,
    stdout: IO[bytes] | int,
    stderr: IO[bytes] | int,
) -> subprocess.Popen:
    # setsid before exec: the child is the process-group leader and pgid == pid.
    return subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=stdout,
        stderr=stderr,
        start_new_session=True,
        close_fds=True,
    )


def _launch_windows(
    argv: list[str],
    cwd: str | None,
    env: dict[str, str] | None,
    stdout: IO[bytes] | int,
    stderr: IO[bytes] | int,
) -> tuple[subprocess.Popen, int]:
    job = _create_kill_on_close_job()
    proc: subprocess.Popen | None = None
    try:
        proc = _popen_windows(argv, cwd, env, stdout, stderr, CREATE_SUSPENDED)
        try:
            _assign_job(job, int(proc._handle))
        except OSError as exc:
            # Parent is already inside a job that rejects nesting. Break the child out and assign
            # it to ours before any of its code runs. Still suspended across the assign.
            if getattr(exc, "winerror", None) != _ERROR_ACCESS_DENIED:
                raise
            _kill_proc(proc)
            proc = None
            proc = _popen_windows(
                argv,
                cwd,
                env,
                stdout,
                stderr,
                CREATE_SUSPENDED | CREATE_BREAKAWAY_FROM_JOB,
            )
            _assign_job(job, int(proc._handle))
        _resume_process(int(proc._handle))
        return proc, job
    except BaseException:
        if proc is not None:
            _kill_proc(proc)
        _close_raw_handle(job)
        raise


def _popen_windows(
    argv: list[str],
    cwd: str | None,
    env: dict[str, str] | None,
    stdout: IO[bytes] | int,
    stderr: IO[bytes] | int,
    creationflags: int,
) -> subprocess.Popen:
    return subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=stdout,
        stderr=stderr,
        creationflags=creationflags,
        close_fds=True,
    )


def _create_kill_on_close_job() -> int:
    assert kernel32 is not None
    handle = kernel32.CreateJobObjectW(None, None)
    if _invalid_handle(handle):
        _raise_win("CreateJobObjectW failed")
    job = int(handle)
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ok = kernel32.SetInformationJobObject(
        job,
        JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(info),
        ctypes.sizeof(info),
    )
    if not ok:
        err = ctypes.get_last_error()
        _close_raw_handle(job)
        raise ctypes.WinError(err, "SetInformationJobObject failed")
    return job


def _assign_job(job: int, process_handle: int) -> None:
    assert kernel32 is not None
    if not kernel32.AssignProcessToJobObject(job, process_handle):
        _raise_win("AssignProcessToJobObject failed")


def _wait_job_drained(handle: int, timeout: float) -> None:
    assert kernel32 is not None
    info = JOBOBJECT_BASIC_ACCOUNTING_INFORMATION()
    deadline = time.monotonic() + timeout
    while True:
        ok = kernel32.QueryInformationJobObject(
            handle,
            JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
            None,
        )
        if not ok or int(info.ActiveProcesses) == 0:
            return
        if time.monotonic() >= deadline:
            return
        time.sleep(0.02)


def _resume_process(process_handle: int) -> None:
    assert ntdll is not None
    status = int(ntdll.NtResumeProcess(process_handle))
    if status < 0:
        raise OSError(f"NtResumeProcess failed with NTSTATUS 0x{status & 0xFFFFFFFF:08X}")


def _stop_windows(proc: OwnedProcess, grace_seconds: float) -> None:
    grace = max(0.0, float(grace_seconds))
    if proc.poll() is None:
        # Hard-kill of the root only. Descendants stay in the job until TerminateJobObject.
        with contextlib.suppress(OSError):
            proc.proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=grace)
    handle = proc.job_handle
    if handle and kernel32 is not None:
        kernel32.TerminateJobObject(int(handle), 1)
        # TerminateJobObject marks every member. Wait until the job reports none left so a
        # grandchild cannot still be scheduled when the caller checks.
        _wait_job_drained(int(handle), 5.0)
    with contextlib.suppress(subprocess.TimeoutExpired, OSError):
        proc.wait()


def _stop_posix(proc: OwnedProcess, grace_seconds: float) -> None:
    grace = max(0.0, float(grace_seconds))
    pgid = _foreign_pgid(proc)
    if pgid is None:
        _signal_root_only(proc, grace)
        return
    if proc.poll() is None or _group_alive(pgid):
        _killpg(pgid, signal.SIGTERM)
        _wait_group_quiet(proc, pgid, grace)
    if proc.poll() is None or _group_alive(pgid):
        _killpg(pgid, signal.SIGKILL)
        _wait_group_quiet(proc, pgid, 2.0)
    with contextlib.suppress(subprocess.TimeoutExpired, OSError):
        proc.wait()


def _signal_root_only(proc: OwnedProcess, grace: float) -> None:
    """Used only when the recorded pgid is our own group. Never signal that group."""
    if proc.poll() is None:
        _signal_pid(proc.pid, signal.SIGTERM)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=grace)
    if proc.poll() is None:
        _signal_pid(proc.pid, signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired, OSError):
        proc.wait()


def _wait_group_quiet(proc: OwnedProcess, pgid: int, grace: float) -> None:
    deadline = time.monotonic() + grace
    while True:
        if proc.poll() is not None and not _group_alive(pgid):
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        if proc.poll() is None:
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=min(0.1, remaining))
        else:
            time.sleep(min(0.05, remaining))


def _foreign_pgid(proc: OwnedProcess) -> int | None:
    pgid = proc.pgid
    if pgid is None or pgid <= 1:
        return None
    try:
        own = os.getpgrp()
    except (AttributeError, OSError):
        return pgid
    if pgid == own:
        return None
    return pgid


def _group_alive(pgid: int) -> bool:
    if pgid <= 1:
        return False
    try:
        os.killpg(pgid, 0)
    except OSError:
        return False
    return True


def _killpg(pgid: int, sig: int) -> None:
    if pgid <= 1:
        return
    try:
        os.killpg(pgid, sig)
    except OSError:
        return


def _signal_pid(pid: int, sig: int) -> None:
    if pid <= 1:
        return
    try:
        os.kill(pid, sig)
    except OSError:
        return


def _kill_proc(proc: subprocess.Popen) -> None:
    with contextlib.suppress(OSError):
        if proc.poll() is None:
            proc.terminate()
    try:
        proc.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass
    if os.name == "nt":
        # A CREATE_SUSPENDED process that ignored TerminateProcess is resumed only long enough to die.
        with contextlib.suppress(OSError):
            _resume_process(int(proc._handle))
        with contextlib.suppress(OSError):
            proc.terminate()
    else:
        with contextlib.suppress(OSError):
            proc.kill()
    with contextlib.suppress(subprocess.TimeoutExpired, OSError):
        proc.wait(timeout=5)


def _close_stdout(proc: OwnedProcess) -> None:
    fh = proc.stdout_file
    proc.stdout_file = None
    if fh is None:
        return
    try:
        if not fh.closed:
            try:
                fh.flush()
                os.fsync(fh.fileno())
            except OSError:
                pass
            fh.close()
    except OSError:
        return


def _close_stream(fh: IO[bytes] | None) -> None:
    if fh is None:
        return
    try:
        if not fh.closed:
            fh.close()
    except OSError:
        return


def _close_job(proc: OwnedProcess) -> None:
    handle = proc.job_handle
    proc.job_handle = None
    if not handle or os.name != "nt":
        return
    _close_raw_handle(int(handle))


def _close_raw_handle(handle: int | None) -> None:
    if not handle or kernel32 is None:
        return
    kernel32.CloseHandle(int(handle))


def _output_matched(owned: OwnedProcess, check_output_fn: Callable[[str], bool] | None) -> bool:
    if check_output_fn is None:
        return False
    return bool(check_output_fn(_read_stdout(owned)))


def _read_stdout(owned: OwnedProcess) -> str:
    fh = owned.stdout_file
    if fh is not None and not fh.closed:
        with contextlib.suppress(OSError):
            fh.flush()
    return _read_path(owned.stdout_path)


def _read_path(path: Path | None) -> str:
    if path is None or not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _sleep_chunk(deadline: float | None, interval: float) -> None:
    delay = interval
    if deadline is not None:
        delay = min(delay, max(0.0, deadline - time.monotonic()))
    if delay > 0:
        time.sleep(delay)


def _invalid_handle(value: int | None) -> bool:
    if not value:
        return True
    width = 8 * ctypes.sizeof(ctypes.c_void_p)
    masked = int(value) & ((1 << width) - 1)
    return masked == 0 or masked == (1 << width) - 1


def _raise_win(prefix: str) -> None:
    err = ctypes.get_last_error()
    raise ctypes.WinError(err, prefix)
