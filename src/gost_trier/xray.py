from __future__ import annotations

import argparse
import copy
import json
import os
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

from .common import free_port, is_successful_test, test_url, wait_for_port
from .gost import ensure_tmux_session, strip_listen_args


DEFAULT_CONVERTER_CLONE = Path.home() / ".base" / "Xray-Link-Json"
DEFAULT_CONVERTER_CACHE = Path.home() / ".cache" / "gost-trier" / "Xray-Link-Json"
_CONVERTED_LINK_CACHE: dict[str, list[dict[str, Any]]] = {}
_CONVERTED_LINK_CACHE_LOCK = Lock()


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
        raise ValueError("at least one -F forward is required")
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


def locate_converter() -> list[str]:
    env_path = os.environ.get("XRAY_LINK_JSON")
    if env_path:
        return [env_path]
    path_binary = shutil.which("Xray-Link-Json")
    if path_binary:
        return [path_binary]
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
        )
        return [str(DEFAULT_CONVERTER_CACHE)]
    raise FileNotFoundError("Xray-Link-Json not found; set XRAY_LINK_JSON or install it on PATH")


def install_converter() -> None:
    print("xray-run: installing Xray-Link-Json with go install", file=sys.stderr)
    subprocess.run(
        ["go", "install", "github.com/NightMachinery/Xray-Link-Json@latest"],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )


def convert_link_to_outbounds(link: str, converter: Sequence[str] | None = None) -> list[dict[str, Any]]:
    if link in {"direct://", "freedom://"}:
        return [{"protocol": "freedom", "settings": {}}]
    if link == "blackhole://":
        return [{"protocol": "blackhole", "settings": {}}]

    if converter is None:
        with _CONVERTED_LINK_CACHE_LOCK:
            cached = _CONVERTED_LINK_CACHE.get(link)
        if cached is not None:
            return copy.deepcopy(cached)

    command = list(converter or locate_converter())
    completed = subprocess.run(
        [*command, link],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if completed.returncode != 0:
        raise ValueError("Xray-Link-Json failed to convert one forward")
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


def build_xray_config(args: XrayArgs, *, converter: Sequence[str] | None = None) -> dict[str, Any]:
    outbounds: list[dict[str, Any]] = []
    if len(args.forwards) == 1:
        converted = [convert_link_to_outbounds(args.forwards[0], converter=converter)]
    else:
        with ThreadPoolExecutor(max_workers=len(args.forwards)) as executor:
            converted = list(executor.map(lambda forward: convert_link_to_outbounds(forward, converter=converter), args.forwards))
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

    return {
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


def validate_xray_config(config: dict[str, Any]) -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".json", prefix="xray-run-test-", delete=True) as file:
        json.dump(config, file)
        file.flush()
        completed = subprocess.run(
            ["xray", "run", "-test", "-c", file.name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        if completed.returncode != 0:
            raise ValueError("generated Xray config failed validation")


def xray_run_json(args: Sequence[str], *, validate: bool = True) -> dict[str, Any]:
    parsed = parse_xray_args(args)
    config = build_xray_config(parsed)
    if validate:
        validate_xray_config(config)
    return config


def exec_xray(args: Sequence[str]) -> None:
    parsed = parse_xray_args(args)
    config = build_xray_config(parsed)
    validate_xray_config(config)
    print_listener_curl_commands(parsed.listens)
    temp = tempfile.NamedTemporaryFile("w", suffix=".json", prefix="xray-run-", delete=False)
    with temp:
        json.dump(config, temp)
    os.execvp("xray", ["xray", "run", "-c", temp.name])


def listener_curl_command(listen: Listen, url: str = "https://api.ipify.org") -> str:
    proxy = listener_proxy_url(listen)
    return " ".join(shlex.quote(part) for part in ["curl", "--proxy", proxy, url])


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


def run_xray_test(config_args: Sequence[str], test_urls: Sequence[str], timeout: float) -> dict[str, Any] | None:
    port = free_port()
    test_args = [f"-L=socks5://127.0.0.1:{port}", *strip_listen_args(config_args)]
    try:
        config = xray_run_json(test_args, validate=False)
    except Exception:
        return None
    temp = tempfile.NamedTemporaryFile("w", suffix=".json", prefix="xray-trier-", delete=False)
    try:
        with temp:
            json.dump(config, temp)
        proc = subprocess.Popen(["xray", "run", "-c", temp.name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
    ensure_tmux_session(session)
    for index, result in enumerate(results[:run_top], start=1):
        config = list(result["config"])
        if not has_listen_args(config):
            config = [f"-L=socks5://127.0.0.1:{free_port()}", *config]
        subprocess.run(xray_tmux_command(session, f"xray-{index}", config), check=True)


def xray_run_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="xray-run", description="Generate or run Xray config from gost-like args.")
    parser.add_argument("mode", choices=["json", "exec"])
    parser.add_argument("args", nargs=argparse.REMAINDER)
    namespace = parser.parse_args(sys.argv[1:] if argv is None else argv)
    runner_args = list(namespace.args)
    if runner_args and runner_args[0] == "--":
        runner_args = runner_args[1:]
    try:
        if namespace.mode == "json":
            config = xray_run_json(runner_args)
            json.dump(config, sys.stdout, indent=2)
            sys.stdout.write("\n")
            return 0
        exec_xray(runner_args)
        return 0
    except BrokenPipeError:
        return 1
    except Exception as exc:
        print(f"xray-run: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
