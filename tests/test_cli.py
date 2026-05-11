from __future__ import annotations

import base64

import pytest
import socksio

from gost_trier.common import (
    TrierOptions,
    candidate_lines,
    decode_base64_if_needed,
    expand_configs,
    is_http_url,
    parse_duration,
    parse_trier_args,
    run_trier,
    sample_iterable,
)
from gost_trier.gost import (
    has_listen_args,
    strip_listen_args,
    tmux_command,
)
from gost_trier.placeholders import substitute_placeholders
from gost_trier.xray import (
    XrayArgs,
    build_xray_config,
    build_inbound,
    listener_curl_command,
    listener_proxy_url,
    normalize_outbound,
    parse_listen,
    parse_converter_json,
    parse_xray_args,
    xray_tmux_command,
)


def test_socks_dependency_is_installed():
    assert socksio.__version__


def test_parse_args_strips_separator_and_defaults():
    options = parse_trier_args(
        ["trojan.txt", "--", "-L=socks5://127.0.0.1:1050", "-F=MAGIC_FILE_1"],
        prog="gost-trier",
        description="test",
    )

    assert options.files == ["trojan.txt"]
    assert options.runner_args == ["-L=socks5://127.0.0.1:1050", "-F=MAGIC_FILE_1"]
    assert options.test_urls == ["https://api.ipify.org", "https://myip.wtf/json"]
    assert options.timeout == 20.0
    assert options.jobs == 1


def test_parse_trier_args_accepts_custom_default_jobs():
    options = parse_trier_args(
        ["trojan.txt", "--", "-F=MAGIC_FILE_1"],
        prog="xray-trier",
        description="test",
        default_jobs=50,
    )

    assert options.jobs == 50


def test_parse_trier_args_accepts_enough_delay_and_sample():
    options = parse_trier_args(
        ["--enough-delay-ms=750", "--sample=10", "trojan.txt", "--", "-F=MAGIC_FILE_1"],
        prog="xray-trier",
        description="test",
    )

    assert options.enough_delay_ms == 750
    assert options.sample == 10
    assert options.shuffle


def test_parse_trier_args_preserves_url_sources():
    url = "https://raw.githubusercontent.com/example/repo/main/trojan.txt"
    options = parse_trier_args([url, "--", "-F=MAGIC_FILE_1"], prog="gost-trier", description="test")

    assert options.files == [url]
    assert is_http_url(options.files[0])


def test_parse_args_allows_help_without_separator():
    with pytest.raises(SystemExit) as exc_info:
        parse_trier_args(["--help"], prog="gost-trier", description="test")

    assert exc_info.value.code == 0


def test_duration_parsing():
    assert parse_duration("20s") == 20.0
    assert parse_duration("500ms") == 0.5
    assert parse_duration("1m") == 60.0
    assert parse_duration("3") == 3.0


def test_candidate_lines_filters_blank_and_comment_lines(tmp_path):
    path = tmp_path / "candidates.txt"
    path.write_text("\n# comment\n  # indented comment\n trojan://one \n\nvless://two\n")

    assert candidate_lines(str(path)) == ["trojan://one", "vless://two"]


def test_candidate_lines_decodes_base64_file(tmp_path):
    path = tmp_path / "subscription.txt"
    payload = "trojan://one\n# comment\nvless://two\n"
    path.write_text(base64.b64encode(payload.encode()).decode().rstrip("="))

    assert candidate_lines(str(path)) == ["trojan://one", "vless://two"]


def test_candidate_lines_downloads_url(monkeypatch):
    monkeypatch.setattr("gost_trier.common.download_source", lambda url: b"# comment\ntrojan://one\n")

    assert candidate_lines("https://example.com/list.txt") == ["trojan://one"]


def test_decode_base64_if_needed_leaves_normal_text():
    assert decode_base64_if_needed(b"trojan://one\n") == b"trojan://one\n"


def test_strip_listen_args_removes_joined_and_split_forms():
    assert strip_listen_args(["-D", "-L=socks5://a", "-L", "socks5://b", "-F=x"]) == ["-D", "-F=x"]


