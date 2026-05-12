from __future__ import annotations

import base64
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import HTTPError

import pytest
import socksio

from gost_trier.common import (
    TrierOptions,
    candidate_lines,
    decode_base64_if_needed,
    expand_configs,
    is_http_url,
    normalize_split_url_args,
    parse_duration,
    parse_trier_args,
    progress_message,
    run_trier,
    sample_iterable,
)
from gost_trier.downloads import (
    content_length,
    download_bytes,
    download_file,
    download_proxy_for_url,
    print_proxy_notice,
    redact_proxy_url,
)
from gost_trier.gost import (
    has_listen_args,
    listen_args,
    listener_curl_command as gost_listener_curl_command,
    run_gost_in_tmux,
    strip_listen_args,
    tmux_command,
    tmux_install_commands,
)
from gost_trier.placeholders import substitute_placeholders
from gost_trier.xray import (
    XrayArgs,
    build_xray_config,
    build_inbound,
    convert_link_to_outbounds,
    listener_curl_command,
    listener_proxy_url,
    normalize_outbound,
    parse_listen,
    parse_converter_json,
    parse_xray_args,
    preflight_xray_trier,
    print_xray_tmux_launch_info,
    run_xray_in_tmux,
    run_xray_test,
    smoke_test_converter,
    smoke_test_xray,
    validate_xray_config,
    xray_run_main,
    xray_tmux_command,
)
from gost_trier.native import (
    Release,
    ReleaseAsset,
    executable_suffix,
    find_cached_executable,
    latest_release,
    resolve_release_binary,
    update_release_binary,
    xray_asset_name,
    xray_link_json_asset_name,
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
    assert options.output == "-"
    assert options.progress


def test_parse_trier_args_normalizes_shell_split_runner_urls():
    options = parse_trier_args(
        ["trojan.txt", "--", "-L=socks5:", "//127.0.0.1:1050", "-F=MAGIC_FILE_1"],
        prog="xray-trier",
        description="test",
    )

    assert options.runner_args == ["-L=socks5://127.0.0.1:1050", "-F=MAGIC_FILE_1"]


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


def test_parse_trier_args_accepts_repeatable_verbose():
    options = parse_trier_args(["-vv", "trojan.txt", "--", "-F=MAGIC_FILE_1"], prog="xray-trier", description="test")

    assert options.verbose == 2


def test_parse_trier_args_accepts_output():
    options = parse_trier_args(
        ["--output=results/out.json", "trojan.txt", "--", "-F=MAGIC_FILE_1"],
        prog="xray-trier",
        description="test",
    )

    assert options.output == "results/out.json"


def test_parse_trier_args_accepts_no_progress():
    options = parse_trier_args(
        ["--no-progress", "trojan.txt", "--", "-F=MAGIC_FILE_1"],
        prog="xray-trier",
        description="test",
    )

    assert not options.progress


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


def test_listen_args_extracts_joined_and_split_forms():
    assert listen_args(["-L=socks5://127.0.0.1:1080", "-L", "http://127.0.0.1:2080", "-F=x"]) == [
        "socks5://127.0.0.1:1080",
        "http://127.0.0.1:2080",
    ]


def test_normalize_split_url_args_joins_shell_split_urls():
    assert normalize_split_url_args(["-L=socks5:", "//127.0.0.1:1080", "-F=vless:", "//example.com"]) == [
        "-L=socks5://127.0.0.1:1080",
        "-F=vless://example.com",
    ]


def test_strip_listen_args_removes_shell_split_listener_tail():
    assert strip_listen_args(["-L=socks5:", "//127.0.0.1:1080", "-F=direct://"]) == ["-F=direct://"]


def test_gost_listener_curl_command_uses_listener_scheme():
    assert (
        gost_listener_curl_command("http://user:pass@:2080")
        == "curl --proxy http://user:pass@127.0.0.1:2080 https://api.ipify.org"
    )


def test_gost_listener_curl_command_uses_curl_exe_on_windows(monkeypatch):
    monkeypatch.setattr("platform.system", lambda: "Windows")

    assert gost_listener_curl_command("http://user:pass@:2080").startswith("curl.exe --proxy ")


def test_gost_run_in_tmux_falls_back_to_managed_session(monkeypatch, capsys):
    launched = []

    def fake_run_managed_session(session, processes):
        launched.append((session, processes))

    monkeypatch.setattr("gost_trier.gost.ensure_tmux_session", lambda session: (_ for _ in ()).throw(RuntimeError("no tmux")))
    monkeypatch.setattr("gost_trier.gost.run_managed_session", fake_run_managed_session)

    run_gost_in_tmux("fallback", [{"best-delay-ms": 1, "config": ["-L=socks5://127.0.0.1:1080", "-F=x"]}], 1)

    assert launched == [("fallback", [(["gost", "-L=socks5://127.0.0.1:1080", "-F=x"], ["socks5://127.0.0.1:1080"])])]
    err = capsys.readouterr().err
    assert "runner: managed detached processes" in err
    assert "curl --proxy socks5h://127.0.0.1:1080 https://api.ipify.org" in err


def test_tmux_install_commands_include_windows_options(monkeypatch):
    monkeypatch.setattr("platform.system", lambda: "Windows")

    commands = tmux_install_commands()

    assert any(command[0] == "scoop" for command in commands)
    assert any(command[0] == "choco" for command in commands)
    assert any(command[0] == "winget" for command in commands)


def test_xray_link_json_asset_names():
    assert xray_link_json_asset_name("v0.1.0", "linux", "amd64") == "Xray-Link-Json_v0.1.0_linux_amd64.tar.gz"
    assert xray_link_json_asset_name("v0.1.0", "windows", "arm64") == "Xray-Link-Json_v0.1.0_windows_arm64.zip"


def test_xray_asset_names():
    assert xray_asset_name("v26.3.27", "linux", "amd64") == "Xray-linux-64.zip"
    assert xray_asset_name("v26.3.27", "linux", "arm64") == "Xray-linux-arm64-v8a.zip"
    assert xray_asset_name("v26.3.27", "darwin", "arm64") == "Xray-macos-arm64-v8a.zip"
    assert xray_asset_name("v26.3.27", "windows", "amd64") == "Xray-windows-64.zip"


def test_content_length_handles_missing_and_invalid_values():
    class Response:
        headers = {}

    assert content_length(Response()) is None
    Response.headers = {"Content-Length": "bad"}
    assert content_length(Response()) is None
    Response.headers = {"Content-Length": "-1"}
    assert content_length(Response()) is None
    Response.headers = {"Content-Length": "123"}
    assert content_length(Response()) == 123


def test_download_file_streams_with_progress(monkeypatch, tmp_path):
    updates = []

    class Response:
        headers = {"Content-Length": "6"}

        def __init__(self):
            self.chunks = [b"abc", b"def", b""]

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self, size):
            assert size > 0
            return self.chunks.pop(0)

    class Progress:
        def __init__(self, **kwargs):
            assert kwargs["total"] == 6

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def update(self, size):
            updates.append(size)

    monkeypatch.setattr("gost_trier.downloads.urlopen", lambda request, timeout: Response())
    monkeypatch.setattr("gost_trier.downloads.tqdm", Progress)
    monkeypatch.setattr("gost_trier.downloads.print_proxy_notice", lambda url: None)

    destination = tmp_path / "archive.zip"
    download_file("https://example.com/archive.zip", destination)

    assert destination.read_bytes() == b"abcdef"
    assert updates == [3, 3]


