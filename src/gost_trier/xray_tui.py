from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import random
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import ParseResult, unquote, urlparse
from urllib.request import ProxyHandler, Request, build_opener, urlopen

import yaml

from .common import DEFAULT_TEST_URLS, decode_base64_if_needed, is_successful_test, parse_duration, test_url, wait_for_port
from .downloads import USER_AGENT
from .gost import ensure_tmux_session
from .native import DEFAULT_CACHE_ROOT
from .xray import Listen, XrayArgs, build_inbound, build_xray_config, ensure_xray_dependency, parse_listen, write_temp_xray_config


DEFAULT_CONFIG_PATH = Path.home() / ".xray-tui" / "config.yaml"
DEFAULT_SUB_AUTO_REFRESH = 60 * 60
DEFAULT_ROTATE_REFRESH = 15 * 60
DEFAULT_SAMPLE = 100
DEFAULT_TIMEOUT = 20.0
DEFAULT_JOBS = 20
NARROW_LAYOUT_COLUMNS = 100
TUI_CACHE_DIR = DEFAULT_CACHE_ROOT / "xray-tui"
TUI_STATE_FILE = TUI_CACHE_DIR / "state.json"
MANUAL_SUBGROUP_NAME = "Manual configs"
TMUX_WINDOW_NAME = "xray"


EXAMPLE_CONFIG = """# xray-tui config
groups:
  - name: default
    subscriptions:
      - url: https://example.com/sub.txt
    configs:
      - link: direct://
      - link: vless://00000000-0000-0000-0000-000000000000@example.com:443?security=tls#example
      - path: ~/xray-configs/example.json
"""


DEFAULT_HOTKEYS: dict[str, str | None] = {
    "next_row": "j",
    "next_row_alt": "down",
    "previous_row": "k",
    "previous_row_alt": "up",
    "focus_or_collapse_navigation": "h",
    "focus_or_collapse_navigation_alt": "left",
    "focus_or_expand_table": "l",
    "focus_or_expand_table_alt": "right",
    "first_row": "g",
    "last_row": "G",
    "focus_next": "tab",
    "focus_previous": "shift+tab",
    "previous_subgroup": "[",
    "next_subgroup": "]",
    "previous_group": "{",
    "next_group": "}",
    "select": "enter",
    "cancel": "esc",
    "quit": "q",
    "filter": "/",
    "refresh_current": "r",
    "test_now": "t",
    "toggle_auto_rotate": "a",
    "refresh_all": "SPC r a",
    "refresh_group": "SPC r g",
    "refresh_subgroup": "SPC r s",
    "test_sampled": "SPC t a",
    "restart_xray": "SPC x r",
    "show_tmux_info": "SPC x a",
}


ACTION_LABELS: dict[str, str] = {
    "focus_or_collapse_navigation": "focus navigation",
    "focus_or_collapse_navigation_alt": "focus navigation",
    "focus_or_expand_table": "focus table",
    "focus_or_expand_table_alt": "focus table",
    "test_now": "test sampled configs",
    "test_sampled": "test sampled configs",
    "refresh_current": "refresh current scope",
    "show_tmux_info": "show tmux attach command",
}


@dataclass(frozen=True)
class TuiOptions:
    address: str
    socks_port: int
    http_port: int
    test_urls: list[str]
    config: Path
    python_config: Path | None
    tmux_session: str
    stop_on_exit: bool
    sub_auto_refresh: float
    rotate_refresh: float
    sample: int | None
    timeout: float
    jobs: int
    true_color: str
    dark_mode: str
    light_theme: str
    dark_theme: str
    verbose: int


@dataclass(frozen=True)
class ConfigItem:
    id: str
    name: str
    protocol: str
    source: str
    kind: str
    link: str | None = None
    path: Path | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProxyChain:
    name: str | None
    items: list[ConfigItem]


@dataclass(frozen=True)
class Subscription:
    id: str
    name: str
    url: str
    configs: list[ConfigItem] = field(default_factory=list)
    last_refreshed_at: float | None = None
    error: str | None = None


@dataclass(frozen=True)
class Subgroup:
    id: str
    name: str
    kind: str
    configs: list[ConfigItem]
    subscription: Subscription | None = None


@dataclass(frozen=True)
class ConfigGroup:
    id: str
    name: str
    subgroups: list[Subgroup]
    proxy_chain: ProxyChain | None = None


@dataclass(frozen=True)
class TuiConfig:
    groups: list[ConfigGroup]


@dataclass(frozen=True)
class TuiState:
    active_group_id: str | None = None
    active_subgroup_id: str | None = None
    active_config_id: str | None = None
    test_results: dict[str, dict[str, Any]] = field(default_factory=dict)


def parse_tui_args(argv: Sequence[str]) -> TuiOptions:
    parser = argparse.ArgumentParser(
        prog="xray-tui",
        description="Manage Xray subscription groups and configs from an interactive TUI.",
    )
    parser.add_argument("-a", "--address", default="127.0.0.1", help="listener address")
    parser.add_argument("--socks-port", type=int, default=1080, help="SOCKS listener port")
    parser.add_argument("--http-port", type=int, default=2080, help="HTTP listener port")
    parser.add_argument("--test-url", action="append", dest="test_urls", help="URL to test through the proxy; repeatable")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="YAML config path")
    parser.add_argument("--python-config", help="trusted Python hotkey config path")
    parser.add_argument("--no-python-config", action="store_true", help="disable Python hotkey config loading")
    parser.add_argument("--tmux-session", default=None, help="tmux session name; supports {SOCKS_PORT} and {HTTP_PORT}")
    parser.add_argument("--stop-on-exit", dest="stop_on_exit", action="store_true", help="stop active Xray when the TUI exits")
    parser.add_argument("--no-stop-on-exit", dest="stop_on_exit", action="store_false", help="leave active Xray running after exit")
    parser.set_defaults(stop_on_exit=True)
    parser.add_argument("--sub-auto-refresh", type=parse_duration, default=DEFAULT_SUB_AUTO_REFRESH, help="stale subscription refresh interval")
    parser.add_argument("--rotate-refresh", type=parse_duration, default=DEFAULT_ROTATE_REFRESH, help="auto-rotate test interval")
    parser.add_argument("--sample", default=str(DEFAULT_SAMPLE), help="sample N configs per rotation, or all/unlimited")
    parser.add_argument("--timeout", type=parse_duration, default=DEFAULT_TIMEOUT, help="per-test timeout")
    parser.add_argument("--jobs", type=int, default=DEFAULT_JOBS, help="parallel tests for rotation")
    parser.add_argument("--true-color", choices=["auto", "on", "off"], default="auto")
    parser.add_argument("--dark-mode", choices=["auto", "on", "off"], default="auto")
    parser.add_argument("--light-theme", default="xray-light")
    parser.add_argument("--dark-theme", default="xray-dark")
    parser.add_argument("-v", "--verbose", action="count", default=0)
    namespace = parser.parse_args(list(argv))

    if namespace.socks_port < 1 or namespace.socks_port > 65535:
        parser.error("--socks-port must be in 1..65535")
    if namespace.http_port < 1 or namespace.http_port > 65535:
        parser.error("--http-port must be in 1..65535")
    if namespace.jobs < 1:
        parser.error("--jobs must be >= 1")
    sample = parse_sample(namespace.sample, parser)
    config_path = Path(namespace.config).expanduser()
    python_config = None
    if not namespace.no_python_config:
        python_config = Path(namespace.python_config).expanduser() if namespace.python_config else config_path.with_suffix(".py")
    tmux_template = namespace.tmux_session or "xray-tui-s{SOCKS_PORT}-h{HTTP_PORT}"
    tmux_session = tmux_template.format(SOCKS_PORT=namespace.socks_port, HTTP_PORT=namespace.http_port)

    return TuiOptions(
        address=namespace.address,
        socks_port=namespace.socks_port,
        http_port=namespace.http_port,
        test_urls=namespace.test_urls or [DEFAULT_TEST_URLS[0]],
        config=config_path,
        python_config=python_config,
        tmux_session=tmux_session,
        stop_on_exit=namespace.stop_on_exit,
        sub_auto_refresh=namespace.sub_auto_refresh,
        rotate_refresh=namespace.rotate_refresh,
        sample=sample,
        timeout=namespace.timeout,
        jobs=namespace.jobs,
        true_color=namespace.true_color,
        dark_mode=namespace.dark_mode,
        light_theme=namespace.light_theme,
        dark_theme=namespace.dark_theme,
        verbose=namespace.verbose,
    )