def test_has_listen_args_detects_joined_and_split_forms():
    assert has_listen_args(["-F=x", "-L=socks5://a"])
    assert has_listen_args(["-F=x", "-L", "socks5://a"])
    assert not has_listen_args(["-F=x"])


def test_substitute_placeholders():
    args = substitute_placeholders(["-F=MAGIC_FILE_1", "-F=x-MAGIC_FILE_2"], ["a", "b"])

    assert args == ["-F=a", "-F=x-b"]


def test_missing_placeholder_file_raises():
    with pytest.raises(ValueError):
        substitute_placeholders(["-F=MAGIC_FILE_2"], ["a"])


def test_expand_configs_uses_cartesian_product():
    configs = list(
        expand_configs(
            ["-F=MAGIC_FILE_1", "-F=MAGIC_FILE_2"],
            [["a", "b"], ["x", "y"]],
            substitute_placeholders,
        )
    )

    assert configs == [
        ["-F=a", "-F=x"],
        ["-F=a", "-F=y"],
        ["-F=b", "-F=x"],
        ["-F=b", "-F=y"],
    ]


def test_sample_iterable_limits_and_randomizes_size():
    items = [["-F=a"], ["-F=b"], ["-F=c"]]

    sampled = sample_iterable(iter(items), 2)

    assert len(sampled) == 2
    assert all(item in items for item in sampled)


def test_run_trier_stops_when_enough_delay_found(capsys):
    calls = []

    def fake_substitute(args, values):
        return [values[0]]

    def fake_run_test(config, test_urls, timeout):
        calls.append(config)
        return {"best-delay-ms": 100, "config": config, "tests": []}

    options = TrierOptions(
        files=[],
        runner_args=["-F=MAGIC_FILE_1"],
        test_urls=["https://example.com"],
        shuffle=False,
        timeout=1,
        jobs=1,
        enough_delay_ms=200,
        sample=None,
        run_in_tmux=None,
        run_top=1,
    )

    def fake_read_candidate_files(files, shuffle):
        return [["a", "b", "c"]]

    import gost_trier.common as common

    original = common.read_candidate_files
    common.read_candidate_files = fake_read_candidate_files
    try:
        run_trier(options, substitute=fake_substitute, run_test=fake_run_test, run_tmux=lambda session, results, top: None)
    finally:
        common.read_candidate_files = original

    assert calls == [["a"]]
    assert '"best-delay-ms": 100' in capsys.readouterr().out


def test_tmux_command_quotes_config():
    command = tmux_command("sess", "gost-1", ["-L=socks5://127.0.0.1:5000", "-F=a b"])

    assert command[:5] == ["tmux", "new-window", "-t", "sess", "-n"]
    assert command[5] == "gost-1"
    assert command[6] == "gost -L=socks5://127.0.0.1:5000 '-F=a b'"


def test_parse_xray_args_accepts_gost_like_flags():
    parsed = parse_xray_args(["-L=socks5://127.0.0.1:1050", "-F=first", "-F", "second"])

    assert parsed.listens == [parse_listen("socks5://127.0.0.1:1050")]
    assert parsed.forwards == ["first", "second"]


def test_parse_xray_args_accepts_multiple_listeners():
    parsed = parse_xray_args(["-L=socks5://127.0.0.1:1060", "-L=http://user:password@:2060", "-F=first"])

    assert parsed.listens == [
        parse_listen("socks5://127.0.0.1:1060"),
        parse_listen("http://user:password@:2060"),
    ]


def test_parse_listen_defaults_missing_host_to_all_interfaces():
    parsed = parse_listen("http://user:password@:2060")

    assert parsed.scheme == "http"
    assert parsed.host == "0.0.0.0"
    assert parsed.port == 2060
    assert parsed.username == "user"
    assert parsed.password == "password"


def test_parse_listen_allows_socks_auth():
    parsed = parse_listen("socks5://user:password@:1080")

    assert parsed.host == "0.0.0.0"
    assert parsed.username == "user"
    assert parsed.password == "password"


def test_parse_xray_args_auto_listen(monkeypatch):
    monkeypatch.setattr("gost_trier.xray.free_port", lambda: 34567)

    parsed = parse_xray_args(["-F=first"])

    assert parsed.listens[0].host == "127.0.0.1"
    assert parsed.listens[0].port == 34567


