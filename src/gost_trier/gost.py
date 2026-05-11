from __future__ import annotations

import platform
import os
import shlex
import shutil
import subprocess
import sys
from collections.abc import Sequence
from typing import Any
from urllib.parse import quote, unquote, urlparse

from .common import free_port, is_successful_test, test_url, wait_for_port
from .sessions import run_managed_session


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


def listen_args(args: Sequence[str]) -> list[str]:
    listens: list[str] = []
    skip_next = False
    for arg in args:
        if skip_next:
            listens.append(arg)
            skip_next = False
            continue
        if arg == "-L":
            skip_next = True
        elif arg.startswith("-L="):
            listens.append(arg.split("=", 1)[1])
    return listens


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
    ensure_tmux_available()
    check = subprocess.run(["tmux", "has-session", "-t", session], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if check.returncode != 0:
        subprocess.run(["tmux", "new-session", "-d", "-s", session, "sleep", "infinity"], check=True)


def ensure_tmux_available() -> None:
    if shutil.which("tmux"):
        return
    install_tmux()
    if not shutil.which("tmux"):
        raise RuntimeError("tmux was not found after automatic installation attempt")


def install_tmux() -> None:
    if shutil.which("apt-get"):
        print("tmux not found; installing with apt-get", file=sys.stderr)
        run_install_command(["apt-get", "update"])
        run_install_command(["apt-get", "install", "-y", "tmux"])
        return
    for command in tmux_install_commands():
        if not shutil.which(command[0]):
            continue
        print(f"tmux not found; installing with: {' '.join(command)}", file=sys.stderr)
        run_install_command(command)
        return
    raise RuntimeError("tmux is not installed and no supported package manager was found")


def run_install_command(command: list[str]) -> None:
    if platform.system().lower() != "windows" and hasattr(os, "geteuid") and os.geteuid() != 0 and shutil.which("sudo"):
        command = ["sudo", *command]
    subprocess.run(command, check=True)


def tmux_install_commands() -> list[list[str]]:
    system = platform.system().lower()
    if system == "darwin":
        return [["brew", "install", "tmux"]]
    if system == "windows":
        return [
            ["scoop", "install", "tmux"],
            ["choco", "install", "tmux", "-y"],
            ["winget", "install", "-e", "--id", "tmux.tmux", "--accept-package-agreements", "--accept-source-agreements"],
        ]
    return [
        ["dnf", "install", "-y", "tmux"],
        ["yum", "install", "-y", "tmux"],
        ["pacman", "-Sy", "--noconfirm", "tmux"],
        ["zypper", "--non-interactive", "install", "tmux"],
        ["apk", "add", "tmux"],
        ["brew", "install", "tmux"],
    ]


def run_gost_in_tmux(session: str, results: Sequence[dict[str, Any]], run_top: int) -> None:
    if not results:
        print("No working configs found; skipping tmux launch", file=sys.stderr)
        return
    launched: list[list[str]] = []
    for index, result in enumerate(results[:run_top], start=1):
        config = list(result["config"])
        if not has_listen_args(config):
            config = [f"-L=socks5://127.0.0.1:{free_port()}", *config]
        launched.append(config)

    try:
        ensure_tmux_session(session)
    except RuntimeError as exc:
        print(f"tmux unavailable: {exc}", file=sys.stderr)
        processes = [(["gost", *config], listen_args(config)) for config in launched]
        run_managed_session(session, processes)
        print_tmux_launch_info(session, launched, command_name="gost", managed=True)
        return

    for index, config in enumerate(launched, start=1):
        subprocess.run(tmux_command(session, f"gost-{index}", config), check=True)
    print_tmux_launch_info(session, launched, command_name="gost")


def print_tmux_launch_info(session: str, configs: Sequence[Sequence[str]], *, command_name: str, managed: bool = False) -> None:
    print(f"tmux session: {session}", file=sys.stderr)
    if managed:
        print("runner: managed detached processes (tmux unavailable)", file=sys.stderr)
    else:
        print(f"attach: tmux attach -t {shlex.quote(session)}", file=sys.stderr)
    for config in configs:
        for listen in listen_args(config):
            print(f"test {command_name}: {listener_curl_command(listen)}", file=sys.stderr)


def listener_curl_command(listen: str, url: str = "https://api.ipify.org") -> str:
    parsed = urlparse(listen)
    scheme = "http" if parsed.scheme == "http" else "socks5h"
    host = parsed.hostname or "127.0.0.1"
    if host == "0.0.0.0":
        host = "127.0.0.1"
    auth = ""
    if parsed.username is not None:
        auth = quote(unquote(parsed.username), safe="") + ":" + quote(unquote(parsed.password or ""), safe="") + "@"
    proxy = f"{scheme}://{auth}{host}:{parsed.port}"
    curl = "curl.exe" if platform.system().lower() == "windows" else "curl"
    return " ".join(shlex.quote(part) for part in [curl, "--proxy", proxy, url])