def test_download_bytes_error_includes_url(monkeypatch):
    def fake_urlopen(request, timeout):
        raise HTTPError(request.full_url, 403, "rate limit exceeded", {}, None)

    monkeypatch.setattr("gost_trier.downloads.urlopen", fake_urlopen)
    monkeypatch.setattr("gost_trier.downloads.print_proxy_notice", lambda url: None)

    with pytest.raises(RuntimeError, match=r"failed to download https://example\.com/sub\.txt: HTTP 403: rate limit exceeded"):
        download_bytes("https://example.com/sub.txt")


def test_latest_release_error_includes_url(monkeypatch):
    latest_release.cache_clear()

    def fake_urlopen(request, timeout):
        raise HTTPError(request.full_url, 403, "rate limit exceeded", {}, None)

    monkeypatch.setattr("gost_trier.native.urlopen", fake_urlopen)

    with pytest.raises(
        RuntimeError,
        match=r"failed to fetch latest release metadata from https://api\.github\.com/repos/XTLS/Xray-core/releases/latest: HTTP 403: rate limit exceeded",
    ):
        latest_release("XTLS/Xray-core")

    latest_release.cache_clear()


def test_download_proxy_for_url_respects_proxy_and_bypass(monkeypatch):
    monkeypatch.setattr("gost_trier.downloads.getproxies", lambda: {"https": "http://127.0.0.1:8080"})
    monkeypatch.setattr("gost_trier.downloads.proxy_bypass", lambda host: host == "localhost")

    assert download_proxy_for_url("https://example.com/file.zip") == "http://127.0.0.1:8080"
    assert download_proxy_for_url("https://localhost/file.zip") is None