def parse_sample(value: str, parser: argparse.ArgumentParser) -> int | None:
    if value.lower() in {"all", "unlimited", "none"}:
        return None
    try:
        sample = int(value)
    except ValueError:
        parser.error("--sample must be a positive integer or all")
    if sample < 1:
        parser.error("--sample must be >= 1")
    return sample


def ensure_example_config(path: Path) -> bool:
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(EXAMPLE_CONFIG, encoding="utf-8")
    return True


def print_created_config_help(path: Path) -> None:
    print(f"Created example config: {path}", file=sys.stderr)
    print("Edit it, then run xray-tui again. Config names are optional; link #fragments are used as names.", file=sys.stderr)


def load_tui_config(path: Path, *, state: TuiState | None = None) -> TuiConfig:
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML config {path}: {exc}") from exc
    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict):
        raise ValueError("config YAML must be a mapping")
    raw_groups = loaded.get("groups", [])
    raw_proxy_chains = loaded.get("proxy_chains", [])
    if not isinstance(raw_proxy_chains, list):
        raise ValueError("config field 'proxy_chains' must be a list")
    if not isinstance(raw_groups, list):
        raise ValueError("config field 'groups' must be a list")
    proxy_chains = parse_proxy_chains(raw_proxy_chains)
    groups = [parse_group(raw_group, index, state=state, proxy_chains=proxy_chains) for index, raw_group in enumerate(raw_groups, start=1)]
    return TuiConfig(groups=groups)


def startup_log(message: str) -> None:
    print(f"xray-tui: {message}", file=sys.stderr, flush=True)


def count_tui_config(config: TuiConfig) -> tuple[int, int, int]:
    groups = len(config.groups)
    subscriptions = 0
    configs = 0
    for group in config.groups:
        for subgroup in group.subgroups:
            if subgroup.subscription is not None:
                subscriptions += 1
            configs += len(subgroup.configs)
    return groups, subscriptions, configs


def startup_refresh_stale_selected_scope(config: TuiConfig, state: TuiState, options: TuiOptions) -> TuiConfig:
    active_group = find_group(config, state.active_group_id)
    active_subgroup = find_subgroup(active_group, state.active_subgroup_id) if active_group else None
    if not active_subgroup or not active_subgroup.subscription:
        startup_log("active scope has no subscription to auto-refresh")
        return config
    subscription = active_subgroup.subscription
    if not subscription_needs_refresh(subscription, options.sub_auto_refresh):
        startup_log(f"active subscription cache is fresh: {subscription.name}")
        return config

    startup_log(f"refreshing active subscription before TUI loads: {subscription.name}")
    started_at = time.monotonic()
    refreshed = refresh_subscription(subscription, timeout=options.timeout)
    elapsed = time.monotonic() - started_at
    if refreshed.error:
        startup_log(f"refresh failed after {elapsed:.1f}s; keeping cached links: {refreshed.error}")
    else:
        startup_log(f"refreshed {len(refreshed.configs)} link(s) in {elapsed:.1f}s")
    startup_log("reloading config after subscription refresh")
    return load_tui_config(options.config, state=state)


def parse_proxy_chains(raw_proxy_chains: Sequence[Any]) -> dict[str, ProxyChain]:
    proxy_chains: dict[str, ProxyChain] = {}
    for index, raw_chain in enumerate(raw_proxy_chains, start=1):
        if not isinstance(raw_chain, dict):
            raise ValueError(f"proxy chain #{index} must be a mapping")
        name = string_field(raw_chain, "name")
        if not name:
            raise ValueError(f"proxy chain #{index} is missing name")
        if name in proxy_chains:
            raise ValueError(f"duplicate proxy chain name: {name}")
        raw_items = raw_chain.get("chain", [])
        proxy_chains[name] = ProxyChain(name=name, items=parse_proxy_chain_items(raw_items, f"proxy chain {name!r}"))
    return proxy_chains


def parse_proxy_chain_items(raw_items: Any, source: str) -> list[ConfigItem]:
    if not isinstance(raw_items, list):
        raise ValueError(f"{source} field 'chain' must be a list")
    if not raw_items:
        raise ValueError(f"{source} must contain at least one proxy")
    return [parse_config_item(raw_item, f"{source}:chain", item_index, source=source) for item_index, raw_item in enumerate(raw_items, start=1)]


def parse_group(raw: Any, index: int, *, state: TuiState | None, proxy_chains: Mapping[str, ProxyChain] | None = None) -> ConfigGroup:
    if not isinstance(raw, dict):
        raise ValueError(f"group #{index} must be a mapping")
    name = string_field(raw, "name") or f"group-{index}"
    group_id = stable_id("group", raw.get("id") or name)
    raw_subs = raw.get("subscriptions", [])
    raw_configs = raw.get("configs", [])
    raw_proxy_chain = raw.get("proxy_chain")
    if not isinstance(raw_subs, list):
        raise ValueError(f"group {name!r} field 'subscriptions' must be a list")
    if not isinstance(raw_configs, list):
        raise ValueError(f"group {name!r} field 'configs' must be a list")
    proxy_chain = parse_group_proxy_chain(raw_proxy_chain, name, proxy_chains or {})

    subgroups: list[Subgroup] = []
    for sub_index, raw_sub in enumerate(raw_subs, start=1):
        subscription = parse_subscription(raw_sub, group_id, sub_index, state=state)
        subgroups.append(
            Subgroup(
                id=subscription.id,
                name=subscription.name,
                kind="subscription",
                configs=subscription.configs,
                subscription=subscription,
            )
        )
    manual_configs = [parse_config_item(raw_config, f"{group_id}:manual", cfg_index, source=MANUAL_SUBGROUP_NAME) for cfg_index, raw_config in enumerate(raw_configs, start=1)]
    if manual_configs or not subgroups:
        manual_id = stable_id("subgroup", f"{group_id}:manual")
        subgroups.append(Subgroup(id=manual_id, name=MANUAL_SUBGROUP_NAME, kind="manual", configs=manual_configs))
    return ConfigGroup(id=group_id, name=name, subgroups=subgroups, proxy_chain=proxy_chain)