def test_parse_xray_args_defaults_to_direct_forward(monkeypatch):
    monkeypatch.setattr("gost_trier.xray.free_port", lambda: 34567)

    parsed = parse_xray_args(["-L=socks5://127.0.0.1:1050"])

    assert parsed.forwards == ["direct://"]


def test_listener_curl_command_uses_socks5h():
    command = listener_curl_command(parse_listen("socks5://127.0.0.1:1050"))

    assert command == "curl --proxy socks5h://127.0.0.1:1050 https://api.ipify.org"


def test_listener_curl_command_uses_http_auth_and_loopback_for_wildcard():
    command = listener_curl_command(parse_listen("http://user:password@:2060"))

    assert command == "curl --proxy http://user:password@127.0.0.1:2060 https://api.ipify.org"


def test_listener_proxy_url_percent_encodes_auth():
    proxy = listener_proxy_url(parse_listen("http://u%20s:p%40ss@:2060"))

    assert proxy == "http://u%20s:p%40ss@127.0.0.1:2060"


def test_build_inbound_for_http_auth():
    inbound = build_inbound(parse_listen("http://user:password@:2060"))

    assert inbound["listen"] == "0.0.0.0"
    assert inbound["port"] == 2060
    assert inbound["protocol"] == "http"
    assert inbound["settings"] == {"accounts": [{"user": "user", "pass": "password"}]}


def test_build_inbound_for_socks_auth():
    inbound = build_inbound(parse_listen("socks5://user:password@:1080"))

    assert inbound["protocol"] == "socks"
    assert inbound["settings"] == {
        "auth": "password",
        "udp": True,
        "accounts": [{"user": "user", "pass": "password"}],
    }


def test_normalize_outbound_removes_send_through():
    outbound = normalize_outbound({"protocol": "trojan", "sendThrough": "bad", "settings": {}})

    assert outbound == {"protocol": "trojan", "settings": {}}


def test_parse_converter_json_ignores_surrounding_output():
    assert parse_converter_json('warn\n{"outbounds": []}\nextra') == {"outbounds": []}


def test_build_xray_config_chains_outbounds(monkeypatch):
    def fake_convert(link, converter=None):
        return [{"protocol": "freedom", "settings": {"link": link}, "sendThrough": "ignored"}]

    monkeypatch.setattr("gost_trier.xray.convert_link_to_outbounds", fake_convert)
    args = XrayArgs(listens=[parse_listen("socks5://127.0.0.1:1050")], forwards=["first", "second"])

    config = build_xray_config(args)

    assert config["inbounds"][0]["port"] == 1050
    assert config["outbounds"][0]["tag"] == "proxy-1"
    assert config["outbounds"][0]["proxySettings"] == {"tag": "proxy-2"}
    assert config["outbounds"][1]["tag"] == "proxy-2"
    assert "proxySettings" not in config["outbounds"][1]
    assert "sendThrough" not in config["outbounds"][0]


def test_build_xray_config_creates_multiple_inbounds(monkeypatch):
    def fake_convert(link, converter=None):
        return [{"protocol": "freedom", "settings": {"link": link}}]

    monkeypatch.setattr("gost_trier.xray.convert_link_to_outbounds", fake_convert)
    args = XrayArgs(
        listens=[
            parse_listen("socks5://127.0.0.1:1060"),
            parse_listen("http://user:password@:2060"),
        ],
        forwards=["first"],
    )

    config = build_xray_config(args)

    assert [inbound["tag"] for inbound in config["inbounds"]] == ["in-1", "in-2"]
    assert [inbound["protocol"] for inbound in config["inbounds"]] == ["socks", "http"]
    assert config["routing"]["rules"][0]["inboundTag"] == ["in-1", "in-2"]


def test_xray_tmux_command_uses_xray_run_exec():
    command = xray_tmux_command("sess", "xray-1", ["-L=socks5://127.0.0.1:5000", "-F=a b"])

    assert command[:6] == ["tmux", "new-window", "-t", "sess", "-n", "xray-1"]
    assert command[6] == "xray-run exec -L=socks5://127.0.0.1:5000 '-F=a b'"