def test_proxy_notice_prints_once_and_redacts_credentials(monkeypatch, capsys):
    monkeypatch.setattr("gost_trier.downloads._PROXY_NOTICE_PRINTED", False)
    monkeypatch.setattr("gost_trier.downloads.download_proxy_for_url", lambda url: "http://user:pass@127.0.0.1:8080")

    print_proxy_notice("https://example.com/one.zip")
    print_proxy_notice("https://example.com/two.zip")

    assert capsys.readouterr().err == "Using proxy for downloads: http://***:***@127.0.0.1:8080\n"


def test_redact_proxy_url_leaves_proxy_without_credentials():
    assert redact_proxy_url("http://127.0.0.1:8080") == "http://127.0.0.1:8080"
    assert redact_proxy_url("socks5://user:pass@example.com:1080") == "socks5://***:***@example.com:1080"


def test_executable_suffix_uses_windows_extension():
    assert executable_suffix("windows") == ".exe"
    assert executable_suffix("linux") == ""


def test_find_cached_executable_uses_existing_platform_cache(tmp_path):
    binary = tmp_path / "v1.0.0" / "linux-amd64" / "xray"
    binary.parent.mkdir(parents=True)
    binary.write_text("binary")

    assert find_cached_executable(tmp_path, "linux", "amd64", ["xray"]) == binary
    assert find_cached_executable(tmp_path, "windows", "amd64", ["xray.exe"]) is None


def test_resolve_release_binary_uses_existing_cache_before_github(monkeypatch, tmp_path):
    binary = tmp_path / "bin" / "xray" / "v1.0.0" / "linux-amd64" / "xray"
    binary.parent.mkdir(parents=True)
    binary.write_text("binary")

    monkeypatch.setattr("gost_trier.native.DEFAULT_CACHE_ROOT", tmp_path)
    monkeypatch.setattr("gost_trier.native.system_arch", lambda: ("linux", "amd64"))
    monkeypatch.setattr("gost_trier.native.shutil.which", lambda name: None)
    monkeypatch.setattr("gost_trier.native.latest_release", lambda repo: (_ for _ in ()).throw(AssertionError("unexpected GitHub request")))

    resolved = resolve_release_binary(
        tool="xray",
        repo="XTLS/Xray-core",
        executable_names=["xray"],
        asset_name=lambda tag, goos, goarch: "xray.zip",
    )

    assert resolved == binary


