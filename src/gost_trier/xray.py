from __future__ import annotations

import argparse
import copy
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any
from urllib.parse import quote, unquote, urlparse

from .common import free_port, is_successful_test, test_url, wait_for_port, write_json_output
from .diagnostics import completed_process_details, dump_json_for_debug, run_logged, verbose_log
from .gost import ensure_tmux_session, strip_listen_args
from .native import locate_xray, locate_xray_link_json
from .sessions import run_managed_session


DEFAULT_CONVERTER_CLONE = Path.home() / ".base" / "Xray-Link-Json"
DEFAULT_CONVERTER_CACHE = Path.home() / ".cache" / "gost-trier" / "Xray-Link-Json"
_CONVERTED_LINK_CACHE: dict[str, list[dict[str, Any]]] = {}
_CONVERTED_LINK_CACHE_LOCK = Lock()
_SMOKE_CHECKED: set[tuple[str, str]] = set()
_SMOKE_CHECK_LOCK = Lock()
CONVERTER_SMOKE_LINK = (
    "vless://00000000-0000-0000-0000-000000000000@example.com:443"
    "?security=tls&type=tcp&sni=example.com#smoke"
)


@dataclass(frozen=True)
class Listen:
    host: str
    port: int
    scheme: str
    username: str | None = None
    password: str | None = None


@dataclass(frozen=True)
class XrayArgs:
    listens: list[Listen]
    forwards: list[str]


def parse_xray_args(args: Sequence[str], *, auto_listen: bool = True) -> XrayArgs:
    listens: list[Listen] = []
    forwards: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "-L":
            index += 1
            if index >= len(args):
                raise ValueError("-L requires a value")
            listens.append(parse_listen(args[index]))
        elif arg.startswith("-L="):
            listens.append(parse_listen(arg.split("=", 1)[1]))
        elif arg == "-F":
            index += 1
            if index >= len(args):
                raise ValueError("-F requires a value")
            forwards.append(args[index])
        elif arg.startswith("-F="):
            forwards.append(arg.split("=", 1)[1])
        else:
            raise ValueError(f"unsupported xray-run argument: {arg}")
        index += 1

    if not listens and auto_listen:
        listens.append(Listen(host="127.0.0.1", port=free_port(), scheme="socks5"))
        print(f"xray-run: auto-selected -L=socks5://127.0.0.1:{listens[0].port}", file=sys.stderr)
    if not forwards:
        forwards.append("direct://")
    return XrayArgs(listens=listens, forwards=forwards)


def parse_listen(value: str) -> Listen:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "socks", "socks5", "socks5h"}:
        raise ValueError(f"unsupported xray listener scheme: {parsed.scheme or value}")
    if parsed.port is None:
        raise ValueError(f"listener must include port: {value}")
    return Listen(
        host=parsed.hostname or "0.0.0.0",
        port=parsed.port,
        scheme=parsed.scheme,
        username=unquote(parsed.username) if parsed.username else None,
        password=unquote(parsed.password) if parsed.password else None,
    )


def locate_converter(verbose: int = 0) -> list[str]:
    env_path = os.environ.get("XRAY_LINK_JSON")
    if env_path:
        command = [env_path]
        verbose_log(verbose, 1, f"using Xray-Link-Json from XRAY_LINK_JSON: {env_path}")
        smoke_test_converter(command, verbose=verbose)
        return command
    path_binary = shutil.which("Xray-Link-Json")
    if path_binary:
        command = [path_binary]
        verbose_log(verbose, 1, f"using Xray-Link-Json from PATH: {path_binary}")
        smoke_test_converter(command, verbose=verbose)
        return command
    try:
        command = [str(locate_xray_link_json())]
        verbose_log(verbose, 1, f"using cached/downloaded Xray-Link-Json: {command[0]}")
        smoke_test_converter(command, verbose=verbose)
        return command
    except Exception as release_exc:
        print(f"xray-run: release install for Xray-Link-Json failed: {release_exc}", file=sys.stderr)
    install_converter()
    path_binary = shutil.which("Xray-Link-Json")
    if path_binary:
        command = [path_binary]
        smoke_test_converter(command, verbose=verbose)
        return command
    if DEFAULT_CONVERTER_CACHE.exists():
        command = [str(DEFAULT_CONVERTER_CACHE)]
        smoke_test_converter(command, verbose=verbose)
        return command
    if (DEFAULT_CONVERTER_CLONE / "go.mod").exists():
        DEFAULT_CONVERTER_CACHE.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["go", "build", "-o", str(DEFAULT_CONVERTER_CACHE), "."],
            cwd=DEFAULT_CONVERTER_CLONE,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
        )
        command = [str(DEFAULT_CONVERTER_CACHE)]
        smoke_test_converter(command, verbose=verbose)
        return command
    raise FileNotFoundError("Xray-Link-Json not found; set XRAY_LINK_JSON or install it on PATH")


