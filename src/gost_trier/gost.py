from __future__ import annotations

import shlex
import subprocess
import sys
from collections.abc import Sequence
from typing import Any

from .common import free_port, is_successful_test, test_url, wait_for_port


def strip_listen_args(args: Sequence[str]) -> list[str]:
    stripped: list[str] = []
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg == "-L":
            skip_next = True
            continue
        if arg.startswith("-L="):
            continue
        stripped.append(arg)
    return stripped


def has_listen_args(args: Sequence[str]) -> bool:
    return any(arg == "-L" or arg.startswith("-L=") for arg in args)


def run_gost_test(config: Sequence[str], test_urls: Sequence[str], timeout: float) -> dict[str, Any] | None:
    port = free_port()
    test_args = [f"-L=socks5://127.0.0.1:{port}", *strip_listen_args(config)]
    proc = subprocess.Popen(
        ["gost", *test_args],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        if proc.poll() is not None:
            return None
        if not wait_for_port(port, min(timeout, 5.0)):
            return None

        tests: list[dict[str, Any]] = []
        for url in test_urls:
            result = test_url(url, port, timeout)
            tests.append(result)
            if is_successful_test(result):
                break
        successful_delays = [item["delay-ms"] for item in tests if is_successful_test(item)]
        if not successful_delays:
            return None
        return {"best-delay-ms": min(successful_delays), "config": list(config), "tests": tests}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)


def tmux_command(session: str, window: str, config: Sequence[str]) -> list[str]:
    command = " ".join(shlex.quote(part) for part in ["gost", *config])
    return ["tmux", "new-window", "-t", session, "-n", window, command]


def ensure_tmux_session(session: str) -> None:
    check = subprocess.run(["tmux", "has-session", "-t", session], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if check.returncode != 0:
        subprocess.run(["tmux", "new-session", "-d", "-s", session, "sleep", "infinity"], check=True)


def run_gost_in_tmux(session: str, results: Sequence[dict[str, Any]], run_top: int) -> None:
    if not results:
        print("No working configs found; skipping tmux launch", file=sys.stderr)
        return
    ensure_tmux_session(session)
    for index, result in enumerate(results[:run_top], start=1):
        config = list(result["config"])
        if not has_listen_args(config):
            config = [f"-L=socks5://127.0.0.1:{free_port()}", *config]
        subprocess.run(tmux_command(session, f"gost-{index}", config), check=True)
