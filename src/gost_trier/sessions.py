from __future__ import annotations

import json
import os
import platform
import signal
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .native import DEFAULT_CACHE_ROOT


SESSION_DIR = DEFAULT_CACHE_ROOT / "sessions"


@dataclass(frozen=True)
class ManagedProcess:
    pid: int
    command: list[str]
    listens: list[str]
    started_at: float


def run_managed_session(
    session: str,
    processes: Sequence[tuple[list[str], list[str]]],
) -> None:
    cleanup_managed_session(session)
    launched: list[ManagedProcess] = []
    for command, listens in processes:
        print(f"tmux unavailable; launching detached process: {' '.join(command)}", file=sys.stderr)
        proc = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=platform.system().lower() != "windows",
            creationflags=windows_detached_flags(),
        )
        launched.append(ManagedProcess(pid=proc.pid, command=command, listens=listens, started_at=time.time()))
    write_session(session, launched)


def cleanup_managed_session(session: str) -> None:
    payload = read_session(session)
    if not payload:
        return
    for item in payload.get("processes", []):
        pid = item.get("pid")
        command = item.get("command", [])
        if isinstance(pid, int) and process_matches(pid, command):
            terminate_process(pid)
    session_file(session).unlink(missing_ok=True)


def read_session(session: str) -> dict[str, Any] | None:
    path = session_file(session)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None


def write_session(session: str, processes: Sequence[ManagedProcess]) -> None:
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "session": session,
        "processes": [
            {
                "pid": process.pid,
                "command": process.command,
                "listens": process.listens,
                "started_at": process.started_at,
            }
            for process in processes
        ],
    }
    session_file(session).write_text(json.dumps(payload, indent=2))


def session_file(session: str) -> Path:
    safe = "".join(char if char.isalnum() or char in "._-" else "_" for char in session)
    return SESSION_DIR / f"{safe}.json"


def windows_detached_flags() -> int:
    if platform.system().lower() != "windows":
        return 0
    return subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]


def process_matches(pid: int, command: Sequence[str]) -> bool:
    if not process_exists(pid):
        return False
    if platform.system().lower() != "linux":
        return True
    cmdline = Path(f"/proc/{pid}/cmdline")
    try:
        text = cmdline.read_bytes().replace(b"\x00", b" ").decode("utf-8", "replace")
    except OSError:
        return False
    executable = Path(command[0]).name if command else ""
    names = {executable}
    if executable == "xray-run":
        names.add("xray")
    return any(name and name in text for name in names)


def process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def terminate_process(pid: int) -> None:
    system = platform.system().lower()
    try:
        if system == "windows":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        try:
            os.killpg(pid, signal.SIGTERM)
        except OSError:
            os.kill(pid, signal.SIGTERM)
        time.sleep(0.5)
        if process_exists(pid):
            try:
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                os.kill(pid, signal.SIGKILL)
    except OSError:
        return