def test_resolve_release_binary_prefers_env_over_cache_and_path(monkeypatch, tmp_path):
    cached = tmp_path / "bin" / "xray" / "v1.0.0" / "linux-amd64" / "xray"
    cached.parent.mkdir(parents=True)
    cached.write_text("cached")
    env_binary = tmp_path / "env-xray"
    path_binary = tmp_path / "path-xray"

    monkeypatch.setenv("XRAY_BIN", str(env_binary))
    monkeypatch.setattr("gost_trier.native.DEFAULT_CACHE_ROOT", tmp_path)
    monkeypatch.setattr("gost_trier.native.system_arch", lambda: ("linux", "amd64"))
    monkeypatch.setattr("gost_trier.native.shutil.which", lambda name: str(path_binary))

    resolved = resolve_release_binary(
        tool="xray",
        repo="XTLS/Xray-core",
        executable_names=["xray"],
        asset_name=lambda tag, goos, goarch: "xray.zip",
        env_var="XRAY_BIN",
    )

    assert resolved == env_binary


def test_resolve_release_binary_prefers_cache_over_path(monkeypatch, tmp_path):
    cached = tmp_path / "bin" / "xray" / "v1.0.0" / "linux-amd64" / "xray"
    cached.parent.mkdir(parents=True)
    cached.write_text("cached")
    path_binary = tmp_path / "path-xray"

    monkeypatch.setattr("gost_trier.native.DEFAULT_CACHE_ROOT", tmp_path)
    monkeypatch.setattr("gost_trier.native.system_arch", lambda: ("linux", "amd64"))
    monkeypatch.setattr("gost_trier.native.shutil.which", lambda name: str(path_binary))
    monkeypatch.setattr("gost_trier.native.latest_release", lambda repo: (_ for _ in ()).throw(AssertionError("unexpected GitHub request")))

    resolved = resolve_release_binary(
        tool="xray",
        repo="XTLS/Xray-core",
        executable_names=["xray"],
        asset_name=lambda tag, goos, goarch: "xray.zip",
    )

    assert resolved == cached


def test_update_release_binary_no_download_reports_latest_without_installing(monkeypatch, tmp_path):
    monkeypatch.setattr("gost_trier.native.DEFAULT_CACHE_ROOT", tmp_path)
    monkeypatch.setattr("gost_trier.native.system_arch", lambda: ("linux", "amd64"))
    monkeypatch.setattr(
        "gost_trier.native.latest_release",
        lambda repo: Release(
            tag="v2.0.0",
            assets=[ReleaseAsset(name="xray.zip", download_url="https://example.com/xray.zip")],
        ),
    )
    monkeypatch.setattr("gost_trier.native.install_release_asset", lambda **kwargs: (_ for _ in ()).throw(AssertionError("unexpected download")))

    result = update_release_binary(
        tool="xray",
        repo="XTLS/Xray-core",
        executable_names=["xray"],
        asset_name=lambda tag, goos, goarch: "xray.zip",
        no_download=True,
    )

    assert result.tag == "v2.0.0"
    assert result.asset_name == "xray.zip"
    assert result.cached is None
    assert result.installed is None
    assert result.download_url == "https://example.com/xray.zip"


def test_update_release_binary_downloads_missing_latest(monkeypatch, tmp_path):
    installed = tmp_path / "bin" / "xray" / "v2.0.0" / "linux-amd64" / "xray"

    monkeypatch.setattr("gost_trier.native.DEFAULT_CACHE_ROOT", tmp_path)
    monkeypatch.setattr("gost_trier.native.system_arch", lambda: ("linux", "amd64"))
    monkeypatch.setattr(
        "gost_trier.native.latest_release",
        lambda repo: Release(
            tag="v2.0.0",
            assets=[ReleaseAsset(name="xray.zip", download_url="https://example.com/xray.zip")],
        ),
    )
    monkeypatch.setattr("gost_trier.native.install_release_asset", lambda **kwargs: installed)

    result = update_release_binary(
        tool="xray",
        repo="XTLS/Xray-core",
        executable_names=["xray"],
        asset_name=lambda tag, goos, goarch: "xray.zip",
    )

    assert result.installed == installed
    assert result.cached is None


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

    def fake_run_test(config, test_urls, timeout, verbose=0):
        assert verbose == 2
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
        verbose=2,
        output="-",
        progress=True,
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


