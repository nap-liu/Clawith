"""Black-box regression tests for the Clawith `/v1/shell` lifecycle patch.

Run inside the aio-sandbox container so PID assertions observe the same /proc:

    python /opt/clawith-tests/test_shell_lifecycle.py -v
"""

import json
import os
import signal
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor


API_URL = os.environ.get("SANDBOX_TEST_API_URL", "http://127.0.0.1:8091")


def request(method: str, path: str, data: dict | None = None, timeout: float = 20):
    payload = None if data is None else json.dumps(data).encode()
    req = urllib.request.Request(
        API_URL + path,
        data=payload,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def read_pid(path: str) -> int:
    with open(path) as handle:
        return int(handle.read().strip())


def pid_exists(pid: int) -> bool:
    return os.path.exists(f"/proc/{pid}")


def wait_pid_gone(pid: int, timeout: float = 3) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pid_exists(pid):
            return True
        time.sleep(0.025)
    return not pid_exists(pid)


class ShellLifecycleTest(unittest.TestCase):
    def setUp(self):
        token = uuid.uuid4().hex[:10]
        self.session_id = f"clawith-lifecycle-{token}"
        self.prefix = f"/tmp/clawith-lifecycle-{token}"
        status, body = request(
            "POST",
            "/v1/shell/sessions/create",
            {"id": self.session_id, "exec_dir": "/tmp"},
        )
        self.assertEqual(status, 200, body)

    def tearDown(self):
        request("DELETE", f"/v1/shell/sessions/{self.session_id}")
        for suffix in ("fg.pid", "child.pid", "tree.pids", "bg.pid", "bg.log"):
            path = f"{self.prefix}-{suffix}"
            try:
                pid = read_pid(path)
            except (FileNotFoundError, ValueError):
                pid = 0
            if pid and pid_exists(pid):
                try:
                    os.killpg(os.getpgid(pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

    def exec(self, command: str, *, timeout: float = 8):
        payload = {
            "id": self.session_id,
            "command": command,
            "timeout": timeout,
        }
        status, body = request("POST", "/v1/shell/exec", payload, timeout=timeout + 8)
        self.assertEqual(status, 200, body)
        return body

    def test_timeout_kills_tree_and_preserves_shell_state_and_background(self):
        bg_pidfile = f"{self.prefix}-bg.pid"
        setup = self.exec(
            "export CLAWITH_TIMEOUT_MARKER=preserved; "
            "cd /var/tmp; "
            f"nohup sleep 120 >{self.prefix}-bg.log 2>&1 & "
            f"echo $! > {bg_pidfile}; echo setup-ok"
        )
        self.assertEqual(setup["data"]["status"], "completed")
        bg_pid = read_pid(bg_pidfile)
        self.assertTrue(pid_exists(bg_pid))

        fg_pidfile = f"{self.prefix}-fg.pid"
        child_pidfile = f"{self.prefix}-child.pid"
        command = (
            "bash -c 'trap \"\" HUP INT TERM; "
            f"echo $$ > {fg_pidfile}; "
            "(trap \"\" HUP INT TERM; "
            f"echo $BASHPID > {child_pidfile}; while :; do sleep 1; done) & "
            "wait'"
        )
        started = time.monotonic()
        result = self.exec(command, timeout=1)
        elapsed = time.monotonic() - started

        self.assertEqual(result["data"]["status"], "hard_timeout", result)
        self.assertLess(elapsed, 6, result)
        self.assertTrue(wait_pid_gone(read_pid(fg_pidfile)))
        self.assertTrue(wait_pid_gone(read_pid(child_pidfile)))
        self.assertTrue(pid_exists(bg_pid), "pre-existing background process was killed")

        follow = self.exec(
            'printf "marker=%s cwd=%s" "$CLAWITH_TIMEOUT_MARKER" "$PWD"'
        )
        self.assertEqual(follow["data"]["status"], "completed", follow)
        self.assertIn("marker=preserved cwd=/var/tmp", follow["data"]["output"])

    def test_repeated_timeouts_do_not_poison_followup_calls(self):
        for index in range(5):
            pidfile = f"{self.prefix}-fg.pid"
            result = self.exec(
                f"bash -c 'trap \"\" HUP INT TERM; echo $$ > {pidfile}; "
                "while :; do sleep 1; done'",
                timeout=0.5,
            )
            self.assertEqual(result["data"]["status"], "hard_timeout", result)
            self.assertTrue(wait_pid_gone(read_pid(pidfile)))
            follow = self.exec(f"echo followup-{index}")
            self.assertEqual(follow["data"]["status"], "completed", follow)
            self.assertIn(f"followup-{index}", follow["data"]["output"])

    def test_shell_builtin_timeout_preserves_state(self):
        self.exec("export BUILTIN_MARKER=preserved")
        result = self.exec("while :; do :; done", timeout=0.5)
        self.assertEqual(result["data"]["status"], "hard_timeout", result)
        follow = self.exec('echo "marker=$BUILTIN_MARKER"')
        self.assertEqual(follow["data"]["status"], "completed", follow)
        self.assertIn("marker=preserved", follow["data"]["output"])

    def test_signal_trapping_shell_builtin_cannot_poison_session(self):
        result = self.exec(
            "trap '' INT TERM; while :; do :; done",
            timeout=0.5,
        )
        self.assertEqual(result["data"]["status"], "hard_timeout", result)
        follow = self.exec("echo recovered-after-reset")
        self.assertEqual(follow["data"]["status"], "completed", follow)
        self.assertIn("recovered-after-reset", follow["data"]["output"])

    def test_timeout_kills_wide_signal_resistant_process_tree(self):
        parent_pidfile = f"{self.prefix}-fg.pid"
        tree_pidfile = f"{self.prefix}-tree.pids"
        result = self.exec(
            "bash -c 'trap \"\" HUP INT TERM; "
            f"echo $$ > {parent_pidfile}; : > {tree_pidfile}; "
            "for i in $(seq 1 32); do "
            "(trap \"\" HUP INT TERM; while :; do sleep 1; done) & "
            f"echo $! >> {tree_pidfile}; "
            "done; wait'",
            timeout=0.5,
        )
        self.assertEqual(result["data"]["status"], "hard_timeout", result)
        with open(tree_pidfile) as handle:
            pids = [read_pid(parent_pidfile)] + [
                int(line.strip()) for line in handle if line.strip()
            ]
        self.assertEqual(len(pids), 33)
        survivors = [pid for pid in pids if not wait_pid_gone(pid)]
        self.assertEqual(survivors, [])
        follow = self.exec("echo wide-tree-followup-ok")
        self.assertEqual(follow["data"]["status"], "completed", follow)
        self.assertIn("wide-tree-followup-ok", follow["data"]["output"])

    def test_output_flood_timeout_does_not_poison_session(self):
        result = self.exec("yes clawith-timeout-flood", timeout=0.5)
        self.assertEqual(result["data"]["status"], "hard_timeout", result)
        follow = self.exec("echo flood-followup-ok")
        self.assertEqual(follow["data"]["status"], "completed", follow)
        self.assertIn("flood-followup-ok", follow["data"]["output"])

    def test_delete_reaps_signal_resistant_background_process(self):
        bg_pidfile = f"{self.prefix}-bg.pid"
        result = self.exec(
            f"nohup bash -c 'trap \"\" HUP INT TERM; while :; do sleep 1; done' "
            f">{self.prefix}-bg.log 2>&1 & echo $! > {bg_pidfile}"
        )
        self.assertEqual(result["data"]["status"], "completed", result)
        bg_pid = read_pid(bg_pidfile)
        self.assertTrue(pid_exists(bg_pid))

        status, body = request(
            "DELETE", f"/v1/shell/sessions/{self.session_id}"
        )
        self.assertEqual(status, 200, body)
        self.assertTrue(wait_pid_gone(bg_pid), f"background PID {bg_pid} survived DELETE")


class ShellLifecycleStressTest(unittest.TestCase):
    def test_many_precreated_sessions_timeout_simultaneously(self):
        """Drive the timeout path concurrently without conflating session creation."""
        case_count = int(os.environ.get("SIMULTANEOUS_SESSION_CASES", "10"))
        round_count = int(os.environ.get("SIMULTANEOUS_SESSION_ROUNDS", "1"))
        start_epoch = float(os.environ.get("SIMULTANEOUS_START_EPOCH", "0"))
        command_timeout = float(os.environ.get("SHELL_STRESS_TIMEOUT", "0.5"))
        token = uuid.uuid4().hex[:10]
        sessions = [f"clawith-simultaneous-{token}-{index}" for index in range(case_count)]
        pidfiles = [f"/tmp/{session_id}.pid" for session_id in sessions]

        try:
            # Session creation has its own upstream concurrency characteristics.
            # Finish it first so the barrier below measures simultaneous command
            # timeout, termination, and follow-up usability only.
            for session_id in sessions:
                status, body = request(
                    "POST",
                    "/v1/shell/sessions/create",
                    {"id": session_id, "exec_dir": "/tmp"},
                    timeout=30,
                )
                self.assertEqual(status, 200, body)

            with ThreadPoolExecutor(max_workers=case_count) as pool:
                for round_index in range(round_count):
                    barrier = threading.Barrier(case_count)

                    def run_case(index: int) -> tuple[int, int]:
                        session_id = sessions[index]
                        pidfile = pidfiles[index]
                        barrier.wait(timeout=30)
                        if start_epoch:
                            time.sleep(max(0, start_epoch - time.time()))
                        status, body = request(
                            "POST",
                            "/v1/shell/exec",
                            {
                                "id": session_id,
                                "command": (
                                    "bash -c 'trap \"\" HUP INT TERM; "
                                    f"echo $$ > {pidfile}; while :; do sleep 1; done'"
                                ),
                                "timeout": command_timeout,
                            },
                            timeout=45,
                        )
                        if (
                            status != 200
                            or body["data"]["status"] != "hard_timeout"
                        ):
                            raise AssertionError(body)
                        pid = read_pid(pidfile)
                        if not wait_pid_gone(pid):
                            raise AssertionError(
                                f"round {round_index} case {index}: "
                                f"PID {pid} survived timeout"
                            )
                        marker = f"simultaneous-followup-{round_index}-{index}"
                        status, body = request(
                            "POST",
                            "/v1/shell/exec",
                            {
                                "id": session_id,
                                "command": f"echo {marker}",
                                "timeout": 5,
                            },
                            timeout=45,
                        )
                        if (
                            status != 200
                            or body["data"]["status"] != "completed"
                            or marker not in body["data"]["output"]
                        ):
                            raise AssertionError(body)
                        return index, pid

                    results = list(pool.map(run_case, range(case_count)))
                    self.assertEqual(len(results), case_count)
                    for index, pid in results:
                        self.assertFalse(
                            pid_exists(pid),
                            f"round {round_index} case {index}: PID {pid} remains",
                        )
        finally:
            for session_id, pidfile in zip(sessions, pidfiles, strict=True):
                request("DELETE", f"/v1/shell/sessions/{session_id}")
                try:
                    os.unlink(pidfile)
                except FileNotFoundError:
                    pass

    def test_concurrent_timeouts_leave_every_session_usable_and_no_live_pid(self):
        case_count = int(os.environ.get("SHELL_STRESS_CASES", "16"))
        worker_count = int(os.environ.get("SHELL_STRESS_WORKERS", "8"))
        command_timeout = float(os.environ.get("SHELL_STRESS_TIMEOUT", "0.5"))

        def run_case(index: int) -> tuple[int, int]:
            token = uuid.uuid4().hex[:10]
            session_id = f"clawith-stress-{token}"
            pidfile = f"/tmp/clawith-stress-{token}.pid"
            pid = 0
            try:
                status, body = request(
                    "POST",
                    "/v1/shell/sessions/create",
                    {"id": session_id, "exec_dir": "/tmp"},
                )
                if status != 200:
                    raise AssertionError(body)
                status, body = request(
                    "POST",
                    "/v1/shell/exec",
                    {
                        "id": session_id,
                        "command": (
                            "bash -c 'trap \"\" HUP INT TERM; "
                            f"echo $$ > {pidfile}; while :; do sleep 1; done'"
                        ),
                        "timeout": command_timeout,
                    },
                    timeout=10,
                )
                if status != 200 or body["data"]["status"] != "hard_timeout":
                    raise AssertionError(body)
                pid = read_pid(pidfile)
                if not wait_pid_gone(pid):
                    raise AssertionError(f"case {index}: PID {pid} survived timeout")
                status, body = request(
                    "POST",
                    "/v1/shell/exec",
                    {
                        "id": session_id,
                        "command": f"echo stress-followup-{index}",
                        "timeout": 3,
                    },
                    timeout=10,
                )
                if (
                    status != 200
                    or body["data"]["status"] != "completed"
                    or f"stress-followup-{index}" not in body["data"]["output"]
                ):
                    raise AssertionError(body)
                return index, pid
            finally:
                request("DELETE", f"/v1/shell/sessions/{session_id}")
                if pid and pid_exists(pid):
                    try:
                        os.killpg(os.getpgid(pid), signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
                try:
                    os.unlink(pidfile)
                except FileNotFoundError:
                    pass

        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            results = list(pool.map(run_case, range(case_count)))

        self.assertEqual(len(results), case_count)
        for index, pid in results:
            self.assertFalse(pid_exists(pid), f"case {index}: PID {pid} remains")

    def test_one_stateful_session_survives_repeated_timeouts(self):
        case_count = int(os.environ.get("SAME_SESSION_CASES", "20"))
        command_timeout = float(os.environ.get("SHELL_STRESS_TIMEOUT", "0.5"))
        token = uuid.uuid4().hex[:10]
        session_id = f"clawith-same-session-{token}"
        pidfile = f"/tmp/clawith-same-session-{token}.pid"
        bg_pidfile = f"/tmp/clawith-same-session-{token}.bg.pid"
        bg_log = f"/tmp/clawith-same-session-{token}.bg.log"
        bg_pid = 0
        try:
            status, body = request(
                "POST",
                "/v1/shell/sessions/create",
                {"id": session_id, "exec_dir": "/tmp"},
            )
            self.assertEqual(status, 200, body)
            status, body = request(
                "POST",
                "/v1/shell/exec",
                {
                    "id": session_id,
                    "command": (
                        "export STRESS_MARKER=preserved; "
                        f"nohup sleep 600 >{bg_log} 2>&1 & echo $! > {bg_pidfile}"
                    ),
                    "timeout": 3,
                },
            )
            self.assertEqual(status, 200, body)
            self.assertEqual(body["data"]["status"], "completed", body)
            bg_pid = read_pid(bg_pidfile)

            for index in range(case_count):
                status, body = request(
                    "POST",
                    "/v1/shell/exec",
                    {
                        "id": session_id,
                        "command": (
                            "bash -c 'trap \"\" HUP INT TERM; "
                            f"echo $$ > {pidfile}; while :; do sleep 1; done'"
                        ),
                        "timeout": command_timeout,
                    },
                    timeout=10,
                )
                self.assertEqual(status, 200, body)
                self.assertEqual(body["data"]["status"], "hard_timeout", body)
                pid = read_pid(pidfile)
                self.assertTrue(wait_pid_gone(pid), f"iteration {index}: PID {pid}")

                status, body = request(
                    "POST",
                    "/v1/shell/exec",
                    {
                        "id": session_id,
                        "command": 'printf "marker=%s" "$STRESS_MARKER"',
                        "timeout": 3,
                    },
                )
                self.assertEqual(status, 200, body)
                self.assertEqual(body["data"]["status"], "completed", body)
                self.assertIn("marker=preserved", body["data"]["output"])
                self.assertTrue(pid_exists(bg_pid), f"iteration {index}: background died")
        finally:
            request("DELETE", f"/v1/shell/sessions/{session_id}")
            if bg_pid:
                self.assertTrue(wait_pid_gone(bg_pid), f"background PID {bg_pid} survived")
            for path in (pidfile, bg_pidfile, bg_log):
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    pass


if __name__ == "__main__":
    unittest.main()