def install_converter() -> None:
    if not shutil.which("go"):
        raise FileNotFoundError(
            "Xray-Link-Json was not found and automatic release download failed; install Go or set XRAY_LINK_JSON"
        )
    print("xray-run: installing Xray-Link-Json with go install", file=sys.stderr)
    subprocess.run(
        ["go", "install", "github.com/NightMachinery/Xray-Link-Json@latest"],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
    )


def convert_link_to_outbounds(link: str, converter: Sequence[str] | None = None, *, verbose: int = 0) -> list[dict[str, Any]]:
    if link in {"direct://", "freedom://"}:
        return [{"protocol": "freedom", "settings": {}}]
    if link == "blackhole://":
        return [{"protocol": "blackhole", "settings": {}}]

    if converter is None:
        with _CONVERTED_LINK_CACHE_LOCK:
            cached = _CONVERTED_LINK_CACHE.get(link)
        if cached is not None:
            return copy.deepcopy(cached)

    command = list(converter or locate_converter(verbose=verbose))
    if verbose >= 3:
        verbose_log(verbose, 3, f"converting link: {link}")
    completed = run_logged([*command, link], verbose=verbose)
    if completed.returncode != 0:
        detail = completed_process_details(completed, command=[*command, link]) if verbose else completed.stderr.strip()
        raise ValueError(f"Xray-Link-Json failed to convert one forward\n{detail or '<no stderr>'}")
    payload = parse_converter_json(completed.stdout)
    raw_outbounds = payload.get("outbounds")
    if not isinstance(raw_outbounds, list) or not raw_outbounds:
        raise ValueError("converted Xray config did not contain outbounds")
    outbounds = [normalize_outbound(item) for item in raw_outbounds if isinstance(item, dict)]
    if converter is None:
        with _CONVERTED_LINK_CACHE_LOCK:
            _CONVERTED_LINK_CACHE[link] = copy.deepcopy(outbounds)
    return outbounds