def test_run_trier_writes_output_file_and_creates_parent_dirs(tmp_path, capsys):
    output = tmp_path / "nested" / "results.json"
    options = TrierOptions(
        files=[],
        runner_args=["-F=MAGIC_FILE_1"],
        test_urls=["https://example.com"],
        shuffle=False,
        timeout=1,
        jobs=1,
        enough_delay_ms=None,
        sample=None,
        run_in_tmux=None,
        run_top=1,
        verbose=0,
        output=str(output),
        progress=True,
    )

    import gost_trier.common as common

    original = common.read_candidate_files
    common.read_candidate_files = lambda files, shuffle: [["a"]]
    try:
        run_trier(
            options,
            substitute=lambda args, values: [values[0]],
            run_test=lambda config, test_urls, timeout, verbose=0: {"best-delay-ms": 100, "config": config, "tests": []},
            run_tmux=lambda session, results, top: None,
        )
    finally:
        common.read_candidate_files = original

    assert capsys.readouterr().out == ""
    assert output.read_text() == '[\n  {\n    "best-delay-ms": 100,\n    "config": [\n      "a"\n    ],\n    "tests": []\n  }\n]\n'


def test_run_trier_runs_preflight_before_parallel_workers(capsys):
    events = []
    options = TrierOptions(
        files=[],
        runner_args=["-F=MAGIC_FILE_1"],
        test_urls=["https://example.com"],
        shuffle=False,
        timeout=1,
        jobs=2,
        enough_delay_ms=None,
        sample=None,
        run_in_tmux=None,
        run_top=1,
        verbose=0,
        output="-",
        progress=True,
    )

    import gost_trier.common as common

    original = common.read_candidate_files
    common.read_candidate_files = lambda files, shuffle: [["a", "b"]]
    try:
        run_trier(
            options,
            substitute=lambda args, values: [values[0]],
            run_test=lambda config, test_urls, timeout, verbose=0: events.append(("worker", config)) or None,
            run_tmux=lambda session, results, top: None,
            preflight=lambda seen_options: events.append(("preflight", seen_options.jobs)),
        )
    finally:
        common.read_candidate_files = original

    assert events[0] == ("preflight", 2)
    assert {event[0] for event in events[1:]} == {"worker"}
    assert capsys.readouterr().out == "[]\n"


def test_progress_message_prints_success_or_failure_and_delay():
    assert progress_message(1, 3, None) == "[1/3] fail"
    assert progress_message(2, 3, {"best-delay-ms": 123}) == "[2/3] success 123 ms"


def test_tmux_command_quotes_config():
    command = tmux_command("sess", "gost-1", ["-L=socks5://127.0.0.1:5000", "-F=a b"])

    assert command[:5] == ["tmux", "new-window", "-t", "sess", "-n"]
    assert command[5] == "gost-1"
    assert command[6] == "gost -L=socks5://127.0.0.1:5000 '-F=a b'"


def test_parse_xray_args_accepts_gost_like_flags():
    parsed = parse_xray_args(["-L=socks5://127.0.0.1:1050", "-F=first", "-F", "second"])

    assert parsed.listens == [parse_listen("socks5://127.0.0.1:1050")]
    assert parsed.forwards == ["first", "second"]


def test_parse_xray_args_accepts_shell_split_urls():
    parsed = parse_xray_args(["-L=socks5:", "//127.0.0.1:1050", "-F=vless:", "//example.com"], auto_listen=False)

    assert parsed.listens == [parse_listen("socks5://127.0.0.1:1050")]
    assert parsed.forwards == ["vless://example.com"]


