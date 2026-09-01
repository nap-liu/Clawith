import os
import pathlib
import signal
import time
from collections.abc import Callable

from vendors.openhands.core.logger import openhands_logger as logger
from vendors.openhands.events.observation.commands import CMD_OUTPUT_PS1_END


def pane_shell_pid(pane) -> int | None:
    """Return the shell PID that owns this tmux pane."""
    try:
        output = pane.cmd('display-message', '-p', '#{pane_pid}').stdout
        if not output:
            return None
        pid = int(output[0].strip())
        return pid if pid > 1 else None
    except Exception:
        logger.warning('Unable to read tmux pane PID', exc_info=True)
        return None


def read_process_stat(pid: int) -> tuple[str, int, int, int, int] | None:
    """Return (state, ppid, pgrp, session, tpgid) from Linux /proc."""
    try:
        raw = pathlib.Path(f'/proc/{pid}/stat').read_text()
        fields = raw[raw.rfind(')') + 2 :].split()
        return (
            fields[0],
            int(fields[1]),
            int(fields[2]),
            int(fields[3]),
            int(fields[5]),
        )
    except (FileNotFoundError, IndexError, ValueError, OSError):
        return None


def process_group_exists(pgid: int) -> bool:
    for entry in pathlib.Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        stat = read_process_stat(int(entry.name))
        if stat is not None and stat[2] == pgid and stat[0] != 'Z':
            return True
    return False


def wait_for_foreground_release(
    shell_pid: int,
    foreground_pgid: int,
    timeout: float,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        stat = read_process_stat(shell_pid)
        if stat is None or stat[4] != foreground_pgid:
            return True
        time.sleep(0.025)
    stat = read_process_stat(shell_pid)
    return stat is None or stat[4] != foreground_pgid


def wait_for_prompt(get_pane_content: Callable[[], str], timeout: float) -> str | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        content = get_pane_content()
        if content.rstrip().endswith(CMD_OUTPUT_PS1_END.rstrip()):
            return content
        time.sleep(0.025)
    return None


def terminate_foreground_process_group(
    pane,
    process_signal_grace_seconds: float,
    process_kill_wait_seconds: float,
    get_pane_content: Callable[[], str],
) -> int:
    """Kill only the command currently in the pane foreground."""
    shell_pid = pane_shell_pid(pane)
    if shell_pid is None:
        raise RuntimeError('cannot determine tmux pane shell PID')

    stat = read_process_stat(shell_pid)
    if stat is None:
        raise RuntimeError(f'tmux pane shell {shell_pid} disappeared')
    shell_pgid = stat[2]
    foreground_pgid = stat[4]
    if foreground_pgid <= 1:
        raise RuntimeError(
            'cannot identify the foreground command process group '
            f'(shell_pid={shell_pid}, shell_pgid={shell_pgid}, '
            f'tpgid={foreground_pgid})'
        )
    if foreground_pgid == shell_pgid:
        pane.send_keys('C-c', enter=False)
        if wait_for_prompt(get_pane_content, process_kill_wait_seconds) is not None:
            return foreground_pgid
        raise RuntimeError(
            f'shell builtin in process group {shell_pgid} ignored Ctrl-C'
        )

    released = False
    for sig, grace in (
        (signal.SIGINT, process_signal_grace_seconds),
        (signal.SIGTERM, process_signal_grace_seconds),
        (signal.SIGKILL, process_kill_wait_seconds),
    ):
        if not process_group_exists(foreground_pgid):
            released = True
            break
        try:
            os.killpg(foreground_pgid, sig)
        except ProcessLookupError:
            released = True
            break
        released = wait_for_foreground_release(shell_pid, foreground_pgid, grace)
        if released:
            break

    if not released or process_group_exists(foreground_pgid):
        raise RuntimeError(
            f'foreground process group {foreground_pgid} survived SIGKILL'
        )
    return foreground_pgid


def session_process_ids(process_session_id: int) -> list[int]:
    pids: list[int] = []
    for entry in pathlib.Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        stat = read_process_stat(pid)
        if stat is not None and stat[3] == process_session_id and stat[0] != 'Z':
            pids.append(pid)
    return pids


def signal_process_session(process_session_id: int, sig: int) -> None:
    for pid in session_process_ids(process_session_id):
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            continue


def wait_for_process_session_exit(
    process_session_id: int,
    timeout: float,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not session_process_ids(process_session_id):
            return True
        time.sleep(0.025)
    return not session_process_ids(process_session_id)
