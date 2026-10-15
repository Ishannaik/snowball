#!/usr/bin/env python3
"""Owned-process launch and tree shutdown. No network.

python -m unittest tests.test_process_control -v
"""

from __future__ import annotations

import contextlib
import importlib.util
import os
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_STILL_ACTIVE = 259
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_ERROR_ACCESS_DENIED = 5


def import_tool(name: str, rel_path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


pc = import_tool("process_control", "tools/process_control.py")


def py_c(code: str) -> list[str]:
    return [sys.executable, "-u", "-c", code]


def pid_alive(pid: int) -> bool:
    """True while pid is still running. Does not signal the process."""
    if pid <= 0:
        return False
    if os.name == "nt":
        # os.kill(pid, 0) on Windows calls TerminateProcess. Query the exit code instead.
        return _win_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _win_pid_alive(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ctypes.get_last_error() == _ERROR_ACCESS_DENIED
    try:
        code = wintypes.DWORD()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        if not ok:
            return False
        return int(code.value) == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def wait_dead(pid: int, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.05)
    return not pid_alive(pid)


def wait_text(path: Path, needle: str, timeout: float = 15.0) -> str:
    deadline = time.monotonic() + timeout
    last = ""
    while True:
        try:
            if path.is_file():
                last = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            last = f"<unreadable {exc}>"
        if needle in last:
            return last
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out waiting for {needle!r} in {path}; have {last!r}")
        time.sleep(0.05)


def labeled_pid(text: str, label: str) -> int:
    prefix = f"{label}:"
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(prefix):
            return int(stripped[len(prefix) :].strip())
    raise AssertionError(f"{label} pid missing from {text!r}")


class ProcessControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="snowball-pc-")
        self.addCleanup(self.tmp.cleanup)
        self.extras: list[subprocess.Popen] = []
        self.addCleanup(self._stop_extras)

    def _stop_extras(self) -> None:
        for proc in self.extras:
            if proc.poll() is None:
                with contextlib.suppress(OSError):
                    proc.terminate()
                with contextlib.suppress(subprocess.TimeoutExpired, OSError):
                    proc.wait(timeout=5)

    def _spawn_sleeper(self) -> subprocess.Popen:
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.extras.append(proc)
        return proc

    def _track(self, owned: pc.OwnedProcess) -> pc.OwnedProcess:
        self.addCleanup(pc.stop_owned_process_tree, owned)
        return owned

    def _log(self, name: str) -> Path:
        return Path(self.tmp.name) / name

    def test_launch_and_successful_exit(self):
        path = self._log("ok.log")
        owned = self._track(
            pc.launch_owned_process(
                py_c("print('MARKER_OK', flush=True)"),
                cwd=self.tmp.name,
                stdout_path=path,
            )
        )
        self.assertGreater(owned.pid, 0)
        self.assertEqual(owned.proc.pid, owned.pid)
        if os.name == "nt":
            self.assertIsInstance(owned.job_handle, int)
            self.assertNotEqual(owned.job_handle, 0)
            self.assertIsNone(owned.pgid)
        else:
            self.assertEqual(owned.pgid, owned.pid)
            self.assertIsNone(owned.job_handle)
        rc = owned.wait(timeout=30)
        self.assertEqual(rc, 0)
        self.assertEqual(owned.returncode, 0)
        self.assertEqual(owned.poll(), 0)
        self.assertTrue(path.is_file())
        self.assertIn("MARKER_OK", path.read_text(encoding="utf-8"))
        # Already exited: stop must not raise, truncate, or delete the log.
        pc.stop_owned_process_tree(owned)
        pc.stop_owned_process_tree(owned)
        self.assertIsNone(owned.stdout_file)
        self.assertIn("MARKER_OK", path.read_text(encoding="utf-8"))

        run_path = self._log("run.log")
        run_rc, run_text = pc.run_owned_process_tree(
            py_c("print('RUN_MARKER', flush=True)"),
            cwd=self.tmp.name,
            stdout_path=run_path,
        )
        self.assertEqual(run_rc, 0)
        self.assertIn("RUN_MARKER", run_text)
        self.assertIn("RUN_MARKER", run_path.read_text(encoding="utf-8"))

    def test_tree_termination_kills_descendants_and_preserves_unrelated(self):
        unrelated = self._spawn_sleeper()
        path = self._log("tree.log")
        child_code = textwrap.dedent(
            """\
            import subprocess, sys, time
            p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            print(f"GRANDCHILD:{p.pid}", flush=True)
            p.wait()
            """
        )
        owned = self._track(pc.launch_owned_process(py_c(child_code), cwd=self.tmp.name, stdout_path=path))
        text = wait_text(path, "GRANDCHILD:")
        grandchild = labeled_pid(text, "GRANDCHILD")
        self.assertNotEqual(grandchild, unrelated.pid)
        self.assertNotEqual(owned.pid, unrelated.pid)
        self.assertTrue(pid_alive(unrelated.pid))
        self.assertTrue(pid_alive(owned.pid))
        self.assertTrue(pid_alive(grandchild))

        pc.stop_owned_process_tree(owned)

        self.assertTrue(wait_dead(owned.pid), f"child {owned.pid} still running")
        self.assertTrue(wait_dead(grandchild), f"grandchild {grandchild} still running")
        self.assertTrue(pid_alive(unrelated.pid), f"unrelated {unrelated.pid} was killed")
        self.assertIsNone(unrelated.poll())

    def test_preserve_stdout_on_termination(self):
        path = self._log("keep.log")
        owned = self._track(
            pc.launch_owned_process(
                py_c("print('PRESERVE_ME', flush=True); import time; time.sleep(60)"),
                cwd=self.tmp.name,
                stdout_path=path,
            )
        )
        wait_text(path, "PRESERVE_ME")
        self.assertIsNone(owned.poll())
        self.assertIsNotNone(owned.stdout_file)
        pc.stop_owned_process_tree(owned)
        self.assertTrue(wait_dead(owned.pid))
        self.assertIsNone(owned.stdout_file)
        self.assertTrue(path.is_file())
        text = path.read_text(encoding="utf-8")
        self.assertIn("PRESERVE_ME", text)
        self.assertGreater(len(text), 0)

    def test_cleanup_on_timeout(self):
        path = self._log("timeout.log")
        code = (
            "import os, time\n"
            "print('PID:' + str(os.getpid()), flush=True)\n"
            "print('TIMEOUT_MARKER', flush=True)\n"
            "time.sleep(60)\n"
        )
        with self.assertRaises(TimeoutError):
            pc.run_owned_process_tree(
                py_c(code),
                cwd=self.tmp.name,
                stdout_path=path,
                timeout=15,
                poll_interval=0.1,
            )
        self.assertTrue(path.is_file())
        text = path.read_text(encoding="utf-8")
        self.assertIn("TIMEOUT_MARKER", text)
        self.assertTrue(wait_dead(labeled_pid(text, "PID")))

    def test_cleanup_on_exception(self):
        path = self._log("exc.log")
        code = (
            "import os, time\n"
            "print('PID:' + str(os.getpid()), flush=True)\n"
            "print('EXC_MARKER', flush=True)\n"
            "time.sleep(60)\n"
        )
        pid = None
        with (
            self.assertRaises(RuntimeError),
            self._track(pc.launch_owned_process(py_c(code), cwd=self.tmp.name, stdout_path=path)) as owned,
        ):
            pid = owned.pid
            wait_text(path, "EXC_MARKER")
            self.assertIsNone(owned.poll())
            raise RuntimeError("parse failure")
        self.assertIsNotNone(pid)
        self.assertTrue(wait_dead(int(pid)))
        self.assertIn("EXC_MARKER", path.read_text(encoding="utf-8"))

        cancel_path = self._log("cancel.log")
        cancel_pid = None
        with (
            self.assertRaises(KeyboardInterrupt),
            self._track(
                pc.launch_owned_process(
                    py_c("import time; time.sleep(60)"),
                    cwd=self.tmp.name,
                    stdout_path=cancel_path,
                )
            ) as owned,
        ):
            cancel_pid = owned.pid
            self.assertIsNone(owned.poll())
            raise KeyboardInterrupt
        self.assertIsNotNone(cancel_pid)
        self.assertTrue(wait_dead(int(cancel_pid)))

        parse_path = self._log("parse.log")
        parse_code = (
            "import os, time\n"
            "print('PID:' + str(os.getpid()), flush=True)\n"
            "print('PARSE_ME', flush=True)\n"
            "time.sleep(60)\n"
        )

        def parse_fail(text: str) -> bool:
            if "PARSE_ME" in text:
                raise RuntimeError("parse failure")
            return False

        with self.assertRaises(RuntimeError):
            pc.run_owned_process_tree(
                py_c(parse_code),
                cwd=self.tmp.name,
                stdout_path=parse_path,
                timeout=20,
                poll_interval=0.1,
                check_output_fn=parse_fail,
            )
        parse_text = parse_path.read_text(encoding="utf-8")
        self.assertIn("PARSE_ME", parse_text)
        self.assertTrue(wait_dead(labeled_pid(parse_text, "PID")))

    def test_never_kills_by_executable_name(self):
        extra_a = self._spawn_sleeper()
        extra_b = self._spawn_sleeper()
        path = self._log("name.log")
        owned = self._track(
            pc.launch_owned_process(
                py_c("print('NAME_OK', flush=True); import time; time.sleep(60)"),
                cwd=self.tmp.name,
                stdout_path=path,
            )
        )
        wait_text(path, "NAME_OK")
        self.assertEqual(extra_a.args[0], sys.executable)
        self.assertEqual(extra_b.args[0], sys.executable)
        self.assertEqual(owned.proc.args[0], sys.executable)
        self.assertTrue(pid_alive(extra_a.pid))
        self.assertTrue(pid_alive(extra_b.pid))
        pc.stop_owned_process_tree(owned, grace_seconds=1.0)
        self.assertTrue(wait_dead(owned.pid))
        self.assertTrue(pid_alive(extra_a.pid), f"independent python {extra_a.pid} was killed")
        self.assertTrue(pid_alive(extra_b.pid), f"independent python {extra_b.pid} was killed")
        self.assertIsNone(extra_a.poll())
        self.assertIsNone(extra_b.poll())

    def test_run_owned_stops_when_output_matches(self):
        path = self._log("early.log")
        code = (
            "import os, time\n"
            "print('PID:' + str(os.getpid()), flush=True)\n"
            "print('EARLY', flush=True)\n"
            "time.sleep(60)\n"
        )
        rc, text = pc.run_owned_process_tree(
            py_c(code),
            cwd=self.tmp.name,
            stdout_path=path,
            timeout=20,
            poll_interval=0.1,
            check_output_fn=lambda chunk: "EARLY" in chunk,
        )
        self.assertIn("EARLY", text)
        self.assertIn("EARLY", path.read_text(encoding="utf-8"))
        self.assertIsNotNone(rc)
        self.assertTrue(wait_dead(labeled_pid(text, "PID")))

    def test_supervisor_stop_exits_without_killing_unrelated_processes(self):
        """A pre-existing STOP ends an empty schema-4 supervisor and leaves other processes alone."""
        personal = ROOT / "data" / "snowball.db"
        personal_existed = personal.exists()
        personal_stamp = personal.stat().st_mtime_ns if personal_existed else None
        repo_stop = ROOT / "STOP"
        repo_stop_existed = repo_stop.exists()
        runtime = import_tool("snowball_goal_runtime", "tools/goal_runtime.py")
        orch = runtime._sibling("orchestrator")
        db_path = self._log("empty.db")
        store = orch.Store.open(db_path)
        store.db.close()
        stop = self._log("STOP")
        stop.write_text("stop\n", encoding="utf-8")
        script = self._log("supervise_once.py")
        script.write_text(
            "import importlib.util, sys\n"
            "from pathlib import Path\n"
            "root = Path(sys.argv[1])\n"
            "db_path, stop_path = sys.argv[2], sys.argv[3]\n"
            "runtime_path = root / 'tools' / 'goal_runtime.py'\n"
            "spec = importlib.util.spec_from_file_location('snowball_goal_runtime', runtime_path)\n"
            "module = importlib.util.module_from_spec(spec)\n"
            "sys.modules['snowball_goal_runtime'] = module\n"
            "spec.loader.exec_module(module)\n"
            "orchestrator = module._sibling('orchestrator')\n"
            "code = module.supervise(orchestrator.Config(), None, False, db_path=db_path, stop_file=stop_path, max_topics=1)\n"
            "print('SUPERVISE_RC=' + str(code), flush=True)\n",
            encoding="utf-8",
        )
        unrelated = self._spawn_sleeper()
        log_path = self._log("supervise.log")
        code, text = pc.run_owned_process_tree(
            [sys.executable, "-u", str(script), str(ROOT), str(db_path), str(stop)],
            cwd=str(ROOT),
            stdout_path=log_path,
            timeout=180,
        )
        self.assertEqual(code, 0, text)
        self.assertIn("SUPERVISE_RC=0", text)
        self.assertTrue(pid_alive(unrelated.pid))
        self.assertIsNone(unrelated.poll())
        conn = sqlite3.connect(db_path)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM goals").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM fetches").fetchone()[0], 0)
            version = conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0]
            self.assertEqual(str(version), "4")
        finally:
            conn.close()
        self.assertEqual(repo_stop.exists(), repo_stop_existed)
        if personal_existed:
            self.assertEqual(personal.stat().st_mtime_ns, personal_stamp)
        else:
            self.assertFalse(personal.exists())


if __name__ == "__main__":
    unittest.main()