def test_xray_run_main_normalizes_shell_split_urls(monkeypatch):
    seen = {}

    def fake_xray_run_json(args, validate=True, verbose=0):
        seen["args"] = args
        return {"outbounds": []}

    monkeypatch.setattr("gost_trier.xray.xray_run_json", fake_xray_run_json)

    assert xray_run_main(["json", "-L=socks5:", "//127.0.0.1:1050", "-F=direct://"]) == 0
    assert seen["args"] == ["-L=socks5://127.0.0.1:1050", "-F=direct://"]


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


def test_xray_listener_curl_command_uses_curl_exe_on_windows(monkeypatch):
    monkeypatch.setattr("platform.system", lambda: "Windows")

    assert listener_curl_command(parse_listen("socks5://127.0.0.1:1050")).startswith("curl.exe --proxy ")


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


def test_convert_link_failure_includes_verbose_subprocess_details(monkeypatch):
    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 9, stdout="converter stdout", stderr="converter stderr")

    monkeypatch.setattr("subprocess.run", fake_run)

    with pytest.raises(ValueError) as exc_info:
        convert_link_to_outbounds("vless://bad", converter=["Xray-Link-Json"], verbose=2)

    message = str(exc_info.value)
    assert "Xray-Link-Json failed to convert one forward" in message
    assert "return code: 9" in message
    assert "converter stdout" in message
    assert "converter stderr" in message


def test_validate_xray_config_failure_includes_validator_output(monkeypatch):
    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 23, stdout="validator stdout", stderr="validator stderr")

    monkeypatch.setattr("gost_trier.xray.ensure_xray_dependency", lambda verbose=0: "xray")
    monkeypatch.setattr("subprocess.run", fake_run)

    with pytest.raises(ValueError) as exc_info:
        validate_xray_config({"outbounds": [{"protocol": "freedom", "settings": {}}]}, verbose=2)

    message = str(exc_info.value)
    assert "generated Xray config failed validation" in message
    assert "xray run -test -c" in message
    assert "validator stdout" in message
    assert "validator stderr" in message


def test_validate_xray_config_closes_temp_file_before_xray_reads_it(monkeypatch):
    seen_paths = []

    def fake_run(command, **kwargs):
        path = Path(command[-1])
        assert path.read_text()
        seen_paths.append(path)
        return subprocess.CompletedProcess(command, 0, stdout="Configuration OK.\n", stderr="")

    monkeypatch.setattr("gost_trier.xray.ensure_xray_dependency", lambda verbose=0: "xray")
    monkeypatch.setattr("subprocess.run", fake_run)

    validate_xray_config({"outbounds": [{"protocol": "freedom", "settings": {}}]}, verbose=2)

    assert seen_paths
    assert not seen_paths[0].exists()


def test_run_xray_test_strips_shell_split_original_listeners(monkeypatch):
    seen = {}

    monkeypatch.setattr("gost_trier.xray.free_port", lambda: 34567)
    monkeypatch.setattr("gost_trier.xray.ensure_xray_dependency", lambda verbose=0: "xray")

    def fake_xray_run_json(args, validate=True, verbose=0):
        seen["args"] = args
        raise ValueError("stop after setup args")

    monkeypatch.setattr("gost_trier.xray.xray_run_json", fake_xray_run_json)

    assert run_xray_test(["-L=socks5:", "//127.0.0.1:1080", "-F=direct://"], ["https://example.com"], 1) is None
    assert seen["args"] == ["-L=socks5://127.0.0.1:34567", "-F=direct://"]


def test_preflight_xray_trier_resolves_xray_and_converter(monkeypatch):
    calls = []
    options = TrierOptions(
        files=[],
        runner_args=["-F=MAGIC_FILE_1"],
        test_urls=["https://example.com"],
        shuffle=False,
        timeout=1,
        jobs=50,
        enough_delay_ms=None,
        sample=None,
        run_in_tmux=None,
        run_top=1,
        verbose=3,
        output="-",
        progress=True,
    )

    monkeypatch.setattr("gost_trier.xray.ensure_xray_dependency", lambda verbose=0: calls.append(("xray", verbose)) or "xray")
    monkeypatch.setattr("gost_trier.xray.locate_converter", lambda verbose=0: calls.append(("converter", verbose)) or ["Xray-Link-Json"])

    preflight_xray_trier(options)

    assert calls == [("xray", 3), ("converter", 3)]


