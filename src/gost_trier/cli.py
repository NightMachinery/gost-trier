from __future__ import annotations

import argparse
import itertools
import json
import random
import re
import shlex
import socket
import subprocess
import sys
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx


DEFAULT_TEST_URL = "https://myip.wtf/json"
PLACEHOLDER_RE = re.compile(r"MAGIC_FILE_(\d+)")


@dataclass(frozen=True)
class CliOptions:
    files: list[Path]
    gost_args: list[str]
    test_urls: list[str]
    shuffle: bool
    timeout: float
    jobs: int
    run_in_tmux: str | None
    run_top: int


def parse_duration(value: str) -> float:
    text = value.strip().lower()
    if not text:
        raise argparse.ArgumentTypeError("duration cannot be empty")
    try:
        if text.endswith("ms"):
            return float(text[:-2]) / 1000.0
        if text.endswith("s"):
            return float(text[:-1])
        if text.endswith("m"):
            return float(text[:-1]) * 60.0
        return float(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid duration: {value}") from exc


def parse_args(argv: Sequence[str]) -> CliOptions:
    parser = argparse.ArgumentParser(
        prog="gost-trier",
        description="Try gost configs by replacing MAGIC_FILE_N placeholders from text files.",
    )
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("--test-url", action="append", dest="test_urls")
    parser.add_argument("--shuffle", action="store_true")
    parser.add_argument("--timeout", type=parse_duration, default=20.0)
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--run-in-tmux")
    parser.add_argument("--run-top", type=int, default=1)

    if "--" not in argv:
        parser.parse_args(argv)
        parser.error("gost args are required after --")

    separator = list(argv).index("--")
    app_argv = list(argv[:separator])
    gost_args = list(argv[separator + 1 :])
    namespace = parser.parse_args(app_argv)

    if not gost_args:
        parser.error("gost args are required after --")
    if namespace.jobs < 1:
        parser.error("--jobs must be >= 1")
    if namespace.run_top < 1:
        parser.error("--run-top must be >= 1")

    return CliOptions(
        files=namespace.files,
        gost_args=gost_args,
        test_urls=namespace.test_urls or [DEFAULT_TEST_URL],
        shuffle=namespace.shuffle,
        timeout=namespace.timeout,
        jobs=namespace.jobs,
        run_in_tmux=namespace.run_in_tmux,
        run_top=namespace.run_top,
    )


def read_candidate_files(paths: Sequence[Path], shuffle: bool) -> list[list[str]]:
    all_lines: list[list[str]] = []
    for path in paths:
        lines = [line.strip() for line in path.read_text().splitlines() if line.strip()]
        if shuffle:
            random.shuffle(lines)
        all_lines.append(lines)
    return all_lines


def expand_configs(gost_args: Sequence[str], candidates: Sequence[Sequence[str]]) -> Iterable[list[str]]:
    for values in itertools.product(*candidates):
        yield substitute_placeholders(gost_args, values)


def substitute_placeholders(gost_args: Sequence[str], values: Sequence[str]) -> list[str]:
    def replace(match: re.Match[str]) -> str:
        index = int(match.group(1)) - 1
        if index < 0 or index >= len(values):
            raise ValueError(f"{match.group(0)} has no matching input file")
        return values[index]

    return [PLACEHOLDER_RE.sub(replace, arg) for arg in gost_args]


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


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_port(port: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.05)
    return False


def test_url(url: str, port: int, timeout: float) -> dict[str, Any]:
    proxy_url = f"socks5h://127.0.0.1:{port}"
    started = time.perf_counter()
    try:
        with httpx.Client(proxy=proxy_url, timeout=timeout, follow_redirects=True) as client:
            response = client.get(url)
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        return {
            "url": url,
            "delay-ms": elapsed_ms,
            "result": "ok" if response.status_code < 500 else "http-error",
            "result-http-code": response.status_code,
            "bytes": len(response.content),
        }
    except Exception as exc:
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        return {
            "url": url,
            "delay-ms": elapsed_ms,
            "result": "error",
            "result-http-code": None,
            "error": f"{type(exc).__name__}: {exc}",
        }


def is_successful_test(test: dict[str, Any]) -> bool:
    return test.get("result") == "ok"


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


def run_in_tmux(session: str, results: Sequence[dict[str, Any]], run_top: int) -> None:
    if not results:
        print("No working configs found; skipping tmux launch", file=sys.stderr)
        return
    ensure_tmux_session(session)
    for index, result in enumerate(results[:run_top], start=1):
        config = list(result["config"])
        if not has_listen_args(config):
            config = [f"-L=socks5://127.0.0.1:{free_port()}", *config]
        subprocess.run(tmux_command(session, f"gost-{index}", config), check=True)


def run(options: CliOptions) -> int:
    candidates = read_candidate_files(options.files, options.shuffle)
    total = 1
    for lines in candidates:
        total *= len(lines)
    print(f"Testing {total} config(s) with jobs={options.jobs}", file=sys.stderr)

    if options.jobs != 1:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        results: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=options.jobs) as executor:
            future_to_index = {
                executor.submit(run_gost_test, config, options.test_urls, options.timeout): index
                for index, config in enumerate(expand_configs(options.gost_args, candidates), start=1)
            }
            for future in as_completed(future_to_index):
                index = future_to_index[future]
                print(f"[{index}/{total}] tested", file=sys.stderr)
                result = future.result()
                if result is not None:
                    results.append(result)
    else:
        results = []
        for index, config in enumerate(expand_configs(options.gost_args, candidates), start=1):
            print(f"[{index}/{total}] testing", file=sys.stderr)
            result = run_gost_test(config, options.test_urls, options.timeout)
            if result is not None:
                results.append(result)

    results.sort(key=lambda item: item["best-delay-ms"])
    if options.run_in_tmux:
        run_in_tmux(options.run_in_tmux, results, options.run_top)
    json.dump(results, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return run(parse_args(sys.argv[1:] if argv is None else argv))
    except BrokenPipeError:
        return 1
    except Exception as exc:
        print(f"gost-trier: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