def parse_converter_json(stdout: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    start = stdout.find("{")
    if start < 0:
        raise ValueError("Xray-Link-Json did not emit JSON")
    payload, _ = decoder.raw_decode(stdout[start:])
    if not isinstance(payload, dict):
        raise ValueError("Xray-Link-Json emitted non-object JSON")
    return payload


def normalize_outbound(outbound: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(outbound)
    cleaned.pop("sendThrough", None)
    return cleaned


def build_xray_config(args: XrayArgs, *, converter: Sequence[str] | None = None, verbose: int = 0) -> dict[str, Any]:
    outbounds: list[dict[str, Any]] = []
    if len(args.forwards) == 1:
        converted = [convert_link_to_outbounds(args.forwards[0], converter=converter, verbose=verbose)]
    else:
        with ThreadPoolExecutor(max_workers=len(args.forwards)) as executor:
            converted = list(
                executor.map(lambda forward: convert_link_to_outbounds(forward, converter=converter, verbose=verbose), args.forwards)
            )
    for converted_outbounds in converted:
        outbounds.extend(normalize_outbound(item) for item in converted_outbounds)
    if not outbounds:
        raise ValueError("no Xray outbounds were generated")

    for index, outbound in enumerate(outbounds, start=1):
        outbound["tag"] = f"proxy-{index}"
        if index < len(outbounds):
            outbound["proxySettings"] = {"tag": f"proxy-{index + 1}"}
        else:
            outbound.pop("proxySettings", None)

    config = {
        "log": {"loglevel": "warning"},
        "inbounds": [build_inbound(listen, index) for index, listen in enumerate(args.listens, start=1)],
        "outbounds": outbounds,
        "routing": {
            "rules": [
                {
                    "type": "field",
                    "inboundTag": [inbound_tag(index) for index in range(1, len(args.listens) + 1)],
                    "outboundTag": "proxy-1",
                }
            ]
        },
    }
    if verbose >= 3:
        print(f"xray-run: generated Xray config:\n{dump_json_for_debug(config)}", file=sys.stderr)
    return config


def inbound_tag(index: int) -> str:
    return f"in-{index}"


def build_inbound(listen: Listen, index: int = 1) -> dict[str, Any]:
    protocol = "http" if listen.scheme == "http" else "socks"
    inbound = {
        "tag": inbound_tag(index),
        "listen": listen.host,
        "port": listen.port,
        "protocol": protocol,
        "settings": inbound_settings(listen),
        "sniffing": {"enabled": True, "destOverride": ["http", "tls", "quic"]},
    }
    return inbound


def inbound_settings(listen: Listen) -> dict[str, Any]:
    if listen.scheme == "http":
        settings: dict[str, Any] = {}
        if listen.username is not None:
            settings["accounts"] = [{"user": listen.username, "pass": listen.password or ""}]
        return settings

    settings = {"auth": "noauth", "udp": True}
    if listen.username is not None:
        settings["auth"] = "password"
        settings["accounts"] = [{"user": listen.username, "pass": listen.password or ""}]
    return settings


def validate_xray_config(config: dict[str, Any], *, verbose: int = 0) -> None:
    xray_bin = ensure_xray_dependency(verbose=verbose)
    with tempfile.NamedTemporaryFile("w", suffix=".json", prefix="xray-run-test-", delete=True) as file:
        json.dump(config, file)
        file.flush()
        command = [xray_bin, "run", "-test", "-c", file.name]
        completed = run_logged(command, verbose=verbose)
        if completed.returncode != 0:
            details = completed_process_details(completed, command=command)
            raise ValueError(f"generated Xray config failed validation\n{details}")


def xray_run_json(args: Sequence[str], *, validate: bool = True, verbose: int = 0) -> dict[str, Any]:
    parsed = parse_xray_args(args)
    config = build_xray_config(parsed, verbose=verbose)
    if validate:
        validate_xray_config(config, verbose=verbose)
    return config


def exec_xray(args: Sequence[str], *, verbose: int = 0) -> None:
    xray_bin = ensure_xray_dependency(verbose=verbose)
    parsed = parse_xray_args(args)
    config = build_xray_config(parsed, verbose=verbose)
    validate_xray_config(config, verbose=verbose)
    print_listener_curl_commands(parsed.listens)
    temp = tempfile.NamedTemporaryFile("w", suffix=".json", prefix="xray-run-", delete=False)
    with temp:
        json.dump(config, temp)
    verbose_log(verbose, 1, f"execing Xray with config: {temp.name}")
    os.execv(xray_bin, [xray_bin, "run", "-c", temp.name])


def listener_curl_command(listen: Listen, url: str = "https://api.ipify.org") -> str:
    proxy = listener_proxy_url(listen)
    curl = "curl.exe" if platform.system().lower() == "windows" else "curl"
    return " ".join(shlex.quote(part) for part in [curl, "--proxy", proxy, url])


def listener_proxy_url(listen: Listen) -> str:
    scheme = "http" if listen.scheme == "http" else "socks5h"
    host = "127.0.0.1" if listen.host == "0.0.0.0" else listen.host
    auth = ""
    if listen.username is not None:
        auth = quote(listen.username, safe="") + ":" + quote(listen.password or "", safe="") + "@"
    return f"{scheme}://{auth}{host}:{listen.port}"


def print_listener_curl_commands(listens: Sequence[Listen]) -> None:
    for listen in listens:
        print(f"xray-run: test with: {listener_curl_command(listen)}", file=sys.stderr)


def run_xray_test(config_args: Sequence[str], test_urls: Sequence[str], timeout: float, verbose: int = 0) -> dict[str, Any] | None:
    port = free_port()
    test_args = [f"-L=socks5://127.0.0.1:{port}", *strip_listen_args(config_args)]
    try:
        xray_bin = ensure_xray_dependency(verbose=verbose)
        config = xray_run_json(test_args, validate=False, verbose=verbose)
    except Exception as exc:
        verbose_log(verbose, 1, f"skipping config after setup failure: {type(exc).__name__}: {exc}")
        return None
    temp = tempfile.NamedTemporaryFile("w", suffix=".json", prefix="xray-trier-", delete=False)
    try:
        with temp:
            json.dump(config, temp)
        proc = subprocess.Popen([xray_bin, "run", "-c", temp.name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
            return {"best-delay-ms": min(successful_delays), "config": list(config_args), "tests": tests}
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
    finally:
        Path(temp.name).unlink(missing_ok=True)


def xray_tmux_command(session: str, window: str, config: Sequence[str]) -> list[str]:
    command = " ".join(shlex.quote(part) for part in ["xray-run", "exec", *config])
    return ["tmux", "new-window", "-t", session, "-n", window, command]


def has_listen_args(args: Sequence[str]) -> bool:
    return any(arg == "-L" or arg.startswith("-L=") for arg in args)


def run_xray_in_tmux(session: str, results: Sequence[dict[str, Any]], run_top: int) -> None:
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
        processes = [(["xray-run", "exec", *config], [listener_proxy_url(listen) for listen in parse_xray_args(config).listens]) for config in launched]
        run_managed_session(session, processes)
        print_xray_tmux_launch_info(session, launched, managed=True)
        return

    for index, config in enumerate(launched, start=1):
        subprocess.run(xray_tmux_command(session, f"xray-{index}", config), check=True)
    print_xray_tmux_launch_info(session, launched)


def print_xray_tmux_launch_info(session: str, configs: Sequence[Sequence[str]], *, managed: bool = False) -> None:
    print(f"tmux session: {session}", file=sys.stderr)
    if managed:
        print("runner: managed detached processes (tmux unavailable)", file=sys.stderr)
    else:
        print(f"attach: tmux attach -t {shlex.quote(session)}", file=sys.stderr)
    for config in configs:
        parsed = parse_xray_args(config)
        for listen in parsed.listens:
            print(f"test xray: {listener_curl_command(listen)}", file=sys.stderr)


def xray_run_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="xray-run",
        description="Generate or run Xray config from a gost-like -L/-F interface.",
        epilog="""listener examples:
  -L=socks5://127.0.0.1:1060
  -L=http://user:password@:2060

forward examples:
  -F='vless://...'
  -F=direct://

If no -L is provided, a free local socks listener is selected.
If no -F is provided, direct:// is used.
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-v", "--verbose", action="count", default=0, help="increase diagnostic output; repeat for more detail")
    subparsers = parser.add_subparsers(dest="mode", metavar="{json,exec}", required=True)
    add_xray_run_subparser(
        subparsers,
        "json",
        "print generated Xray JSON config to stdout",
        """examples:
  xray-run json -L=socks5://127.0.0.1:1060 -F='vless://...'
  xray-run json -L=http://user:password@:2060
""",
    )
    add_xray_run_subparser(
        subparsers,
        "exec",
        "write config to a temp file and exec xray run -c",
        """examples:
  xray-run exec -F='vless://...'
  xray-run exec -L=socks5://127.0.0.1:1060 -L=http://user:password@:2060 -F='vless://...'
""",
    )
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    namespace, runner_args = parser.parse_known_args(raw_argv)
    namespace.verbose = max(namespace.verbose, count_verbose_flags(raw_argv))
    runner_args = list(runner_args)
    if runner_args and runner_args[0] == "--":
        runner_args = runner_args[1:]
    try:
        if namespace.mode == "json":
            config = xray_run_json(runner_args, verbose=namespace.verbose)
            write_json_output(config, namespace.output)
            return 0
        exec_xray(runner_args, verbose=namespace.verbose)
        return 0
    except BrokenPipeError:
        return 1
    except Exception as exc:
        print(f"xray-run: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


def add_xray_run_subparser(
    subparsers: argparse._SubParsersAction,
    name: str,
    help_text: str,
    epilog: str,
) -> None:
    subparser = subparsers.add_parser(
        name,
        usage=f"xray-run {name} [ARGS ...]",
        help=help_text,
        description=help_text,
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=argparse.SUPPRESS,
        help="increase diagnostic output; repeat for more detail",
    )
    if name == "json":
        subparser.add_argument("-o", "--output", default="-", help="write generated JSON to this file, or - for stdout")


def count_verbose_flags(argv: Sequence[str]) -> int:
    total = 0
    for arg in argv:
        if arg == "--verbose":
            total += 1
        elif arg.startswith("-") and len(arg) > 1 and set(arg[1:]) == {"v"}:
            total += len(arg) - 1
    return total


def ensure_xray_dependency(*, verbose: int = 0) -> str:
    xray_bin = str(locate_xray())
    verbose_log(verbose, 1, f"using Xray: {xray_bin}")
    smoke_test_xray(xray_bin, verbose=verbose)
    return xray_bin


def smoke_test_xray(xray_bin: str, *, verbose: int = 0) -> None:
    key = ("xray", xray_bin)
    with _SMOKE_CHECK_LOCK:
        if key in _SMOKE_CHECKED:
            return
    version = run_logged([xray_bin, "version"], verbose=verbose)
    if version.returncode != 0:
        raise RuntimeError(f"Xray version check failed\n{completed_process_details(version, command=[xray_bin, 'version'])}")
    if verbose >= 1:
        first_line = (version.stdout or version.stderr).splitlines()[0] if (version.stdout or version.stderr) else "unknown"
        verbose_log(verbose, 1, f"Xray version: {first_line}")

    config = {"log": {"loglevel": "warning"}, "inbounds": [], "outbounds": [{"protocol": "freedom", "settings": {}}]}
    with tempfile.NamedTemporaryFile("w", suffix=".json", prefix="xray-run-smoke-", delete=True) as file:
        json.dump(config, file)
        file.flush()
        command = [xray_bin, "run", "-test", "-c", file.name]
        completed = run_logged(command, verbose=verbose)
    if completed.returncode != 0:
        raise RuntimeError(f"Xray smoke test failed\n{completed_process_details(completed, command=command)}")
    with _SMOKE_CHECK_LOCK:
        _SMOKE_CHECKED.add(key)
    verbose_log(verbose, 1, "Xray smoke test passed")


def smoke_test_converter(command: Sequence[str], *, verbose: int = 0) -> None:
    key = ("Xray-Link-Json", command[0])
    with _SMOKE_CHECK_LOCK:
        if key in _SMOKE_CHECKED:
            return
    if verbose >= 3:
        verbose_log(verbose, 3, f"converter smoke link: {CONVERTER_SMOKE_LINK}")
    completed = run_logged([*command, CONVERTER_SMOKE_LINK], verbose=verbose)
    if completed.returncode != 0:
        raise RuntimeError(f"Xray-Link-Json smoke test failed\n{completed_process_details(completed, command=[*command, CONVERTER_SMOKE_LINK])}")
    payload = parse_converter_json(completed.stdout)
    outbounds = payload.get("outbounds")
    if not isinstance(outbounds, list) or not outbounds:
        raise RuntimeError("Xray-Link-Json smoke test did not emit outbounds")
    with _SMOKE_CHECK_LOCK:
        _SMOKE_CHECKED.add(key)
    verbose_log(verbose, 1, "Xray-Link-Json smoke test passed")