def test_xray_run_main_passes_subcommand_verbose(monkeypatch, capsys):
    seen = {}

    def fake_xray_run_json(args, validate=True, verbose=0):
        seen["args"] = args
        seen["verbose"] = verbose
        return {"log": {"loglevel": "warning"}, "inbounds": [], "outbounds": []}

    monkeypatch.setattr("gost_trier.xray.xray_run_json", fake_xray_run_json)

    assert xray_run_main(["json", "-vvv", "-F=direct://"]) == 0
    assert seen == {"args": ["-F=direct://"], "verbose": 3}
    assert '"outbounds": []' in capsys.readouterr().out


def test_xray_run_main_counts_global_verbose(monkeypatch):
    seen = {}

    def fake_xray_run_json(args, validate=True, verbose=0):
        seen["verbose"] = verbose
        return {}

    monkeypatch.setattr("gost_trier.xray.xray_run_json", fake_xray_run_json)

    assert xray_run_main(["-v", "json", "-v", "-F=direct://"]) == 0
    assert seen["verbose"] == 2


def test_xray_run_main_accepts_no_progress_before_or_after_subcommand(monkeypatch, capsys):
    progress_values = []
    seen_args = []

    monkeypatch.setattr("gost_trier.xray.set_download_progress_enabled", progress_values.append)
    monkeypatch.setattr("gost_trier.xray.xray_run_json", lambda args, validate=True, verbose=0: seen_args.append(args) or {})

    assert xray_run_main(["--no-progress", "json", "-F=direct://"]) == 0
    assert xray_run_main(["json", "--no-progress", "-F=direct://"]) == 0

    assert progress_values == [False, False]
    assert seen_args == [["-F=direct://"], ["-F=direct://"]]
    assert capsys.readouterr().out == "{}\n{}\n"


def test_xray_run_main_update_binaries_accepts_no_download_and_no_progress(monkeypatch):
    progress_values = []
    update_calls = []

    monkeypatch.setattr("gost_trier.xray.set_download_progress_enabled", progress_values.append)
    monkeypatch.setattr("gost_trier.xray.update_binaries", lambda no_download=False: update_calls.append(no_download) or [])

    assert xray_run_main(["update-binaries", "--no-progress", "--no-download"]) == 0

    assert progress_values == [False]
    assert update_calls == [True]


def test_xray_run_main_writes_json_output_file(monkeypatch, tmp_path, capsys):
    output = tmp_path / "nested" / "xray-run-debug.json"
    monkeypatch.setattr("gost_trier.xray.xray_run_json", lambda args, validate=True, verbose=0: {"outbounds": []})

    assert xray_run_main(["json", "--output", str(output), "-F=direct://"]) == 0

    assert capsys.readouterr().out == ""
    assert output.read_text() == '{\n  "outbounds": []\n}\n'


def test_xray_run_main_accepts_stdout_output(monkeypatch, capsys):
    monkeypatch.setattr("gost_trier.xray.xray_run_json", lambda args, validate=True, verbose=0: {"outbounds": []})

    assert xray_run_main(["json", "-o", "-", "-F=direct://"]) == 0

    assert '"outbounds": []' in capsys.readouterr().out


