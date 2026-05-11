from __future__ import annotations

import argparse
import base64
import binascii
import itertools
import json
import random
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import httpx


DEFAULT_TEST_URLS = ["https://api.ipify.org", "https://myip.wtf/json"]


@dataclass(frozen=True)
class TrierOptions:
    files: list[str]
    runner_args: list[str]
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


def parse_trier_args(
    argv: Sequence[str],
    *,
    prog: str,
    description: str,
    default_jobs: int = 1,
) -> TrierOptions:
    parser = argparse.ArgumentParser(prog=prog, description=description)
    parser.add_argument("files", nargs="+")
    parser.add_argument("--test-url", action="append", dest="test_urls")
    parser.add_argument("--shuffle", action="store_true")
    parser.add_argument("--timeout", type=parse_duration, default=20.0)
    parser.add_argument("--jobs", type=int, default=default_jobs)
    parser.add_argument("--run-in-tmux")
    parser.add_argument("--run-top", type=int, default=1)

    if "--" not in argv:
        parser.parse_args(argv)
        parser.error("runner args are required after --")

    separator = list(argv).index("--")
    app_argv = list(argv[:separator])
    runner_args = list(argv[separator + 1 :])
    namespace = parser.parse_args(app_argv)

    if not runner_args:
        parser.error("runner args are required after --")
    if namespace.jobs < 1:
        parser.error("--jobs must be >= 1")
    if namespace.run_top < 1:
        parser.error("--run-top must be >= 1")

    return TrierOptions(
        files=namespace.files,
        runner_args=runner_args,
        test_urls=namespace.test_urls or list(DEFAULT_TEST_URLS),
        shuffle=namespace.shuffle,
        timeout=namespace.timeout,
        jobs=namespace.jobs,
        run_in_tmux=namespace.run_in_tmux,
        run_top=namespace.run_top,
    )


def read_candidate_files(paths: Sequence[str], shuffle: bool) -> list[list[str]]:
    all_lines: list[list[str]] = []
    for path in paths:
        lines = candidate_lines(path)
        if shuffle:
            random.shuffle(lines)
        all_lines.append(lines)
    return all_lines


def candidate_lines(source: str) -> list[str]:
    text = read_candidate_text(source)
    return [line for line in (raw.strip() for raw in text.splitlines()) if line and not line.startswith("#")]


def read_candidate_text(source: str) -> str:
    raw = download_source(source) if is_http_url(source) else Path(source).read_bytes()
    decoded = decode_base64_if_needed(raw)
    return decoded.decode("utf-8")


def is_http_url(source: str) -> bool:
    parsed = urlparse(source)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def download_source(url: str) -> bytes:
    request = Request(url, headers={"User-Agent": "gost-trier/0.1.0"})
    with urlopen(request, timeout=30) as response:
        return response.read()


def decode_base64_if_needed(raw: bytes) -> bytes:
    stripped = b"".join(raw.split())
    if not stripped:
        return raw
    padded = stripped + b"=" * (-len(stripped) % 4)
    try:
        decoded = base64.b64decode(padded, validate=True)
        text = decoded.decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return raw
    if b"\x00" in decoded:
        return raw
    if "://" not in text and "\n" not in text:
        return raw
    return decoded


def expand_configs(
    runner_args: Sequence[str],
    candidates: Sequence[Sequence[str]],
    substitute: Callable[[Sequence[str], Sequence[str]], list[str]],
) -> Iterable[list[str]]:
    for values in itertools.product(*candidates):
        yield substitute(runner_args, values)


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
    if shutil.which("curl"):
        return test_url_with_curl(url, port, timeout)
    return test_url_with_httpx(url, port, timeout)


def test_url_with_curl(url: str, port: int, timeout: float) -> dict[str, Any]:
    proxy_url = f"socks5h://127.0.0.1:{port}"
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            [
                "curl",
                "--silent",
                "--show-error",
                "--location",
                "--max-time",
                str(timeout),
                "--output",
                "/dev/null",
                "--write-out",
                "%{http_code} %{size_download}",
                "--proxy",
                proxy_url,
                url,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout + 2,
        )
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        parts = completed.stdout.strip().split()
        http_code = int(parts[0]) if parts and parts[0].isdigit() else None
        size = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        ok = completed.returncode == 0 and http_code is not None and http_code < 500
        result: dict[str, Any] = {
            "url": url,
            "delay-ms": elapsed_ms,
            "result": "ok" if ok else "error",
            "result-http-code": http_code,
            "bytes": size,
        }
        if completed.returncode != 0:
            result["error"] = completed.stderr.strip()
        return result
    except Exception as exc:
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        return {
            "url": url,
            "delay-ms": elapsed_ms,
            "result": "error",
            "result-http-code": None,
            "error": f"{type(exc).__name__}: {exc}",
        }


def test_url_with_httpx(url: str, port: int, timeout: float) -> dict[str, Any]:
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


TestRunner = Callable[[Sequence[str], Sequence[str], float], dict[str, Any] | None]
TmuxRunner = Callable[[str, Sequence[dict[str, Any]], int], None]


def run_trier(
    options: TrierOptions,
    *,
    substitute: Callable[[Sequence[str], Sequence[str]], list[str]],
    run_test: TestRunner,
    run_tmux: TmuxRunner,
) -> int:
    candidates = read_candidate_files(options.files, options.shuffle)
    total = 1
    for lines in candidates:
        total *= len(lines)
    print(f"Testing {total} config(s) with jobs={options.jobs}", file=sys.stderr)

    results: list[dict[str, Any]] = []
    configs = expand_configs(options.runner_args, candidates, substitute)
    if options.jobs != 1:
        with ThreadPoolExecutor(max_workers=options.jobs) as executor:
            future_to_index = {
                executor.submit(run_test, config, options.test_urls, options.timeout): index
                for index, config in enumerate(configs, start=1)
            }
            for future in as_completed(future_to_index):
                index = future_to_index[future]
                print(f"[{index}/{total}] tested", file=sys.stderr)
                try:
                    result = future.result()
                except Exception as exc:
                    print(f"[{index}/{total}] skipped: {type(exc).__name__}: {exc}", file=sys.stderr)
                    continue
                if result is not None:
                    results.append(result)
    else:
        for index, config in enumerate(configs, start=1):
            print(f"[{index}/{total}] testing", file=sys.stderr)
            try:
                result = run_test(config, options.test_urls, options.timeout)
            except Exception as exc:
                print(f"[{index}/{total}] skipped: {type(exc).__name__}: {exc}", file=sys.stderr)
                continue
            if result is not None:
                results.append(result)

    results.sort(key=lambda item: item["best-delay-ms"])
    if options.run_in_tmux:
        run_tmux(options.run_in_tmux, results, options.run_top)
    json.dump(results, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0