def parse_group_proxy_chain(raw_proxy_chain: Any, group_name: str, proxy_chains: Mapping[str, ProxyChain]) -> ProxyChain | None:
    if raw_proxy_chain is None:
        return None
    if isinstance(raw_proxy_chain, str):
        name = raw_proxy_chain.strip()
        if not name:
            return None
        proxy_chain = proxy_chains.get(name)
        if proxy_chain is None:
            raise ValueError(f"group {group_name!r} references unknown proxy chain: {name}")
        return proxy_chain
    if isinstance(raw_proxy_chain, list):
        return ProxyChain(name=None, items=parse_proxy_chain_items(raw_proxy_chain, f"group {group_name!r} proxy_chain"))
    raise ValueError(f"group {group_name!r} field 'proxy_chain' must be a chain name or list")


def parse_subscription(raw: Any, group_id: str, index: int, *, state: TuiState | None) -> Subscription:
    if isinstance(raw, str):
        raw = {"url": raw}
    if not isinstance(raw, dict):
        raise ValueError(f"subscription #{index} must be a mapping or URL string")
    url = string_field(raw, "url")
    if not url:
        raise ValueError(f"subscription #{index} is missing url")
    sub_id = stable_id("subscription", f"{group_id}:{url}")
    name = string_field(raw, "name") or subscription_name_from_url(url, sub_id)
    cache = read_subscription_cache(sub_id)
    configs = [config_item_from_link(link, f"{sub_id}:cache", link_index, source=name) for link_index, link in enumerate(cache.get("links", []), start=1)]
    last_refreshed_at = cache.get("last_refreshed_at") if isinstance(cache.get("last_refreshed_at"), (int, float)) else None
    error = cache.get("error") if isinstance(cache.get("error"), str) else None
    return Subscription(id=sub_id, name=name, url=url, configs=configs, last_refreshed_at=last_refreshed_at, error=error)


def parse_config_item(raw: Any, scope: str, index: int, *, source: str) -> ConfigItem:
    if isinstance(raw, str):
        if looks_like_json_path(raw):
            raw = {"path": raw}
        else:
            raw = {"link": raw}
    if not isinstance(raw, dict):
        raise ValueError(f"config #{index} in {source} must be a mapping or string")
    link = string_field(raw, "link")
    path_text = string_field(raw, "path")
    if bool(link) == bool(path_text):
        raise ValueError(f"config #{index} in {source} must specify exactly one of link or path")
    if link:
        item = config_item_from_link(link, scope, index, source=source, explicit_name=string_field(raw, "name"), raw=raw)
        return item
    path = Path(path_text or "").expanduser()
    item_id = stable_id("config", f"{scope}:path:{path}")
    name = string_field(raw, "name") or path.stem or f"config-{short_hash(str(path))}"
    protocol = json_config_protocol(path)
    return ConfigItem(id=item_id, name=name, protocol=protocol, source=source, kind="path", path=path, raw=dict(raw))


def config_item_from_link(
    link: str,
    scope: str,
    index: int,
    *,
    source: str,
    explicit_name: str | None = None,
    raw: Mapping[str, Any] | None = None,
) -> ConfigItem:
    parsed = safe_urlparse(link)
    item_id = stable_id("config", f"{scope}:link:{link}")
    name = explicit_name or link_display_name(link, fallback=f"config-{short_hash(link)}")
    protocol = link_protocol(link, parsed)
    return ConfigItem(id=item_id, name=name, protocol=protocol, source=source, kind="link", link=link, raw=dict(raw or {}))


def string_field(raw: Mapping[str, Any], key: str) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"field {key!r} must be a string")
    text = value.strip()
    return text or None


def looks_like_json_path(value: str) -> bool:
    parsed = safe_urlparse(value)
    return not parsed.scheme and value.lower().endswith(".json")


def link_display_name(link: str, *, fallback: str) -> str:
    parsed = safe_urlparse(link)
    if parsed.fragment:
        decoded = unquote(parsed.fragment).strip()
        if decoded:
            return decoded
    if parsed.hostname:
        host = parsed.hostname
        try:
            port = parsed.port
        except ValueError:
            port = None
        if port is not None:
            return f"{host}:{port}"
        return host
    return fallback


def safe_urlparse(value: str) -> ParseResult:
    try:
        return urlparse(value)
    except ValueError:
        return ParseResult(scheme=link_scheme(value), netloc="", path=value, params="", query="", fragment="")


def link_protocol(link: str, parsed: ParseResult) -> str:
    if parsed.scheme:
        return parsed.scheme
    if link == "direct://":
        return "direct"
    return link_scheme(link) or "link"


def link_scheme(link: str) -> str:
    separator = link.find("://")
    if separator <= 0:
        return ""
    scheme = link[:separator].lower()
    if all(char.isalnum() or char in "+-." for char in scheme):
        return scheme
    return ""


def subscription_name_from_url(url: str, sub_id: str) -> str:
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    if parsed.hostname and parts:
        return f"{parsed.hostname}/{parts[-1]}"
    if parsed.hostname:
        return parsed.hostname
    return f"subscription-{short_hash(sub_id)}"


def json_config_protocol(path: Path) -> str:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return "json"
    if not isinstance(payload, dict):
        return "json"
    outbounds = payload.get("outbounds")
    if not isinstance(outbounds, list):
        return "json"
    for outbound in outbounds:
        if isinstance(outbound, dict) and isinstance(outbound.get("protocol"), str):
            return outbound["protocol"]
    return "json"


def stable_id(prefix: str, value: object) -> str:
    return f"{prefix}-{short_hash(str(value))}"


def short_hash(value: str, length: int = 10) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def cache_file_for_subscription(subscription_id: str) -> Path:
    return TUI_CACHE_DIR / "subscriptions" / f"{subscription_id}.json"


def read_subscription_cache(subscription_id: str) -> dict[str, Any]:
    path = cache_file_for_subscription(subscription_id)
    if not path.exists():
        return {"links": []}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"links": []}
    if not isinstance(payload, dict):
        return {"links": []}
    links = payload.get("links")
    if not isinstance(links, list):
        payload["links"] = []
    else:
        payload["links"] = [link for link in links if isinstance(link, str)]
    return payload