def test_xray_dependency_smoke_test_checks_version_and_config(monkeypatch):
    commands = []
    seen_paths = []

    def fake_run(command, **kwargs):
        commands.append(command)
        if command[1] == "version":
            return subprocess.CompletedProcess(command, 0, stdout="Xray test-version\n", stderr="")
        path = Path(command[-1])
        assert path.read_text()
        seen_paths.append(path)
        return subprocess.CompletedProcess(command, 0, stdout="Configuration OK.\n", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr("gost_trier.xray._SMOKE_CHECKED", set())

    smoke_test_xray("xray", verbose=1)

    assert [command[:2] for command in commands] == [["xray", "version"], ["xray", "run"]]
    assert seen_paths
    assert not seen_paths[0].exists()


def test_xray_dependency_smoke_test_runs_once_for_concurrent_callers(monkeypatch):
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        time.sleep(0.01)
        if command[1] == "version":
            return subprocess.CompletedProcess(command, 0, stdout="Xray test-version\n", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="Configuration OK.\n", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr("gost_trier.xray._SMOKE_CHECKED", set())

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda _: smoke_test_xray("xray", verbose=1), range(8)))

    assert [command[:2] for command in commands] == [["xray", "version"], ["xray", "run"]]


def test_converter_smoke_test_requires_outbounds(monkeypatch):
    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, stdout='{"outbounds":[]}', stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr("gost_trier.xray._SMOKE_CHECKED", set())

    with pytest.raises(RuntimeError, match="did not emit outbounds"):
        smoke_test_converter(["Xray-Link-Json"], verbose=1)


def test_converter_smoke_test_runs_once_for_concurrent_callers(monkeypatch):
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        time.sleep(0.01)
        return subprocess.CompletedProcess(command, 0, stdout='{"outbounds":[{"protocol":"freedom"}]}', stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr("gost_trier.xray._SMOKE_CHECKED", set())

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda _: smoke_test_converter(["Xray-Link-Json"], verbose=1), range(8)))

    assert commands == [["Xray-Link-Json", "vless://00000000-0000-0000-0000-000000000000@example.com:443?security=tls&type=tcp&sni=example.com#smoke"]]


def test_build_xray_config_chains_outbounds(monkeypatch):
    def fake_convert(link, converter=None, verbose=0):
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
    def fake_convert(link, converter=None, verbose=0):
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


def test_print_xray_tmux_launch_info_includes_attach_and_curl(capsys):
    print_xray_tmux_launch_info("xray-1080", [["-L=socks5://127.0.0.1:1080", "-L=http://127.0.0.1:2080"]])

    err = capsys.readouterr().err
    assert "tmux session: xray-1080" in err
    assert "attach: tmux attach -t xray-1080" in err
    assert "curl --proxy socks5h://127.0.0.1:1080 https://api.ipify.org" in err
    assert "curl --proxy http://127.0.0.1:2080 https://api.ipify.org" in err


def test_print_xray_tmux_launch_info_uses_curl_exe_on_windows(monkeypatch, capsys):
    monkeypatch.setattr("platform.system", lambda: "Windows")

    print_xray_tmux_launch_info("xray-1080", [["-L=socks5://127.0.0.1:1080"]])

    err = capsys.readouterr().err
    assert "test xray: curl.exe --proxy socks5h://127.0.0.1:1080 https://api.ipify.org" in err
    assert "powershell" not in err.lower()


def test_xray_run_in_tmux_falls_back_to_managed_session(monkeypatch, capsys):
    launched = []

    def fake_run_managed_session(session, processes):
        launched.append((session, processes))

    monkeypatch.setattr("gost_trier.xray.ensure_tmux_session", lambda session: (_ for _ in ()).throw(RuntimeError("no tmux")))
    monkeypatch.setattr("gost_trier.xray.run_managed_session", fake_run_managed_session)

    run_xray_in_tmux("fallback", [{"best-delay-ms": 1, "config": ["-L=socks5://127.0.0.1:1080", "-F=direct://"]}], 1)

    assert launched == [
        (
            "fallback",
            [(["xray-run", "exec", "-L=socks5://127.0.0.1:1080", "-F=direct://"], ["socks5h://127.0.0.1:1080"])],
        )
    ]
    err = capsys.readouterr().err
    assert "runner: managed detached processes" in err
    assert "curl --proxy socks5h://127.0.0.1:1080 https://api.ipify.org" in err
