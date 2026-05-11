from __future__ import annotations

import pytest
import socksio

from gost_trier.cli import (
    expand_configs,
    has_listen_args,
    parse_args,
    parse_duration,
    strip_listen_args,
    substitute_placeholders,
    tmux_command,
)


def test_socks_dependency_is_installed():
    assert socksio.__version__


def test_parse_args_strips_separator_and_defaults():
    options = parse_args(["trojan.txt", "--", "-L=socks5://127.0.0.1:1050", "-F=MAGIC_FILE_1"])

    assert [path.name for path in options.files] == ["trojan.txt"]
    assert options.gost_args == ["-L=socks5://127.0.0.1:1050", "-F=MAGIC_FILE_1"]
    assert options.test_urls == ["https://myip.wtf/json"]
    assert options.timeout == 20.0
    assert options.jobs == 1


def test_parse_args_allows_help_without_separator():
    with pytest.raises(SystemExit) as exc_info:
        parse_args(["--help"])

    assert exc_info.value.code == 0


def test_duration_parsing():
    assert parse_duration("20s") == 20.0
    assert parse_duration("500ms") == 0.5
    assert parse_duration("1m") == 60.0
    assert parse_duration("3") == 3.0


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
    configs = list(expand_configs(["-F=MAGIC_FILE_1", "-F=MAGIC_FILE_2"], [["a", "b"], ["x", "y"]]))

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
