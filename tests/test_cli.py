from __future__ import annotations

import base64

import pytest
import socksio

from gost_trier.common import (
    candidate_lines,
    decode_base64_if_needed,
    expand_configs,
    is_http_url,
    parse_duration,
    parse_trier_args,
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


def test_tmux_command_quotes_config():
    command = tmux_command("sess", "gost-1", ["-L=socks5://127.0.0.1:5000", "-F=a b"])

    assert command[:5] == ["tmux", "new-window", "-t", "sess", "-n"]
    assert command[5] == "gost-1"
    assert command[6] == "gost -L=socks5://127.0.0.1:5000 '-F=a b'"


def test_parse_xray_args_accepts_gost_like_flags():
    parsed = parse_xray_args(["-L=socks5://127.0.0.1:1050", "-F=first", "-F", "second"])

    assert parsed.listens == [parse_listen("socks5://127.0.0.1:1050")]
    assert parsed.forwards == ["first", "second"]


def test_parse_xray_args_auto_listen(monkeypatch):
    monkeypatch.setattr("gost_trier.xray.free_port", lambda: 34567)

    parsed = parse_xray_args(["-F=first"])

    assert parsed.listens[0].host == "127.0.0.1"
    assert parsed.listens[0].port == 34567


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


def test_xray_tmux_command_uses_xray_run_exec():
    command = xray_tmux_command("sess", "xray-1", ["-L=socks5://127.0.0.1:5000", "-F=a b"])

    assert command[:6] == ["tmux", "new-window", "-t", "sess", "-n", "xray-1"]
    assert command[6] == "xray-run exec -L=socks5://127.0.0.1:5000 '-F=a b'"
