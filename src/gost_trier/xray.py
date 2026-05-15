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

from .common import (
    TrierOptions,
    free_port,
    is_successful_test,
    normalize_split_url_args,
    test_url,
    wait_for_port,
    write_json_output,
)
from .diagnostics import completed_process_details, dump_json_for_debug, run_logged, verbose_log
from .downloads import set_download_progress_enabled
from .gost import ensure_tmux_session, strip_listen_args
from .native import BinaryUpdateResult, locate_xray, locate_xray_link_json, update_xray, update_xray_link_json
from .sessions import run_managed_session


DEFAULT_CONVERTER_CLONE = Path.home() / ".base" / "Xray-Link-Json"
DEFAULT_CONVERTER_CACHE = Path.home() / ".cache" / "gost-trier" / "Xray-Link-Json"
_CONVERTED_LINK_CACHE: dict[str, list[dict[str, Any]]] = {}
_CONVERTED_LINK_CACHE_LOCK = Lock()
_SMOKE_CHECKED: set[tuple[str, str]] = set()
_SMOKE_CHECK_LOCK = Lock()
_XRAY_BIN_CACHE: str | None = None
_XRAY_BIN_CACHE_LOCK = Lock()
_CONVERTER_COMMAND_CACHE: list[str] | None = None
_CONVERTER_COMMAND_CACHE_LOCK = Lock()
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
    args = normalize_split_url_args(args)
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
    global _CONVERTER_COMMAND_CACHE
    with _CONVERTER_COMMAND_CACHE_LOCK:
        if _CONVERTER_COMMAND_CACHE is not None:
            return list(_CONVERTER_COMMAND_CACHE)
        command = resolve_converter_command(verbose=verbose)
        smoke_test_converter(command, verbose=verbose)
        _CONVERTER_COMMAND_CACHE = list(command)
        return list(command)


def resolve_converter_command(verbose: int = 0) -> list[str]:
    try:
        command = [str(locate_xray_link_json())]
        verbose_log(verbose, 1, f"using Xray-Link-Json: {command[0]}")
        return command
    except Exception as release_exc:
        print(f"xray-run: release install for Xray-Link-Json failed: {release_exc}", file=sys.stderr)
    install_converter()
    path_binary = shutil.which("Xray-Link-Json")
    if path_binary:
        return [path_binary]
    if DEFAULT_CONVERTER_CACHE.exists():
        return [str(DEFAULT_CONVERTER_CACHE)]
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
        return [str(DEFAULT_CONVERTER_CACHE)]
    raise FileNotFoundError("Xray-Link-Json not found; set XRAY_LINK_JSON_BIN or install it on PATH")