def write_subscription_cache(subscription_id: str, payload: Mapping[str, Any]) -> None:
    path = cache_file_for_subscription(subscription_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def load_state(path: Path = TUI_STATE_FILE) -> TuiState:
    if not path.exists():
        return TuiState()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return TuiState()
    if not isinstance(payload, dict):
        return TuiState()
    results = payload.get("test_results", {})
    return TuiState(
        active_group_id=payload.get("active_group_id") if isinstance(payload.get("active_group_id"), str) else None,
        active_subgroup_id=payload.get("active_subgroup_id") if isinstance(payload.get("active_subgroup_id"), str) else None,
        active_config_id=payload.get("active_config_id") if isinstance(payload.get("active_config_id"), str) else None,
        test_results=results if isinstance(results, dict) else {},
    )


def save_state(state: TuiState, path: Path = TUI_STATE_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "active_group_id": state.active_group_id,
                "active_subgroup_id": state.active_subgroup_id,
                "active_config_id": state.active_config_id,
                "test_results": state.test_results,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def load_hotkeys(path: Path | None) -> dict[str, str | None]:
    hotkeys = dict(DEFAULT_HOTKEYS)
    if path is not None and path.exists():
        spec = importlib.util.spec_from_file_location("xray_tui_user_config", path)
        if spec is None or spec.loader is None:
            raise ValueError(f"could not load Python config: {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        configure = getattr(module, "configure", None)
        if configure is None:
            raise ValueError(f"Python config {path} must define configure(hotkeys)")
        if not callable(configure):
            raise ValueError(f"Python config {path} configure must be callable")
        configure(hotkeys)
    validate_hotkeys(hotkeys)
    return hotkeys


def validate_hotkeys(hotkeys: Mapping[str, str | None]) -> None:
    unknown = sorted(set(hotkeys) - set(DEFAULT_HOTKEYS))
    if unknown:
        raise ValueError(f"unknown hotkey action(s): {', '.join(unknown)}")
    seen: dict[str, str] = {}
    for action, binding in hotkeys.items():
        if binding is None:
            continue
        if not isinstance(binding, str) or not binding.strip():
            raise ValueError(f"hotkey for {action} must be a non-empty string or None")
        normalized = normalize_key_sequence(binding)
        conflict = seen.get(normalized)
        if conflict is not None:
            raise ValueError(f"duplicate hotkey {binding!r} for {conflict} and {action}")
        seen[normalized] = action


def normalize_key_sequence(binding: str) -> str:
    return " ".join(normalize_key_part(part) for part in binding.split() if part.strip())


def normalize_key_part(part: str) -> str:
    stripped = part.strip()
    if len(stripped) == 1:
        return stripped
    if stripped.lower() in {"spc", "space"}:
        return "SPC"
    return stripped.lower()


def chord_next_keys(hotkeys: Mapping[str, str | None], prefix: Sequence[str]) -> dict[str, list[str]]:
    normalized_prefix = [normalize_key_part(item) for item in prefix]
    remaining: dict[str, list[str]] = {}
    for action, binding in hotkeys.items():
        if binding is None:
            continue
        parts = normalize_key_sequence(binding).split()
        if len(parts) > len(normalized_prefix) and parts[: len(normalized_prefix)] == normalized_prefix:
            remaining[parts[len(normalized_prefix)]] = [action, *parts[len(normalized_prefix) + 1 :]]
    return remaining


def build_listens(options: TuiOptions) -> list[Listen]:
    return [
        parse_listen(f"socks5://{options.address}:{options.socks_port}"),
        parse_listen(f"http://{options.address}:{options.http_port}"),
    ]


def config_item_forward(item: ConfigItem) -> str:
    if item.kind == "link":
        if item.link is None:
            raise ValueError("link config is missing link")
        return item.link
    if item.path is None:
        raise ValueError("path config is missing path")
    return str(item.path)


def proxy_chain_forwards(proxy_chain: ProxyChain | None) -> list[str]:
    if proxy_chain is None:
        return []
    return [config_item_forward(item) for item in proxy_chain.items]


def proxy_chain_label(proxy_chain: ProxyChain | None) -> str:
    if proxy_chain is None:
        return "-"
    if proxy_chain.name:
        return proxy_chain.name
    return f"inline({len(proxy_chain.items)})"


def config_item_to_xray_config(
    item: ConfigItem,
    options: TuiOptions,
    *,
    proxy_chain: ProxyChain | None = None,
    test_port: int | None = None,
    verbose: int = 0,
) -> dict[str, Any]:
    listens = [parse_listen(f"socks5://127.0.0.1:{test_port}")] if test_port is not None else build_listens(options)
    if proxy_chain is not None:
        return build_xray_config(XrayArgs(listens=listens, forwards=[config_item_forward(item), *proxy_chain_forwards(proxy_chain)]), verbose=verbose)
    if item.kind == "link":
        return build_xray_config(XrayArgs(listens=listens, forwards=[config_item_forward(item)]), verbose=verbose)
    if item.path is None:
        raise ValueError("path config is missing path")
    payload = json.loads(item.path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON config must be an object: {item.path}")
    config = dict(payload)
    config["inbounds"] = [build_inbound(listen, index) for index, listen in enumerate(listens, start=1)]
    return config


def test_config_item(item: ConfigItem, options: TuiOptions, *, proxy_chain: ProxyChain | None = None) -> dict[str, Any] | None:
    port = free_test_port()
    try:
        xray_bin = ensure_xray_dependency(verbose=options.verbose)
        config = config_item_to_xray_config(item, options, proxy_chain=proxy_chain, test_port=port, verbose=options.verbose)
    except Exception as exc:
        return {"config-id": item.id, "result": "error", "error": f"{type(exc).__name__}: {exc}"}
    config_path = write_temp_xray_config(config, prefix="xray-tui-test-")
    proc: subprocess.Popen[bytes] | None = None
    try:
        proc = subprocess.Popen([xray_bin, "run", "-c", str(config_path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if proc.poll() is not None:
            return None
        if not wait_for_port(port, min(options.timeout, 5.0)):
            return None
        tests = [test_url(url, port, options.timeout) for url in options.test_urls]
        successful = [item for item in tests if is_successful_test(item)]
        if not successful:
            return None
        return {
            "config-id": item.id,
            "best-delay-ms": min(result["delay-ms"] for result in successful),
            "tests": tests,
        }
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
        config_path.unlink(missing_ok=True)


def free_test_port() -> int:
    from .common import free_port

    return free_port()


def sample_configs(configs: Sequence[ConfigItem], sample: int | None) -> list[ConfigItem]:
    if sample is None or len(configs) <= sample:
        return list(configs)
    return random.sample(list(configs), sample)


def choose_fastest(results: Iterable[dict[str, Any] | None]) -> dict[str, Any] | None:
    successful = [result for result in results if result is not None and "best-delay-ms" in result]
    if not successful:
        return None
    return min(successful, key=lambda item: item["best-delay-ms"])


def rotate_configs(
    configs: Sequence[ConfigItem],
    options: TuiOptions,
    *,
    proxy_chain: ProxyChain | None = None,
    progress: Callable[[int, int, dict[str, Any] | None], None] | None = None,
) -> dict[str, Any] | None:
    sampled = sample_configs(configs, options.sample)
    if not sampled:
        return None
    results: list[dict[str, Any] | None] = []
    with ThreadPoolExecutor(max_workers=min(options.jobs, len(sampled))) as executor:
        futures = [executor.submit(test_config_item, item, options, proxy_chain=proxy_chain) for item in sampled]
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            if progress is not None:
                progress(len(results), len(sampled), result)
    return choose_fastest(results)


def refresh_subscription(subscription: Subscription, *, timeout: float = 30) -> Subscription:
    old_cache = read_subscription_cache(subscription.id)
    try:
        raw = download_without_proxy(subscription.url, timeout=timeout)
    except Exception as direct_exc:
        try:
            raw = download_with_env_proxy(subscription.url, timeout=timeout)
        except Exception as proxy_exc:
            payload = dict(old_cache)
            payload["error"] = f"direct: {type(direct_exc).__name__}: {direct_exc}; proxy: {type(proxy_exc).__name__}: {proxy_exc}"
            write_subscription_cache(subscription.id, payload)
            return Subscription(
                id=subscription.id,
                name=subscription.name,
                url=subscription.url,
                configs=subscription.configs,
                last_refreshed_at=subscription.last_refreshed_at,
                error=payload["error"],
            )
    links = subscription_links_from_bytes(raw)
    payload = {"url": subscription.url, "links": links, "last_refreshed_at": time.time(), "error": None}
    write_subscription_cache(subscription.id, payload)
    configs = [config_item_from_link(link, f"{subscription.id}:cache", index, source=subscription.name) for index, link in enumerate(links, start=1)]
    return Subscription(id=subscription.id, name=subscription.name, url=subscription.url, configs=configs, last_refreshed_at=payload["last_refreshed_at"])


def subscription_needs_refresh(subscription: Subscription, max_age_seconds: float) -> bool:
    if subscription.last_refreshed_at is None:
        return True
    return time.time() - subscription.last_refreshed_at >= max_age_seconds


def subscription_links_from_bytes(raw: bytes) -> list[str]:
    text = decode_base64_if_needed(raw).decode("utf-8")
    return [line for line in (raw_line.strip() for raw_line in text.splitlines()) if line and not line.startswith("#")]


def download_without_proxy(url: str, *, timeout: float) -> bytes:
    opener = build_opener(ProxyHandler({}))
    request = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.read()
    except (HTTPError, URLError) as exc:
        raise RuntimeError(str(exc)) from exc


def download_with_env_proxy(url: str, *, timeout: float) -> bytes:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.read()
    except (HTTPError, URLError) as exc:
        raise RuntimeError(str(exc)) from exc


class XraySessionManager:
    def __init__(self, options: TuiOptions) -> None:
        self.options = options
        self.mode: str | None = None
        self.process: subprocess.Popen[bytes] | None = None
        self.config_path: Path | None = None

    def start(self, item: ConfigItem, *, proxy_chain: ProxyChain | None = None) -> None:
        self.stop()
        config = config_item_to_xray_config(item, self.options, proxy_chain=proxy_chain, verbose=self.options.verbose)
        config_dir = TUI_CACHE_DIR / "run"
        config_dir.mkdir(parents=True, exist_ok=True)
        fd, raw_path = tempfile.mkstemp(prefix="active-", suffix=".json", dir=config_dir)
        path = Path(raw_path)
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(config, file)
        self.config_path = path
        xray_bin = ensure_xray_dependency(verbose=self.options.verbose)
        if shutil.which("tmux"):
            ensure_tmux_session(self.options.tmux_session)
            self._start_tmux(xray_bin, path)
            self.mode = "tmux"
            return
        self.process = subprocess.Popen([xray_bin, "run", "-c", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.mode = "process"

    def _start_tmux(self, xray_bin: str, config_path: Path) -> None:
        target = f"{self.options.tmux_session}:{TMUX_WINDOW_NAME}"
        subprocess.run(["tmux", "kill-window", "-t", target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        command = " ".join(shlex.quote(part) for part in [xray_bin, "run", "-c", str(config_path)])
        subprocess.run(["tmux", "new-window", "-t", self.options.tmux_session, "-n", TMUX_WINDOW_NAME, command], check=True)

    def stop(self) -> None:
        if self.mode == "tmux":
            target = f"{self.options.tmux_session}:{TMUX_WINDOW_NAME}"
            subprocess.run(["tmux", "kill-window", "-t", target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
            self.process = None
        if self.config_path is not None:
            self.config_path.unlink(missing_ok=True)
            self.config_path = None
        self.mode = None

    def close(self) -> None:
        if self.options.stop_on_exit:
            self.stop()

    def attach_command(self) -> str:
        return " ".join(shlex.quote(part) for part in ["tmux", "attach", "-t", self.options.tmux_session])


def flatten_group(group: ConfigGroup) -> list[ConfigItem]:
    configs: list[ConfigItem] = []
    for subgroup in group.subgroups:
        configs.extend(subgroup.configs)
    return configs


def find_group(config: TuiConfig, group_id: str | None) -> ConfigGroup | None:
    for group in config.groups:
        if group.id == group_id:
            return group
    return config.groups[0] if config.groups else None


def find_subgroup(group: ConfigGroup, subgroup_id: str | None) -> Subgroup | None:
    for subgroup in group.subgroups:
        if subgroup.id == subgroup_id:
            return subgroup
    return group.subgroups[0] if group.subgroups else None


def find_config(items: Sequence[ConfigItem], config_id: str | None) -> ConfigItem | None:
    for item in items:
        if item.id == config_id:
            return item
    return None


def layout_mode(width: int) -> str:
    return "narrow" if width < NARROW_LAYOUT_COLUMNS else "wide"


def true_color_enabled(mode: str) -> bool:
    if mode == "on":
        return True
    if mode == "off":
        return False
    colorterm = os.environ.get("COLORTERM", "").lower()
    if colorterm in {"truecolor", "24bit"}:
        return True
    if os.environ.get("KITTY_WINDOW_ID") or os.environ.get("TERM", "").lower().startswith("xterm-kitty"):
        return True
    return False


def dark_mode_enabled(mode: str) -> bool:
    if mode == "on":
        return True
    if mode == "off":
        return False
    colorfgbg = os.environ.get("COLORFGBG", "")
    if ";" in colorfgbg:
        try:
            background = int(colorfgbg.split(";")[-1])
        except ValueError:
            return True
        return background < 8
    return True


def run_textual_app(config: TuiConfig, state: TuiState, options: TuiOptions, hotkeys: Mapping[str, str | None]) -> int:
    try:
        from textual.app import App, ComposeResult
        from textual.containers import Horizontal, Vertical
        from textual.theme import Theme
        from textual.widgets import DataTable, Footer, Header, Static, Tree
    except ModuleNotFoundError as exc:
        raise RuntimeError("Textual is required for xray-tui; install package dependencies with uv tool install --reinstall .") from exc

    class XrayTuiApp(App[None]):
        CSS = """
        Screen { layout: vertical; }
        #tabs { height: 3; display: none; }
        #body { height: 1fr; }
        #nav { width: 32; min-width: 22; border-right: solid $panel; }
        #status, #hints { height: 1; }
        #table { width: 1fr; }
        .narrow #nav { display: none; }
        .narrow #tabs { display: block; }
        """
        BINDINGS = [(binding.replace("SPC", "space"), action, ACTION_LABELS.get(action, action.replace("_", " "))) for action, binding in hotkeys.items() if binding and " " not in binding]

        def __init__(self) -> None:
            super().__init__()
            self.tui_config = config
            self.state = state
            self.options = options
            self.hotkeys = dict(hotkeys)
            self.chord: list[str] = []
            self.runner = XraySessionManager(options)
            self.active_group = find_group(config, state.active_group_id)
            self.active_subgroup = find_subgroup(self.active_group, state.active_subgroup_id) if self.active_group else None
            self.auto_rotate = False
            self.filter_text = ""
            self.busy: str | None = None
            self.row_status: dict[str, str] = {}

        def compose(self) -> ComposeResult:
            yield Header()
            yield Static("", id="tabs", markup=False)
            with Horizontal(id="body"):
                yield Tree("groups", id="nav")
                with Vertical(id="main"):
                    yield DataTable(id="table")
            yield Static("", id="status", markup=False)
            yield Static("", id="hints", markup=False)
            yield Footer()

        def on_mount(self) -> None:
            self.refresh_layout()
            self.populate_tree()
            self.populate_table()
            self.query_one("#table", DataTable).focus()
            self.update_status()
            self.set_interval(self.options.rotate_refresh, self.maybe_auto_rotate)
            self.start_last_active()

        def on_resize(self) -> None:
            self.refresh_layout()

        def refresh_layout(self) -> None:
            if layout_mode(self.size.width) == "narrow":
                self.add_class("narrow")
            else:
                self.remove_class("narrow")
            self.update_tabs()

        def update_tabs(self) -> None:
            tabs = self.query_one("#tabs", Static)
            if not self.active_group:
                tabs.update("")
                return
            groups = " ".join(f"[{'*' if group.id == self.active_group.id else ' '}]{group.name}" for group in self.tui_config.groups)
            subs = " ".join(
                f"[{'*' if self.active_subgroup and subgroup.id == self.active_subgroup.id else ' '}]{subgroup.name}"
                for subgroup in self.active_group.subgroups
            )
            tabs.update(f"{groups}\n{subs}")

        def populate_tree(self) -> None:
            tree = self.query_one("#nav", Tree)
            tree.clear()
            root = tree.root
            for group in self.tui_config.groups:
                group_node = root.add(group.name, data=group.id)
                for subgroup in group.subgroups:
                    group_node.add_leaf(subgroup.name, data=subgroup.id)
            root.expand()

        def current_items(self) -> list[ConfigItem]:
            if self.active_group is None:
                return []
            if self.active_subgroup is None:
                return flatten_group(self.active_group)
            return list(self.active_subgroup.configs)

        def current_proxy_chain(self) -> ProxyChain | None:
            return self.active_group.proxy_chain if self.active_group else None

        def populate_table(self) -> None:
            table = self.query_one("#table", DataTable)
            table.clear(columns=True)
            table.zebra_stripes = True
            table.fixed_columns = 1
            table.add_columns("#", "Name", "Proto", "Source", "State", "Latency")
            for index, item in enumerate(self.current_items(), start=1):
                result = self.state.test_results.get(item.id, {})
                latency = f"{result.get('best-delay-ms')} ms" if "best-delay-ms" in result else ""
                status = self.row_status.get(item.id)
                if status is None:
                    status = "active" if item.id == self.state.active_config_id else ("ok" if latency else result.get("result", ""))
                table.add_row(str(index), item.name, item.protocol, item.source, status, latency, key=item.id)

        def update_status(self, message: str = "") -> None:
            status = self.query_one("#status", Static)
            group = self.active_group.name if self.active_group else "-"
            subgroup = self.active_subgroup.name if self.active_subgroup else "-"
            parts = []
            if message:
                parts.append(message)
            parts.extend(
                [
                    f"group {group}",
                    f"subgroup {subgroup}",
                    f"chain {proxy_chain_label(self.current_proxy_chain())}",
                    f"socks {self.options.address}:{self.options.socks_port}",
                    f"http {self.options.http_port}",
                    f"tmux {self.options.tmux_session}",
                ]
            )
            status.update(" | ".join(parts))
            hints = self.query_one("#hints", Static)
            if self.chord:
                remaining = ", ".join(sorted(chord_next_keys(self.hotkeys, self.chord)))
                hints.update(f"Chord {' '.join(self.chord)} -> {remaining}")
            else:
                hints.update("q quit | j/k move | h nav | l table | [/] subgroup | {/} group | enter start | r refresh | t test")

        def set_row_status(self, item: ConfigItem, status: str | None) -> None:
            if status is None:
                self.row_status.pop(item.id, None)
            else:
                self.row_status[item.id] = status
            self.populate_table()

        def begin_operation(self, name: str, message: str) -> bool:
            if self.busy is not None:
                self.update_status(f"busy {self.busy}; wait for it to finish")
                return False
            self.busy = name
            self.update_status(message)
            return True

        def finish_operation(self, message: str) -> None:
            self.busy = None
            self.update_status(message)

        def operation_failed(self, name: str, exc: BaseException) -> None:
            self.busy = None
            self.update_status(f"{name} failed: {type(exc).__name__}: {exc}")

        async def on_key(self, event) -> None:  # type: ignore[no-untyped-def]
            key = textual_key_name(event.key)
            if self.chord or key == "SPC":
                event.prevent_default()
                self.handle_chord_key(key)

        def handle_chord_key(self, key: str) -> None:
            candidate = [*self.chord, key]
            normalized = " ".join(normalize_key_part(part) for part in candidate)
            matches = {action: binding for action, binding in self.hotkeys.items() if binding and normalize_key_sequence(binding) == normalized}
            if matches:
                self.chord = []
                self.run_action(next(iter(matches)))
                return
            if chord_next_keys(self.hotkeys, candidate):
                self.chord = candidate
                self.update_status()
                return
            self.chord = []
            self.update_status("cancelled chord")

        def action_quit(self) -> None:
            self.runner.close()
            self.exit()

        def action_next_row(self) -> None:
            self.query_one("#table", DataTable).action_cursor_down()

        def action_next_row_alt(self) -> None:
            self.action_next_row()

        def action_previous_row(self) -> None:
            self.query_one("#table", DataTable).action_cursor_up()

        def action_previous_row_alt(self) -> None:
            self.action_previous_row()

        def action_first_row(self) -> None:
            table = self.query_one("#table", DataTable)
            table.move_cursor(row=0)

        def action_last_row(self) -> None:
            table = self.query_one("#table", DataTable)
            table.move_cursor(row=max(0, table.row_count - 1))

        def action_select(self) -> None:
            table = self.query_one("#table", DataTable)
            if table.cursor_row < 0 or table.cursor_row >= len(self.current_items()):
                return
            item = self.current_items()[table.cursor_row]
            if not self.begin_operation("start", f"starting #{table.cursor_row + 1} {item.name}"):
                return
            self.set_row_status(item, "starting")
            proxy_chain = self.current_proxy_chain()
            self.run_worker(lambda: self.start_item_worker(item, proxy_chain, "start"), name="start-config", group="xray-actions", thread=True, exit_on_error=False)

        def start_item_worker(self, item: ConfigItem, proxy_chain: ProxyChain | None, operation: str) -> None:
            try:
                self.runner.start(item, proxy_chain=proxy_chain)
            except Exception as exc:
                self.call_from_thread(self.set_row_status, item, "error")
                self.call_from_thread(self.operation_failed, operation, exc)
                return
            self.call_from_thread(self.finish_start_item, item)

        def finish_start_item(self, item: ConfigItem) -> None:
            self.row_status.clear()
            self.state = TuiState(
                active_group_id=self.active_group.id if self.active_group else None,
                active_subgroup_id=self.active_subgroup.id if self.active_subgroup else None,
                active_config_id=item.id,
                test_results=self.state.test_results,
            )
            save_state(self.state)
            self.populate_table()
            self.finish_operation(f"active {item.name}")

        def start_tested_item(self, item: ConfigItem) -> None:
            self.set_row_status(item, "starting")
            proxy_chain = self.current_proxy_chain()
            self.run_worker(lambda: self.start_item_worker(item, proxy_chain, "start"), name="start-tested-config", group="xray-actions", thread=True, exit_on_error=False)

        def action_cancel(self) -> None:
            self.chord = []
            self.filter_text = ""
            self.update_status()

        def action_refresh_current(self) -> None:
            if self.active_subgroup and self.active_subgroup.subscription:
                self.start_refresh_worker("refresh subgroup", [self.active_subgroup.subscription])
                return
            if self.active_group:
                self.start_refresh_worker("refresh group", [subgroup.subscription for subgroup in self.active_group.subgroups if subgroup.subscription])

        def action_test_now(self) -> None:
            self.action_test_sampled()

        def action_toggle_auto_rotate(self) -> None:
            self.auto_rotate = not self.auto_rotate
            self.update_status(f"auto-rotate {'on' if self.auto_rotate else 'off'}")

        def action_refresh_all(self) -> None:
            subscriptions = [subgroup.subscription for group in self.tui_config.groups for subgroup in group.subgroups if subgroup.subscription]
            self.start_refresh_worker("refresh all", subscriptions)

        def action_refresh_group(self) -> None:
            if self.active_group:
                self.start_refresh_worker("refresh group", [subgroup.subscription for subgroup in self.active_group.subgroups if subgroup.subscription])

        def action_refresh_subgroup(self) -> None:
            if self.active_subgroup and self.active_subgroup.subscription:
                self.start_refresh_worker("refresh subgroup", [self.active_subgroup.subscription])

        def start_refresh_worker(self, name: str, subscriptions: Sequence[Subscription | None]) -> None:
            targets = [subscription for subscription in subscriptions if subscription is not None]
            if not targets:
                self.update_status("nothing to refresh")
                return
            if not self.begin_operation(name, f"{name}: 0/{len(targets)}"):
                return
            self.run_worker(lambda: self.refresh_worker(name, targets), name=name, group="xray-actions", thread=True, exit_on_error=False)

        def refresh_worker(self, name: str, subscriptions: Sequence[Subscription]) -> None:
            try:
                for index, subscription in enumerate(subscriptions, start=1):
                    self.call_from_thread(self.update_status, f"{name}: {index}/{len(subscriptions)} {subscription.name}")
                    refresh_subscription(subscription, timeout=self.options.timeout)
            except Exception as exc:
                self.call_from_thread(self.operation_failed, name, exc)
                return
            self.call_from_thread(self.finish_refresh, name)

        def finish_refresh(self, name: str) -> None:
            self.reload_config()
            self.finish_operation(f"{name} complete")

        def action_test_sampled(self) -> None:
            items = self.current_items()
            if not items:
                self.update_status("no configs to test")
                return
            if not self.begin_operation("test", f"testing {min(len(items), self.options.sample or len(items))} config(s): 0 done"):
                return
            self.run_worker(
                lambda: self.test_sampled_worker(items, self.current_proxy_chain()),
                name="test-sampled",
                group="xray-actions",
                thread=True,
                exit_on_error=False,
            )

        def test_sampled_worker(self, items: Sequence[ConfigItem], proxy_chain: ProxyChain | None) -> None:
            try:
                result = rotate_configs(
                    items,
                    self.options,
                    proxy_chain=proxy_chain,
                    progress=lambda done, total, result: self.call_from_thread(self.update_test_progress, done, total, result),
                )
            except Exception as exc:
                self.call_from_thread(self.operation_failed, "test", exc)
                return
            self.call_from_thread(self.finish_test_sampled, list(items), result)

        def update_test_progress(self, done: int, total: int, result: dict[str, Any] | None) -> None:
            suffix = ""
            if result is not None and "best-delay-ms" in result:
                suffix = f" | latest ok {result['best-delay-ms']} ms"
            self.update_status(f"testing configs: {done}/{total}{suffix}")

        def finish_test_sampled(self, items: Sequence[ConfigItem], result: dict[str, Any] | None) -> None:
            if result is None:
                self.finish_operation("no working config")
                return
            config_id = result.get("config-id")
            if not isinstance(config_id, str):
                self.finish_operation("test result missing config id")
                return
            active = find_config(items, config_id)
            if active is None:
                self.finish_operation("tested config disappeared")
                return
            test_results = dict(self.state.test_results)
            test_results[config_id] = result
            self.state = TuiState(
                active_group_id=self.active_group.id if self.active_group else None,
                active_subgroup_id=self.active_subgroup.id if self.active_subgroup else None,
                active_config_id=self.state.active_config_id,
                test_results=test_results,
            )
            save_state(self.state)
            self.populate_table()
            self.update_status(f"selected fastest {active.name}: {result['best-delay-ms']} ms; starting")
            self.start_tested_item(active)

        def action_restart_xray(self) -> None:
            active = find_config(flatten_group(self.active_group), self.state.active_config_id) if self.active_group else None
            if active:
                if not self.begin_operation("restart", f"restarting {active.name}"):
                    return
                self.set_row_status(active, "starting")
                proxy_chain = self.current_proxy_chain()
                self.run_worker(lambda: self.start_item_worker(active, proxy_chain, "restart"), name="restart-config", group="xray-actions", thread=True, exit_on_error=False)

        def action_show_tmux_info(self) -> None:
            self.update_status(self.runner.attach_command())

        def action_previous_subgroup(self) -> None:
            self.move_subgroup(-1)

        def action_next_subgroup(self) -> None:
            self.move_subgroup(1)

        def action_previous_group(self) -> None:
            self.move_group(-1)

        def action_next_group(self) -> None:
            self.move_group(1)

        def move_group(self, delta: int) -> None:
            if not self.tui_config.groups or not self.active_group:
                return
            index = self.tui_config.groups.index(self.active_group)
            self.active_group = self.tui_config.groups[(index + delta) % len(self.tui_config.groups)]
            self.active_subgroup = find_subgroup(self.active_group, None)
            self.populate_table()
            self.query_one("#table", DataTable).focus()
            self.update_tabs()
            self.update_status()

        def move_subgroup(self, delta: int) -> None:
            if not self.active_group or not self.active_group.subgroups or not self.active_subgroup:
                return
            index = self.active_group.subgroups.index(self.active_subgroup)
            self.active_subgroup = self.active_group.subgroups[(index + delta) % len(self.active_group.subgroups)]
            self.populate_table()
            self.query_one("#table", DataTable).focus()
            self.update_tabs()
            self.update_status()

        def action_focus_or_collapse_navigation(self) -> None:
            self.query_one("#nav", Tree).focus()

        def action_focus_or_collapse_navigation_alt(self) -> None:
            self.action_focus_or_collapse_navigation()

        def action_focus_or_expand_table(self) -> None:
            self.query_one("#table", DataTable).focus()

        def action_focus_or_expand_table_alt(self) -> None:
            self.action_focus_or_expand_table()

        def action_focus_next(self) -> None:
            self.screen.focus_next()

        def action_focus_previous(self) -> None:
            self.screen.focus_previous()

        def action_filter(self) -> None:
            self.update_status("filter entry is planned")

        def maybe_auto_rotate(self) -> None:
            if self.auto_rotate and self.busy is None:
                self.action_test_sampled()

        def refresh_stale_selected_scope(self) -> None:
            if self.active_subgroup and self.active_subgroup.subscription and subscription_needs_refresh(
                self.active_subgroup.subscription, self.options.sub_auto_refresh
            ):
                refresh_subscription(self.active_subgroup.subscription, timeout=self.options.timeout)
                self.tui_config = load_tui_config(self.options.config, state=self.state)
                self.active_group = find_group(self.tui_config, self.state.active_group_id)
                self.active_subgroup = find_subgroup(self.active_group, self.state.active_subgroup_id) if self.active_group else None

        def start_last_active(self) -> None:
            if not self.active_group or not self.state.active_config_id:
                return
            active = find_config(flatten_group(self.active_group), self.state.active_config_id)
            if active is None:
                return
            if not self.begin_operation("start", f"restoring active {active.name}"):
                return
            self.set_row_status(active, "starting")
            proxy_chain = self.current_proxy_chain()
            self.run_worker(lambda: self.start_item_worker(active, proxy_chain, "restore"), name="restore-active-config", group="xray-actions", thread=True, exit_on_error=False)

        def refresh_group(self, group: ConfigGroup) -> None:
            for subgroup in group.subgroups:
                if subgroup.subscription:
                    refresh_subscription(subgroup.subscription, timeout=self.options.timeout)

        def reload_config(self) -> None:
            self.tui_config = load_tui_config(self.options.config, state=self.state)
            self.active_group = find_group(self.tui_config, self.active_group.id if self.active_group else None)
            self.active_subgroup = find_subgroup(self.active_group, self.active_subgroup.id if self.active_group and self.active_subgroup else None) if self.active_group else None
            self.populate_tree()
            self.populate_table()
            self.update_tabs()

    app = XrayTuiApp()
    app.title = "xray-tui"
    for theme in [
        Theme(name="xray-dark", primary="#7dd3fc", secondary="#a7f3d0", accent="#fbbf24", foreground="#e5e7eb", background="#111827", surface="#1f2937", panel="#172033", dark=True),
        Theme(name="xray-graphite", primary="#93c5fd", secondary="#c4b5fd", accent="#fca5a5", foreground="#f3f4f6", background="#18181b", surface="#27272a", panel="#202025", dark=True),
        Theme(name="xray-forest", primary="#86efac", secondary="#67e8f9", accent="#fde68a", foreground="#ecfdf5", background="#10201a", surface="#183027", panel="#132820", dark=True),
        Theme(name="xray-light", primary="#2563eb", secondary="#059669", accent="#d97706", foreground="#111827", background="#f8fafc", surface="#ffffff", panel="#eef2f7", dark=False),
        Theme(name="xray-paper", primary="#0f766e", secondary="#7c3aed", accent="#c2410c", foreground="#1f2937", background="#fffbeb", surface="#fffdf5", panel="#f3ead2", dark=False),
        Theme(name="xray-mono", primary="#374151", secondary="#0e7490", accent="#be123c", foreground="#111827", background="#f9fafb", surface="#ffffff", panel="#e5e7eb", dark=False),
    ]:
        app.register_theme(theme)
    app.theme = options.dark_theme if dark_mode_enabled(options.dark_mode) else options.light_theme
    app.run()
    return 0


def textual_key_name(key: str) -> str:
    if key == "space":
        return "SPC"
    return key


def main(argv: Sequence[str] | None = None) -> int:
    try:
        options = parse_tui_args(sys.argv[1:] if argv is None else argv)
        startup_log(f"using config: {options.config}")
        created = ensure_example_config(options.config)
        if created:
            print_created_config_help(options.config)
            return 0
        startup_log("loading saved state")
        state = load_state()
        startup_log("loading config and cached subscriptions")
        config = load_tui_config(options.config, state=state)
        group_count, subscription_count, config_count = count_tui_config(config)
        startup_log(f"loaded {group_count} group(s), {subscription_count} subscription(s), {config_count} config(s)")
        config = startup_refresh_stale_selected_scope(config, state, options)
        group_count, subscription_count, config_count = count_tui_config(config)
        startup_log(f"ready with {group_count} group(s), {subscription_count} subscription(s), {config_count} config(s)")
        startup_log("loading hotkeys")
        hotkeys = load_hotkeys(options.python_config)
        startup_log("starting TUI")
        return run_textual_app(config, state, options, hotkeys)
    except BrokenPipeError:
        return 1
    except Exception as exc:
        print(f"xray-tui: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
