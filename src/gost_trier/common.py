from __future__ import annotations

import argparse
import base64
import binascii
import itertools
import json
import math
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

import httpx

from .downloads import download_bytes, set_download_progress_enabled


DEFAULT_TEST_URLS = ["https://api.ipify.org", "https://myip.wtf/json"]
TRIER_EPILOG = """examples:
  %(prog)s trojan.txt -- -F=MAGIC_FILE_1
  %(prog)s --shuffle --timeout=5s --jobs=20 trojan.txt -- -L=socks5://127.0.0.1:1050 -F=MAGIC_FILE_1
  %(prog)s --sample=100 --enough-delay-ms=800 https://example.com/sub.txt -- -F=MAGIC_FILE_1
  %(prog)s https://example.com/sub.txt -- -F=MAGIC_FILE_1

candidate sources:
  FILE arguments may be local paths or http(s) URLs.
  Plain text and base64 subscription text are supported.
  Blank lines and lines starting with # are ignored.
"""


@dataclass(frozen=True)
class TrierOptions:
    files: list[str]
    runner_args: list[str]
    test_urls: list[str]
    shuffle: bool
    timeout: float
    jobs: int
    enough_delay_ms: float | None
    sample: int | None
    run_in_tmux: str | None
    run_top: int
    top_n: int
    test_n: int
    loss_std_weight: float
    min_success_rate: float
    balancer_strategy: str | None
    verbose: int
    output: str
    progress: bool


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
    balancer_strategies: Sequence[str] | None = None,
) -> TrierOptions:
    parser = argparse.ArgumentParser(
        prog=prog,
        description=description,
        epilog=TRIER_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("files", nargs="+", metavar="FILE_OR_URL", help="candidate list source for MAGIC_FILE_N")
    parser.add_argument("--test-url", action="append", dest="test_urls", help="URL to test through the proxy; repeatable")
    parser.add_argument("--shuffle", action="store_true", help="shuffle each candidate source before testing")
    parser.add_argument("--timeout", type=parse_duration, default=20.0, help="per-URL timeout, e.g. 500ms, 5s, 1m")
    parser.add_argument("--jobs", type=int, default=default_jobs, help=f"parallel configs to test (default: {default_jobs})")
    parser.add_argument("--enough-delay-ms", type=float, help="stop submitting new configs after finding this latency or lower")
    parser.add_argument("--sample", type=int, metavar="N", help="randomly sample N expanded configs; implies --shuffle")
    parser.add_argument("--run-in-tmux", metavar="SESSION", help="launch the fastest working configs in this tmux session")
    parser.add_argument("--run-top", type=int, default=5, help="number of working configs to launch with --run-in-tmux")
    parser.add_argument("--top-n", type=int, default=20, help="number of fastest initial successes to confirm")
    parser.add_argument("--test-n", type=int, default=10, help="total confirmation trials per top config")
    parser.add_argument("--loss-std-weight", type=float, default=0.2, help="weight for latency stddev in confirmation loss")
    parser.add_argument("--min-success-rate", type=float, default=0.7, help="soft minimum success rate for selection")
    if balancer_strategies is not None:
        parser.add_argument(
            "--balancer-strategy",
            choices=list(balancer_strategies),
            default="leastLoad",
            help="Xray balancer strategy for --run-in-tmux --run-top > 1",
        )
    parser.add_argument("-v", "--verbose", action="count", default=0, help="increase diagnostic output; repeat for more detail")
    parser.add_argument("-o", "--output", default="-", help="write final JSON to this file, or - for stdout")
    parser.add_argument("--progress", dest="progress", action="store_true", help="show download progress bars")
    parser.add_argument("--no-progress", dest="progress", action="store_false", help="hide download progress bars")
    parser.set_defaults(progress=True)

    if "--" not in argv:
        parser.parse_args(argv)
        parser.error("runner args are required after --")

    separator = list(argv).index("--")
    app_argv = list(argv[:separator])
    runner_args = normalize_split_url_args(argv[separator + 1 :])
    namespace = parser.parse_args(app_argv)

    if not runner_args:
        parser.error("runner args are required after --")
    if namespace.jobs < 1:
        parser.error("--jobs must be >= 1")
    if namespace.enough_delay_ms is not None and namespace.enough_delay_ms < 0:
        parser.error("--enough-delay-ms must be >= 0")
    if namespace.sample is not None and namespace.sample < 1:
        parser.error("--sample must be >= 1")
    if namespace.run_top < 1:
        parser.error("--run-top must be >= 1")
    if namespace.top_n < 1:
        parser.error("--top-n must be >= 1")
    if namespace.test_n < 1:
        parser.error("--test-n must be >= 1")
    if namespace.loss_std_weight < 0:
        parser.error("--loss-std-weight must be >= 0")
    if not 0 < namespace.min_success_rate <= 1:
        parser.error("--min-success-rate must be > 0 and <= 1")

    return TrierOptions(
        files=namespace.files,
        runner_args=runner_args,
        test_urls=namespace.test_urls or list(DEFAULT_TEST_URLS),
        shuffle=namespace.shuffle or namespace.sample is not None,
        timeout=namespace.timeout,
        jobs=namespace.jobs,
        enough_delay_ms=namespace.enough_delay_ms,
        sample=namespace.sample,
        run_in_tmux=namespace.run_in_tmux,
        run_top=namespace.run_top,
        top_n=namespace.top_n,
        test_n=namespace.test_n,
        loss_std_weight=namespace.loss_std_weight,
        min_success_rate=namespace.min_success_rate,
        balancer_strategy=getattr(namespace, "balancer_strategy", None),
        verbose=namespace.verbose,
        output=namespace.output,
        progress=namespace.progress,
    )


def normalize_split_url_args(args: Sequence[str]) -> list[str]:
    normalized: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if index + 1 < len(args) and arg.endswith(":") and args[index + 1].startswith("//"):
            normalized.append(arg + args[index + 1])
            index += 2
            continue
        normalized.append(arg)
        index += 1
    return normalized


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
    return download_bytes(url, timeout=30)


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


def sample_iterable(items: Iterable[list[str]], sample_size: int) -> list[list[str]]:
    sample: list[list[str]] = []
    for index, item in enumerate(items, start=1):
        if index <= sample_size:
            sample.append(item)
            continue
        replacement = random.randrange(index)
        if replacement < sample_size:
            sample[replacement] = item
    random.shuffle(sample)
    return sample


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


TestRunner = Callable[[Sequence[str], Sequence[str], float, int], dict[str, Any] | None]
TmuxRunner = Callable[[str, Sequence[dict[str, Any]], TrierOptions], None]
PreflightRunner = Callable[[TrierOptions], None]


def run_trier(
    options: TrierOptions,
    *,
    substitute: Callable[[Sequence[str], Sequence[str]], list[str]],
    run_test: TestRunner,
    run_tmux: TmuxRunner,
    preflight: PreflightRunner | None = None,
) -> int:
    set_download_progress_enabled(options.progress)
    candidates = read_candidate_files(options.files, options.shuffle)
    total = 1
    for lines in candidates:
        total *= len(lines)

    results: list[dict[str, Any]] = []
    configs = expand_configs(options.runner_args, candidates, substitute)
    if options.sample is not None:
        configs = sample_iterable(configs, options.sample)
        sampled_total = len(configs)
        print(f"Testing {sampled_total} sampled config(s) from {total} total with jobs={options.jobs}", file=sys.stderr)
        total = sampled_total
    else:
        print(f"Testing {total} config(s) with jobs={options.jobs}", file=sys.stderr)

    if preflight is not None:
        preflight(options)

    if options.jobs != 1:
        results.extend(run_parallel_tests(configs, total, options, run_test))
    else:
        for index, config in enumerate(configs, start=1):
            print(f"[{index}/{total}] testing", file=sys.stderr)
            try:
                result = run_test(config, options.test_urls, options.timeout, options.verbose)
            except Exception as exc:
                print(f"[{index}/{total}] skipped: {type(exc).__name__}: {exc}", file=sys.stderr)
                continue
            print(progress_message(index, total, result), file=sys.stderr)
            if result is not None:
                results.append(result)
                if is_enough_result(result, options.enough_delay_ms):
                    print(f"[{index}/{total}] enough: {result['best-delay-ms']} ms", file=sys.stderr)
                    break

    results.sort(key=lambda item: item["best-delay-ms"])
    results = confirm_top_results(results, options, run_test)
    if options.run_in_tmux:
        run_tmux(options.run_in_tmux, results, options)
    write_json_output(results, options.output)
    return 0


def write_json_output(value: Any, output: str) -> None:
    if output == "-":
        json.dump(value, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def run_parallel_tests(
    configs: Iterable[list[str]],
    total: int,
    options: TrierOptions,
    run_test: TestRunner,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    indexed_configs = enumerate(configs, start=1)
    executor = ThreadPoolExecutor(max_workers=options.jobs)
    future_to_index = {}
    stop = False
    try:
        for _ in range(options.jobs):
            try:
                index, config = next(indexed_configs)
            except StopIteration:
                break
            future_to_index[executor.submit(run_test, config, options.test_urls, options.timeout, options.verbose)] = index

        while future_to_index:
            for future in as_completed(future_to_index):
                index = future_to_index.pop(future)
                try:
                    result = future.result()
                except Exception as exc:
                    print(f"[{index}/{total}] skipped: {type(exc).__name__}: {exc}", file=sys.stderr)
                    result = None
                else:
                    print(progress_message(index, total, result), file=sys.stderr)
                if result is not None:
                    results.append(result)
                    if is_enough_result(result, options.enough_delay_ms):
                        print(f"[{index}/{total}] enough: {result['best-delay-ms']} ms", file=sys.stderr)
                        stop = True
                if stop:
                    break
                try:
                    next_index, next_config = next(indexed_configs)
                except StopIteration:
                    continue
                future_to_index[executor.submit(run_test, next_config, options.test_urls, options.timeout, options.verbose)] = next_index
            if stop:
                for pending in future_to_index:
                    pending.cancel()
                break
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
    return results


def confirm_top_results(
    results: Sequence[dict[str, Any]],
    options: TrierOptions,
    run_test: TestRunner,
) -> list[dict[str, Any]]:
    if not results:
        return []
    confirmed_count = min(options.top_n, len(results))
    top_results = [dict(item) for item in results[:confirmed_count]]
    remaining = [dict(item) for item in results[confirmed_count:]]
    print(
        f"Confirming top {confirmed_count} config(s) with test-n={options.test_n}, "
        f"loss-std-weight={options.loss_std_weight}, min-success-rate={options.min_success_rate}",
        file=sys.stderr,
    )

    trials_by_index: dict[int, list[dict[str, Any] | None]] = {index: [result] for index, result in enumerate(top_results)}
    repeat_jobs: list[tuple[int, list[str], int]] = []
    for index, result in enumerate(top_results):
        config = list(result["config"])
        for trial in range(2, options.test_n + 1):
            repeat_jobs.append((index, config, trial))

    if options.jobs == 1:
        for index, config, trial in repeat_jobs:
            trials_by_index[index].append(run_confirmation_trial(index, confirmed_count, trial, options, config, run_test))
    else:
        executor = ThreadPoolExecutor(max_workers=options.jobs)
        future_to_job = {
            executor.submit(run_test, config, options.test_urls, options.timeout, options.verbose): (index, trial)
            for index, config, trial in repeat_jobs
        }
        try:
            for future in as_completed(future_to_job):
                index, trial = future_to_job[future]
                try:
                    result = future.result()
                except Exception as exc:
                    print(
                        f"[confirm {index + 1}/{confirmed_count} trial {trial}/{options.test_n}] "
                        f"skipped: {type(exc).__name__}: {exc}",
                        file=sys.stderr,
                    )
                    result = None
                print(confirmation_progress_message(index, confirmed_count, trial, options.test_n, result), file=sys.stderr)
                trials_by_index[index].append(result)
        finally:
            executor.shutdown(wait=True, cancel_futures=True)

    for index, result in enumerate(top_results):
        summarize_confirmation(result, trials_by_index[index], options)

    apply_min_success_rate_penalty(top_results, options)
    top_results.sort(key=confirmed_sort_key)
    for item in remaining:
        item["confirmed"] = False
    return [*top_results, *remaining]


def run_confirmation_trial(
    index: int,
    total: int,
    trial: int,
    options: TrierOptions,
    config: Sequence[str],
    run_test: TestRunner,
) -> dict[str, Any] | None:
    try:
        result = run_test(config, options.test_urls, options.timeout, options.verbose)
    except Exception as exc:
        print(f"[confirm {index + 1}/{total} trial {trial}/{options.test_n}] skipped: {type(exc).__name__}: {exc}", file=sys.stderr)
        result = None
    print(confirmation_progress_message(index, total, trial, options.test_n, result), file=sys.stderr)
    return result


def confirmation_progress_message(index: int, total: int, trial: int, test_n: int, result: dict[str, Any] | None) -> str:
    prefix = f"[confirm {index + 1}/{total} trial {trial}/{test_n}]"
    if result is None:
        return f"{prefix} fail"
    return f"{prefix} success {result['best-delay-ms']} ms"


def summarize_confirmation(result: dict[str, Any], trials: Sequence[dict[str, Any] | None], options: TrierOptions) -> None:
    successful = [trial for trial in trials if trial is not None]
    delays = [float(trial["best-delay-ms"]) for trial in successful]
    success_count = len(successful)
    test_count = len(trials)
    failure_count = test_count - success_count
    success_rate = success_count / test_count if test_count else 0.0
    avg_delay = sum(delays) / len(delays) if delays else math.inf
    std_delay = population_stddev(delays)
    base_loss = (avg_delay + options.loss_std_weight * std_delay) / success_rate if success_rate > 0 else math.inf

    result["confirmed"] = True
    result["confirmation-results"] = [confirmation_result_summary(trial) for trial in trials]
    result["test-count"] = test_count
    result["success-count"] = success_count
    result["failure-count"] = failure_count
    result["success-rate"] = round(success_rate, 3)
    result["avg-delay-ms"] = round(avg_delay, 3) if math.isfinite(avg_delay) else None
    result["std-delay-ms"] = round(std_delay, 3) if math.isfinite(std_delay) else None
    result["base-loss"] = round(base_loss, 3) if math.isfinite(base_loss) else None
    result["loss"] = result["base-loss"]
    result["loss-std-weight"] = options.loss_std_weight
    result["min-success-rate"] = options.min_success_rate
    if delays:
        result["best-delay-ms"] = round(min(delays))


def confirmation_result_summary(trial: dict[str, Any] | None) -> dict[str, Any]:
    if trial is None:
        return {"result": "error"}
    return {
        "result": "ok",
        "best-delay-ms": trial["best-delay-ms"],
        "tests": trial.get("tests", []),
    }


def population_stddev(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    return math.sqrt(variance)


def apply_min_success_rate_penalty(results: Sequence[dict[str, Any]], options: TrierOptions) -> None:
    usable = [item for item in results if item.get("success-count", 0) > 0]
    if not usable:
        return
    if not any(float(item.get("success-rate", 0)) >= options.min_success_rate for item in usable):
        return
    for item in usable:
        if float(item.get("success-rate", 0)) >= options.min_success_rate:
            continue
        base_loss = item.get("base-loss")
        if base_loss is None:
            continue
        item["unselected-penalty"] = 1_000_000
        item["loss"] = round(float(base_loss) + 1_000_000, 3)


def confirmed_sort_key(result: dict[str, Any]) -> tuple[float, float, float, float, float]:
    loss = float(result["loss"]) if result.get("loss") is not None else math.inf
    success_rate = float(result.get("success-rate", 0))
    avg_delay = float(result["avg-delay-ms"]) if result.get("avg-delay-ms") is not None else math.inf
    std_delay = float(result["std-delay-ms"]) if result.get("std-delay-ms") is not None else math.inf
    best_delay = float(result.get("best-delay-ms", math.inf))
    return (loss, -success_rate, avg_delay, std_delay, best_delay)


def is_enough_result(result: dict[str, Any], enough_delay_ms: float | None) -> bool:
    return enough_delay_ms is not None and result["best-delay-ms"] <= enough_delay_ms


def progress_message(index: int, total: int, result: dict[str, Any] | None) -> str:
    if result is None:
        return f"[{index}/{total}] fail"
    return f"[{index}/{total}] success {result['best-delay-ms']} ms"