def install_converter() -> None:
    if not shutil.which("go"):
        raise FileNotFoundError(
            "Xray-Link-Json was not found and automatic release download failed; install Go or set XRAY_LINK_JSON_BIN"
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


def convert_forward_to_outbounds(
    forward: str,
    converter: Sequence[str] | None = None,
    *,
    verbose: int = 0,
    require_single_json_outbound: bool = False,
) -> list[dict[str, Any]]:
    if looks_like_json_forward(forward):
        outbounds = load_json_forward_outbounds(Path(forward).expanduser())
        if require_single_json_outbound and len(outbounds) != 1:
            raise ValueError(f"chained JSON forward must contain exactly one outbound: {forward}")
        return outbounds
    return convert_link_to_outbounds(forward, converter=converter, verbose=verbose)


def looks_like_json_forward(value: str) -> bool:
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    return not parsed.scheme and value.lower().endswith(".json")


def load_json_forward_outbounds(path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"could not read JSON forward {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON forward {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"JSON forward must be an object: {path}")
    if isinstance(payload.get("protocol"), str):
        return [normalize_outbound(payload)]
    raw_outbounds = payload.get("outbounds")
    if not isinstance(raw_outbounds, list) or not raw_outbounds:
        raise ValueError(f"JSON forward did not contain outbounds: {path}")
    outbounds = [normalize_outbound(item) for item in raw_outbounds if isinstance(item, dict)]
    if not outbounds:
        raise ValueError(f"JSON forward did not contain object outbounds: {path}")
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
    cleaned = copy.deepcopy(outbound)
    cleaned.pop("sendThrough", None)
    if cleaned.get("protocol") == "vless":
        settings = cleaned.get("settings")
        if isinstance(settings, dict):
            vnext = settings.get("vnext")
            if isinstance(vnext, list):
                for server in vnext:
                    if not isinstance(server, dict):
                        continue
                    users = server.get("users")
                    if not isinstance(users, list):
                        continue
                    for user in users:
                        if isinstance(user, dict) and "encryption" not in user:
                            user["encryption"] = "none"
    return cleaned


def build_xray_config(args: XrayArgs, *, converter: Sequence[str] | None = None, verbose: int = 0) -> dict[str, Any]:
    outbounds: list[dict[str, Any]] = []
    require_single_json_outbound = len(args.forwards) > 1
    if len(args.forwards) == 1:
        converted = [
            convert_forward_to_outbounds(
                args.forwards[0],
                converter=converter,
                verbose=verbose,
                require_single_json_outbound=require_single_json_outbound,
            )
        ]
    else:
        with ThreadPoolExecutor(max_workers=len(args.forwards)) as executor:
            converted = list(
                executor.map(
                    lambda forward: convert_forward_to_outbounds(
                        forward,
                        converter=converter,
                        verbose=verbose,
                        require_single_json_outbound=require_single_json_outbound,
                    ),
                    args.forwards,
                )
            )
    for converted_outbounds in converted:
        outbounds.extend(normalize_outbound(item) for item in converted_outbounds)
    if not outbounds:
        raise ValueError("no Xray outbounds were generated")

    for index, outbound in enumerate(outbounds, start=1):
        outbound["tag"] = f"proxy-{index}"
        if index < len(outbounds):
            outbound["proxySettings"] = {"tag": f"proxy-{index + 1}", "transportLayer": True}
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
    config_path = write_temp_xray_config(config, prefix="xray-run-test-")
    try:
        command = [xray_bin, "run", "-test", "-c", str(config_path)]
        completed = run_logged(command, verbose=verbose)
        if completed.returncode != 0:
            details = completed_process_details(completed, command=command)
            raise ValueError(f"generated Xray config failed validation\n{details}")
    finally:
        config_path.unlink(missing_ok=True)


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


def preflight_xray_trier(options: TrierOptions) -> None:
    ensure_xray_dependency(verbose=options.verbose)
    locate_converter(verbose=options.verbose)


def xray_tmux_command(session: str, window: str, config: Sequence[str]) -> list[str]:
    command = " ".join(shlex.quote(part) for part in ["xray-run", "exec", *config])
    return ["tmux", "new-window", "-t", session, "-n", window, command]


def xray_config_tmux_command(session: str, window: str, xray_bin: str, config_path: Path) -> list[str]:
    command = " ".join(shlex.quote(part) for part in [xray_bin, "run", "-c", str(config_path)])
    return ["tmux", "new-window", "-t", session, "-n", window, command]


def has_listen_args(args: Sequence[str]) -> bool:
    args = normalize_split_url_args(args)
    return any(arg == "-L" or arg.startswith("-L=") for arg in args)


def run_xray_in_tmux(session: str, results: Sequence[dict[str, Any]], options: TrierOptions) -> None:
    if not results:
        print("No working configs found; skipping tmux launch", file=sys.stderr)
        return
    run_top = options.run_top
    selected_results = list(results[:run_top])
    balanced = len(selected_results) > 1
    launched = xray_launch_configs(selected_results, balanced=balanced)

    print_xray_selected_results(selected_results, options, balanced=balanced)
    balanced_config_path: Path | None = None
    xray_bin: str | None = None
    if len(launched) > 1:
        xray_bin = ensure_xray_dependency(verbose=options.verbose)
        balanced_config = build_balanced_xray_config(launched, options)
        validate_xray_config(balanced_config, verbose=options.verbose)
        balanced_config_path = write_temp_xray_config(balanced_config, prefix="xray-trier-balanced-")

    try:
        ensure_tmux_session(session)
    except RuntimeError as exc:
        print(f"tmux unavailable: {exc}", file=sys.stderr)
        if balanced_config_path is not None and xray_bin is not None:
            listens = parse_xray_args(launched[0]).listens
            processes = [([xray_bin, "run", "-c", str(balanced_config_path)], [listener_proxy_url(listen) for listen in listens])]
        else:
            processes = [(["xray-run", "exec", *config], [listener_proxy_url(listen) for listen in parse_xray_args(config).listens]) for config in launched]
        run_managed_session(session, processes)
        print_xray_tmux_launch_info(session, launched[:1] if balanced_config_path is not None else launched, managed=True)
        return

    if balanced_config_path is not None and xray_bin is not None:
        subprocess.run(xray_config_tmux_command(session, "xray-balanced", xray_bin, balanced_config_path), check=True)
    else:
        for index, config in enumerate(launched, start=1):
            subprocess.run(xray_tmux_command(session, f"xray-{index}", config), check=True)
    print_xray_tmux_launch_info(session, launched[:1] if balanced_config_path is not None else launched)


def xray_launch_configs(results: Sequence[dict[str, Any]], *, balanced: bool) -> list[list[str]]:
    if not balanced:
        launched: list[list[str]] = []
        for result in results:
            config = list(result["config"])
            if not has_listen_args(config):
                config = [f"-L=socks5://127.0.0.1:{free_port()}", *config]
            launched.append(config)
        return launched

    first_config = list(results[0]["config"])
    listen_args = xray_listen_args(first_config)
    if not listen_args:
        listen_args = [f"-L=socks5://127.0.0.1:{free_port()}"]
    return [[*listen_args, *strip_listen_args(result["config"])] for result in results]


def xray_listen_args(args: Sequence[str]) -> list[str]:
    args = normalize_split_url_args(args)
    listens: list[str] = []
    skip_next = False
    for arg in args:
        if skip_next:
            listens.append(f"-L={arg}")
            skip_next = False
            continue
        if arg == "-L":
            skip_next = True
        elif arg.startswith("-L="):
            listens.append(arg)
    return listens


def build_balanced_xray_config(configs: Sequence[Sequence[str]], options: TrierOptions) -> dict[str, Any]:
    parsed_first = parse_xray_args(configs[0])
    outbounds: list[dict[str, Any]] = []
    entry_tags: list[str] = []
    for candidate_index, config_args in enumerate(configs, start=1):
        parsed = parse_xray_args(config_args)
        converted = convert_candidate_forwards(parsed.forwards, verbose=options.verbose)
        candidate_outbounds: list[dict[str, Any]] = []
        for converted_outbounds in converted:
            candidate_outbounds.extend(normalize_outbound(item) for item in converted_outbounds)
        if not candidate_outbounds:
            raise ValueError("no Xray outbounds were generated for balancer candidate")
        for outbound_index, outbound in enumerate(candidate_outbounds, start=1):
            tag = f"trier-candidate-{candidate_index}-proxy-{outbound_index}"
            outbound["tag"] = tag
            if outbound_index < len(candidate_outbounds):
                outbound["proxySettings"] = {
                    "tag": f"trier-candidate-{candidate_index}-proxy-{outbound_index + 1}",
                    "transportLayer": True,
                }
            else:
                outbound.pop("proxySettings", None)
        entry_tags.append(candidate_outbounds[0]["tag"])
        outbounds.extend(candidate_outbounds)

    strategy = {"type": options.balancer_strategy or "leastLoad"}
    if strategy["type"] == "leastLoad":
        strategy["settings"] = {"expected": 1}
    config: dict[str, Any] = {
        "log": {"loglevel": "warning"},
        "inbounds": [build_inbound(listen, index) for index, listen in enumerate(parsed_first.listens, start=1)],
        "outbounds": outbounds,
        "routing": {
            "rules": [
                {
                    "type": "field",
                    "inboundTag": [inbound_tag(index) for index in range(1, len(parsed_first.listens) + 1)],
                    "balancerTag": "trier-balancer",
                }
            ],
            "balancers": [
                {
                    "tag": "trier-balancer",
                    "selector": ["trier-candidate-"],
                    "fallbackTag": entry_tags[0],
                    "strategy": strategy,
                }
            ],
        },
    }
    if strategy["type"] in {"leastPing", "leastLoad"}:
        config["burstObservatory"] = {
            "subjectSelector": ["trier-candidate-"],
            "pingConfig": {
                "interval": "10s",
                "sampling": 3,
                "timeout": f"{min(max(options.timeout, 0.001), 5.0):g}s",
            },
        }
    if options.verbose >= 3:
        print(f"xray-trier: generated balanced Xray config:\n{dump_json_for_debug(config)}", file=sys.stderr)
    return config


def convert_candidate_forwards(forwards: Sequence[str], *, verbose: int = 0) -> list[list[dict[str, Any]]]:
    require_single_json_outbound = len(forwards) > 1
    if len(forwards) == 1:
        return [
            convert_forward_to_outbounds(
                forwards[0],
                verbose=verbose,
                require_single_json_outbound=require_single_json_outbound,
            )
        ]
    with ThreadPoolExecutor(max_workers=len(forwards)) as executor:
        return list(
            executor.map(
                lambda forward: convert_forward_to_outbounds(
                    forward,
                    verbose=verbose,
                    require_single_json_outbound=require_single_json_outbound,
                ),
                forwards,
            )
        )


def print_xray_selected_results(results: Sequence[dict[str, Any]], options: TrierOptions, *, balanced: bool) -> None:
    mode = f"balanced pool strategy={options.balancer_strategy or 'leastLoad'}" if balanced else "single config"
    print(f"selected xray config(s): {mode}", file=sys.stderr)
    for index, result in enumerate(results, start=1):
        print(
            f"  #{index}: loss={format_metric(result.get('loss'))} "
            f"avg={format_metric(result.get('avg-delay-ms'))} ms "
            f"std={format_metric(result.get('std-delay-ms'))} ms "
            f"success-rate={format_metric(result.get('success-rate'))} "
            f"success={result.get('success-count', '?')}/{result.get('test-count', '?')} "
            f"top-n={options.top_n} test-n={options.test_n} run-top={options.run_top}",
            file=sys.stderr,
        )
        for link in xray_config_links(result.get("config", []), verbose=options.verbose):
            print(f"    link: {link}", file=sys.stderr)


def format_metric(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def xray_config_links(args: Sequence[str], *, verbose: int = 0) -> list[str]:
    try:
        forwards = parse_xray_args(args, auto_listen=False).forwards
    except Exception:
        return [" ".join(shlex.quote(part) for part in args)]
    links: list[str] = []
    for forward in forwards:
        links.extend(xray_forward_links(forward, verbose=verbose))
    return links or [" ".join(shlex.quote(part) for part in args)]


def xray_forward_links(forward: str, *, verbose: int = 0) -> list[str]:
    if not looks_like_json_forward(forward):
        return [forward]
    try:
        command = [*locate_converter(verbose=verbose), forward]
        completed = run_logged(command, verbose=verbose)
        if completed.returncode != 0:
            raise ValueError(completed.stderr.strip() or "converter failed")
        lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
        return lines or [forward]
    except Exception as exc:
        return [f"{forward} (link conversion failed: {type(exc).__name__}: {exc})"]


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
    parser.add_argument("--progress", dest="progress", action="store_true", help="show download progress bars")
    parser.add_argument("--no-progress", dest="progress", action="store_false", help="hide download progress bars")
    parser.set_defaults(progress=True)
    subparsers = parser.add_subparsers(dest="mode", metavar="{json,exec,update-binaries}", required=True)
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
    update_parser = subparsers.add_parser(
        "update-binaries",
        help="check for Xray binary updates and download missing latest releases",
        description="Check GitHub releases for Xray and Xray-Link-Json and cache missing latest binaries.",
    )
    update_parser.add_argument("--progress", dest="progress", action="store_true", default=argparse.SUPPRESS, help="show download progress bars")
    update_parser.add_argument("--no-progress", dest="progress", action="store_false", default=argparse.SUPPRESS, help="hide download progress bars")
    update_parser.add_argument("--no-download", action="store_true", help="only report latest cached/downloadable binaries")
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    namespace, runner_args = parser.parse_known_args(raw_argv)
    namespace.verbose = max(namespace.verbose, count_verbose_flags(raw_argv))
    set_download_progress_enabled(namespace.progress)
    runner_args = normalize_split_url_args(runner_args)
    if runner_args and runner_args[0] == "--":
        runner_args = runner_args[1:]
    try:
        if namespace.mode == "json":
            config = xray_run_json(runner_args, verbose=namespace.verbose)
            write_json_output(config, namespace.output)
            return 0
        if namespace.mode == "update-binaries":
            update_binaries(no_download=namespace.no_download)
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
    subparser.add_argument("--progress", dest="progress", action="store_true", default=argparse.SUPPRESS, help="show download progress bars")
    subparser.add_argument("--no-progress", dest="progress", action="store_false", default=argparse.SUPPRESS, help="hide download progress bars")
    if name == "json":
        subparser.add_argument("-o", "--output", default="-", help="write generated JSON to this file, or - for stdout")


def update_binaries(*, no_download: bool = False) -> list[BinaryUpdateResult]:
    results = [update_xray(no_download=no_download), update_xray_link_json(no_download=no_download)]
    for result in results:
        print_binary_update_result(result, no_download=no_download)
    return results


def print_binary_update_result(result: BinaryUpdateResult, *, no_download: bool = False) -> None:
    if result.cached is not None:
        print(f"{result.tool}: latest {result.tag} already cached: {result.cached}", file=sys.stderr)
    elif result.installed is not None:
        print(f"{result.tool}: downloaded {result.tag}: {result.installed}", file=sys.stderr)
    elif no_download:
        print(f"{result.tool}: latest {result.tag} available: {result.asset_name} ({result.download_url})", file=sys.stderr)
    else:
        print(f"{result.tool}: latest {result.tag} available: {result.asset_name}", file=sys.stderr)


def count_verbose_flags(argv: Sequence[str]) -> int:
    total = 0
    for arg in argv:
        if arg == "--verbose":
            total += 1
        elif arg.startswith("-") and len(arg) > 1 and set(arg[1:]) == {"v"}:
            total += len(arg) - 1
    return total


def ensure_xray_dependency(*, verbose: int = 0) -> str:
    global _XRAY_BIN_CACHE
    with _XRAY_BIN_CACHE_LOCK:
        if _XRAY_BIN_CACHE is None:
            _XRAY_BIN_CACHE = str(locate_xray())
            verbose_log(verbose, 1, f"using Xray: {_XRAY_BIN_CACHE}")
            smoke_test_xray(_XRAY_BIN_CACHE, verbose=verbose)
        return _XRAY_BIN_CACHE


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
        config_path = write_temp_xray_config(config, prefix="xray-run-smoke-")
        try:
            command = [xray_bin, "run", "-test", "-c", str(config_path)]
            completed = run_logged(command, verbose=verbose)
        finally:
            config_path.unlink(missing_ok=True)
        if completed.returncode != 0:
            raise RuntimeError(f"Xray smoke test failed\n{completed_process_details(completed, command=command)}")
        _SMOKE_CHECKED.add(key)
        verbose_log(verbose, 1, "Xray smoke test passed")


def write_temp_xray_config(config: dict[str, Any], *, prefix: str) -> Path:
    temp = tempfile.NamedTemporaryFile("w", suffix=".json", prefix=prefix, delete=False)
    with temp:
        json.dump(config, temp)
    return Path(temp.name)


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
        _SMOKE_CHECKED.add(key)
        verbose_log(verbose, 1, "Xray-Link-Json smoke test passed")
