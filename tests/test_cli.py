from __future__ import annotations

import base64
import json
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
from gost_trier.xray_tui import (
    DEFAULT_HOTKEYS,
    TuiOptions,
    TuiState,
    XraySessionManager,
    build_listens,
    choose_fastest,
    config_item_to_xray_config,
    ensure_example_config,
    layout_mode,
    link_display_name,
    load_hotkeys,
    load_tui_config,
    parse_tui_args,
    refresh_subscription,
    rotate_configs,
    sample_configs,
    stable_id,
    startup_refresh_stale_selected_scope,
    subscription_links_from_bytes,
    true_color_enabled,
)
from gost_trier.native import (
    Release,
    ReleaseAsset,
    external_deps,
    executable_suffix,
    find_cached_executable,
    latest_release,
    locate_xray_link_json,
    parse_semver_tag,
    resolve_release_binary,
    update_release_binary,
    version_satisfies,
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


def test_external_deps_declares_xray_link_json_minimum():
    external_deps.cache_clear()
    deps = external_deps()

    assert deps["Xray-Link-Json"]["repo"] == "NightMachinery/Xray-Link-Json"
    assert deps["Xray-Link-Json"]["env_var"] == "XRAY_LINK_JSON_BIN"
    assert deps["Xray-Link-Json"]["min_version"] == "v0.2.1"
    assert deps["xray"]["repo"] == "XTLS/Xray-core"


def test_version_satisfies_semver_tags():
    assert parse_semver_tag("Xray-Link-Json v0.2.1") == (0, 2, 1)
    assert version_satisfies("v0.2.1", "v0.2.1")
    assert version_satisfies("0.2.2", "v0.2.1")
    assert not version_satisfies("v0.2.0", "v0.2.1")
    assert not version_satisfies("Xray-Link-Json dev", "v0.2.1")


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


def test_find_cached_executable_skips_cache_below_min_version(tmp_path):
    old_binary = tmp_path / "v0.2.0" / "linux-amd64" / "Xray-Link-Json"
    old_binary.parent.mkdir(parents=True)
    old_binary.write_text("old")
    new_binary = tmp_path / "v0.2.1" / "linux-amd64" / "Xray-Link-Json"
    new_binary.parent.mkdir(parents=True)
    new_binary.write_text("new")

    assert (
        find_cached_executable(tmp_path, "linux", "amd64", ["Xray-Link-Json"], min_version="v0.2.1")
        == new_binary
    )


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


def test_resolve_release_binary_skips_old_path_binary_and_downloads(monkeypatch, tmp_path, capsys):
    path_binary = tmp_path / "path-Xray-Link-Json"
    installed = tmp_path / "installed-Xray-Link-Json"

    monkeypatch.setattr("gost_trier.native.DEFAULT_CACHE_ROOT", tmp_path)
    monkeypatch.setattr("gost_trier.native.system_arch", lambda: ("linux", "amd64"))
    monkeypatch.setattr("gost_trier.native.shutil.which", lambda name: str(path_binary))
    monkeypatch.setattr(
        "gost_trier.native.subprocess_run_version",
        lambda path: subprocess.CompletedProcess([str(path), "--version"], 0, stdout="Xray-Link-Json v0.2.0\n", stderr=""),
    )
    monkeypatch.setattr(
        "gost_trier.native.latest_release",
        lambda repo: Release(
            tag="v0.2.1",
            assets=[ReleaseAsset(name="Xray-Link-Json_v0.2.1_linux_amd64.tar.gz", download_url="https://example.com/xlj.tgz")],
        ),
    )
    monkeypatch.setattr("gost_trier.native.install_release_asset", lambda **kwargs: installed)

    resolved = resolve_release_binary(
        tool="Xray-Link-Json",
        repo="NightMachinery/Xray-Link-Json",
        executable_names=["Xray-Link-Json"],
        asset_name=xray_link_json_asset_name,
        min_version="v0.2.1",
    )

    assert resolved == installed
    assert "ignoring PATH binary" in capsys.readouterr().err


def test_resolve_release_binary_allows_dev_path_binary_with_warning(monkeypatch, tmp_path, capsys):
    path_binary = tmp_path / "path-Xray-Link-Json"

    monkeypatch.setattr("gost_trier.native.DEFAULT_CACHE_ROOT", tmp_path)
    monkeypatch.setattr("gost_trier.native.system_arch", lambda: ("linux", "amd64"))
    monkeypatch.setattr("gost_trier.native.shutil.which", lambda name: str(path_binary))
    monkeypatch.setattr(
        "gost_trier.native.subprocess_run_version",
        lambda path: subprocess.CompletedProcess([str(path), "--version"], 0, stdout="Xray-Link-Json dev\n", stderr=""),
    )
    monkeypatch.setattr("gost_trier.native.latest_release", lambda repo: (_ for _ in ()).throw(AssertionError("unexpected GitHub request")))

    resolved = resolve_release_binary(
        tool="Xray-Link-Json",
        repo="NightMachinery/Xray-Link-Json",
        executable_names=["Xray-Link-Json"],
        asset_name=xray_link_json_asset_name,
        min_version="v0.2.1",
    )

    assert resolved == path_binary
    assert "reports a dev version" in capsys.readouterr().err


def test_resolve_release_binary_warns_but_uses_old_env_override(monkeypatch, tmp_path, capsys):
    env_binary = tmp_path / "env-Xray-Link-Json"

    monkeypatch.setenv("XRAY_LINK_JSON_BIN", str(env_binary))
    monkeypatch.setattr(
        "gost_trier.native.subprocess_run_version",
        lambda path: subprocess.CompletedProcess([str(path), "--version"], 0, stdout="Xray-Link-Json v0.2.0\n", stderr=""),
    )

    resolved = resolve_release_binary(
        tool="Xray-Link-Json",
        repo="NightMachinery/Xray-Link-Json",
        executable_names=["Xray-Link-Json"],
        asset_name=xray_link_json_asset_name,
        env_var="XRAY_LINK_JSON_BIN",
        min_version="v0.2.1",
    )

    assert resolved == env_binary
    assert "does not satisfy minimum v0.2.1" in capsys.readouterr().err


def test_locate_xray_link_json_uses_external_dependency_minimum(monkeypatch):
    seen = {}

    def fake_resolve(**kwargs):
        seen.update(kwargs)
        return Path("/tmp/Xray-Link-Json")

    monkeypatch.setattr("gost_trier.native.resolve_release_binary", fake_resolve)

    assert locate_xray_link_json() == Path("/tmp/Xray-Link-Json")
    assert seen["repo"] == "NightMachinery/Xray-Link-Json"
    assert seen["min_version"] == "v0.2.1"


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
    assert config["outbounds"][0]["proxySettings"] == {"tag": "proxy-2", "transportLayer": True}
    assert config["outbounds"][1]["tag"] == "proxy-2"
    assert "proxySettings" not in config["outbounds"][1]
    assert "sendThrough" not in config["outbounds"][0]


def test_build_xray_config_accepts_single_json_forward_with_multiple_outbounds(tmp_path):
    path = tmp_path / "multi.json"
    path.write_text(
        json.dumps(
            {
                "outbounds": [
                    {"protocol": "freedom", "settings": {"hop": 1}},
                    {"protocol": "blackhole", "settings": {"hop": 2}},
                ]
            }
        )
    )
    args = XrayArgs(listens=[parse_listen("socks5://127.0.0.1:1050")], forwards=[str(path)])

    config = build_xray_config(args)

    assert [outbound["protocol"] for outbound in config["outbounds"]] == ["freedom", "blackhole"]


def test_build_xray_config_rejects_multi_outbound_json_in_chain(tmp_path):
    path = tmp_path / "multi.json"
    path.write_text('{"outbounds": [{"protocol": "freedom"}, {"protocol": "blackhole"}]}')
    args = XrayArgs(listens=[parse_listen("socks5://127.0.0.1:1050")], forwards=["direct://", str(path)])

    with pytest.raises(ValueError, match="chained JSON forward must contain exactly one outbound"):
        build_xray_config(args)


def test_build_xray_config_accepts_raw_outbound_json_forward(tmp_path):
    path = tmp_path / "outbound.json"
    path.write_text('{"protocol": "freedom", "settings": {"domainStrategy": "UseIP"}}')
    args = XrayArgs(listens=[parse_listen("socks5://127.0.0.1:1050")], forwards=[str(path)])

    config = build_xray_config(args)

    assert config["outbounds"][0]["protocol"] == "freedom"
    assert config["outbounds"][0]["settings"] == {"domainStrategy": "UseIP"}


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


def test_xray_tui_parse_args_defaults_and_tmux_template():
    options = parse_tui_args([])

    assert options.address == "127.0.0.1"
    assert options.socks_port == 1080
    assert options.http_port == 2080
    assert options.test_urls == ["https://api.ipify.org"]
    assert options.tmux_session == "xray-tui-s1080-h2080"
    assert options.stop_on_exit
    assert options.sample == 100


def test_xray_tui_parse_args_accepts_sample_all_and_no_python_config(tmp_path):
    options = parse_tui_args(["--config", str(tmp_path / "config.yaml"), "--sample=all", "--no-python-config", "--no-stop-on-exit"])

    assert options.sample is None
    assert options.python_config is None
    assert not options.stop_on_exit


def test_xray_tui_creates_example_config(tmp_path):
    path = tmp_path / "nested" / "config.yaml"

    assert ensure_example_config(path)
    assert not ensure_example_config(path)
    assert "groups:" in path.read_text()


def test_xray_tui_loads_groups_subgroups_names_and_protocols(monkeypatch, tmp_path):
    monkeypatch.setattr("gost_trier.xray_tui.TUI_CACHE_DIR", tmp_path / "cache")
    json_path = tmp_path / "config.json"
    json_path.write_text('{"outbounds": [{"protocol": "trojan"}]}')
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
groups:
  - name: main
    subscriptions:
      - url: https://example.com/trojan.txt
    configs:
      - link: vless://00000000-0000-0000-0000-000000000000@example.com:443#Decoded%20Name
      - path: {json_path}
"""
    )

    loaded = load_tui_config(config_path)

    assert loaded.groups[0].name == "main"
    assert [subgroup.name for subgroup in loaded.groups[0].subgroups] == ["example.com/trojan.txt", "Manual configs"]
    manual = loaded.groups[0].subgroups[1]
    assert [(item.name, item.protocol) for item in manual.configs] == [("Decoded Name", "vless"), ("config", "trojan")]


def test_xray_tui_loads_named_and_inline_proxy_chains(tmp_path):
    path = tmp_path / "hop.json"
    path.write_text('{"outbounds": [{"protocol": "freedom"}]}')
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
proxy_chains:
  - name: shared
    chain:
      - link: socks5://127.0.0.1:10050
      - path: {path}
groups:
  - name: named
    proxy_chain: shared
    configs:
      - link: direct://
  - name: inline
    proxy_chain:
      - link: socks5://127.0.0.1:10051
    configs:
      - link: direct://
"""
    )

    loaded = load_tui_config(config_path)

    assert loaded.groups[0].proxy_chain is not None
    assert loaded.groups[0].proxy_chain.name == "shared"
    assert [item.protocol for item in loaded.groups[0].proxy_chain.items] == ["socks5", "freedom"]
    assert loaded.groups[1].proxy_chain is not None
    assert loaded.groups[1].proxy_chain.name is None
    assert loaded.groups[1].proxy_chain.items[0].link == "socks5://127.0.0.1:10051"


def test_xray_tui_rejects_unknown_proxy_chain(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("groups:\n  - name: main\n    proxy_chain: missing\n    configs:\n      - link: direct://\n")

    with pytest.raises(ValueError, match="unknown proxy chain"):
        load_tui_config(config_path)


def test_xray_tui_subscription_cache_becomes_configs(monkeypatch, tmp_path):
    cache_dir = tmp_path / "cache"
    monkeypatch.setattr("gost_trier.xray_tui.TUI_CACHE_DIR", cache_dir)
    sub_id = stable_id("subscription", f"{stable_id('group', 'main')}:https://example.com/sub.txt")
    cache_file = cache_dir / "subscriptions" / f"{sub_id}.json"
    cache_file.parent.mkdir(parents=True)
    cache_file.write_text('{"links": ["trojan://user@example.com:443#One"], "last_refreshed_at": 123}')
    config_path = tmp_path / "config.yaml"
    config_path.write_text("groups:\n  - name: main\n    subscriptions:\n      - url: https://example.com/sub.txt\n")

    loaded = load_tui_config(config_path)
    subgroup = loaded.groups[0].subgroups[0]

    assert subgroup.configs[0].name == "One"
    assert subgroup.configs[0].protocol == "trojan"


def test_xray_tui_link_display_name_decodes_fragment_and_falls_back_to_host_port():
    assert link_display_name("trojan://user@example.com:443#hello%20world", fallback="x") == "hello world"
    assert link_display_name("trojan://user@example.com:443", fallback="x") == "example.com:443"
    assert link_display_name("direct://", fallback="x") == "x"
    assert link_display_name("trojan://8r<[9'l6hAO#8ZQi@example.com:443", fallback="x") == "x"


def test_xray_tui_malformed_subscription_link_does_not_crash_cache_load(monkeypatch, tmp_path):
    cache_dir = tmp_path / "cache"
    monkeypatch.setattr("gost_trier.xray_tui.TUI_CACHE_DIR", cache_dir)
    sub_id = stable_id("subscription", f"{stable_id('group', 'main')}:https://example.com/sub.txt")
    cache_file = cache_dir / "subscriptions" / f"{sub_id}.json"
    cache_file.parent.mkdir(parents=True)
    cache_file.write_text(json.dumps({"links": ["trojan://8r<[9'l6hAO#8ZQi@example.com:443"], "last_refreshed_at": 123}))
    config_path = tmp_path / "config.yaml"
    config_path.write_text("groups:\n  - name: main\n    subscriptions:\n      - url: https://example.com/sub.txt\n")

    item = load_tui_config(config_path).groups[0].subgroups[0].configs[0]

    assert item.protocol == "trojan"
    assert item.name.startswith("config-")


def test_xray_tui_hotkey_config_can_override_and_disable(tmp_path):
    config = tmp_path / "config.py"
    config.write_text(
        """
def configure(hotkeys):
    hotkeys["restart_xray"] = "SPC x R"
    hotkeys["refresh_all"] = None
"""
    )

    hotkeys = load_hotkeys(config)

    assert hotkeys["restart_xray"] == "SPC x R"
    assert hotkeys["refresh_all"] is None


def test_xray_tui_hotkey_config_rejects_duplicate(tmp_path):
    config = tmp_path / "config.py"
    config.write_text(
        """
def configure(hotkeys):
    hotkeys["quit"] = "r"
"""
    )

    with pytest.raises(ValueError, match="duplicate hotkey"):
        load_hotkeys(config)


def test_xray_tui_subscription_links_decode_base64():
    payload = base64.b64encode(b"# comment\ntrojan://one\nvless://two\n")

    assert subscription_links_from_bytes(payload) == ["trojan://one", "vless://two"]


def test_xray_tui_refresh_subscription_keeps_old_cache_on_failure(monkeypatch, tmp_path):
    monkeypatch.setattr("gost_trier.xray_tui.TUI_CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr("gost_trier.xray_tui.download_without_proxy", lambda url, timeout: (_ for _ in ()).throw(RuntimeError("direct bad")))
    monkeypatch.setattr("gost_trier.xray_tui.download_with_env_proxy", lambda url, timeout: (_ for _ in ()).throw(RuntimeError("proxy bad")))
    sub_id = "subscription-test"
    cache_file = tmp_path / "cache" / "subscriptions" / f"{sub_id}.json"
    cache_file.parent.mkdir(parents=True)
    cache_file.write_text('{"links": ["trojan://old"], "last_refreshed_at": 12}')

    from gost_trier.xray_tui import Subscription

    refreshed = refresh_subscription(Subscription(id=sub_id, name="sub", url="https://example.com/sub.txt"))

    assert refreshed.configs == []
    cache = json.loads(cache_file.read_text())
    assert cache["links"] == ["trojan://old"]
    assert "direct bad" in cache["error"]


def test_xray_tui_refresh_subscription_tries_proxy_after_direct(monkeypatch, tmp_path):
    monkeypatch.setattr("gost_trier.xray_tui.TUI_CACHE_DIR", tmp_path / "cache")
    calls = []
    monkeypatch.setattr("gost_trier.xray_tui.download_without_proxy", lambda url, timeout: calls.append("direct") or (_ for _ in ()).throw(RuntimeError("bad")))
    monkeypatch.setattr("gost_trier.xray_tui.download_with_env_proxy", lambda url, timeout: calls.append("proxy") or b"trojan://new#Name\n")

    from gost_trier.xray_tui import Subscription

    refreshed = refresh_subscription(Subscription(id="subscription-test", name="sub", url="https://example.com/sub.txt"))

    assert calls == ["direct", "proxy"]
    assert refreshed.configs[0].name == "Name"


def test_xray_tui_startup_refreshes_stale_subscription_before_tui(monkeypatch, tmp_path):
    monkeypatch.setattr("gost_trier.xray_tui.TUI_CACHE_DIR", tmp_path / "cache")
    config_path = tmp_path / "config.yaml"
    config_path.write_text("groups:\n  - name: main\n    subscriptions:\n      - url: https://example.com/sub.txt\n")
    config = load_tui_config(config_path)
    state = TuiState(active_group_id=config.groups[0].id, active_subgroup_id=config.groups[0].subgroups[0].id)
    options = parse_tui_args(["--config", str(config_path), "--timeout=1s"])
    monkeypatch.setattr("gost_trier.xray_tui.download_without_proxy", lambda url, timeout: b"trojan://new#Name\n")

    refreshed = startup_refresh_stale_selected_scope(config, state, options)

    assert refreshed.groups[0].subgroups[0].configs[0].name == "Name"


def test_xray_tui_json_config_replaces_inbounds(tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{"inbounds": [{"port": 1}], "outbounds": [{"protocol": "freedom", "settings": {}}]}')
    config_path = tmp_path / "config.yaml"
    config_path.write_text(f"groups:\n  - name: main\n    configs:\n      - path: {path}\n")
    item = load_tui_config(config_path).groups[0].subgroups[0].configs[0]
    options = TuiOptions(
        address="127.0.0.1",
        socks_port=1080,
        http_port=2080,
        test_urls=["https://example.com"],
        config=config_path,
        python_config=None,
        tmux_session="xray-tui-test",
        stop_on_exit=True,
        sub_auto_refresh=60,
        rotate_refresh=60,
        sample=100,
        timeout=1,
        jobs=1,
        true_color="auto",
        dark_mode="auto",
        light_theme="xray-light",
        dark_theme="xray-dark",
        verbose=0,
    )

    config = config_item_to_xray_config(item, options)

    assert [inbound["port"] for inbound in config["inbounds"]] == [1080, 2080]
    assert [listen.port for listen in build_listens(options)] == [1080, 2080]


def test_xray_tui_chained_json_config_uses_single_outbound_rule(tmp_path):
    selected = tmp_path / "selected.json"
    selected.write_text('{"outbounds": [{"protocol": "freedom"}, {"protocol": "blackhole"}]}')
    hop = tmp_path / "hop.json"
    hop.write_text('{"outbounds": [{"protocol": "freedom"}]}')
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
proxy_chains:
  - name: shared
    chain:
      - path: {hop}
groups:
  - name: main
    proxy_chain: shared
    configs:
      - path: {selected}
"""
    )
    loaded = load_tui_config(config_path)
    group = loaded.groups[0]
    item = group.subgroups[0].configs[0]
    options = parse_tui_args(["--config", str(config_path)])

    with pytest.raises(ValueError, match="chained JSON forward must contain exactly one outbound"):
        config_item_to_xray_config(item, options, proxy_chain=group.proxy_chain)


def test_xray_tui_chained_link_config_preserves_selected_then_chain_order(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
groups:
  - name: main
    proxy_chain:
      - link: blackhole://
    configs:
      - link: direct://
"""
    )
    loaded = load_tui_config(config_path)
    group = loaded.groups[0]
    item = group.subgroups[0].configs[0]
    options = parse_tui_args(["--config", str(config_path)])

    config = config_item_to_xray_config(item, options, proxy_chain=group.proxy_chain)

    assert [outbound["protocol"] for outbound in config["outbounds"]] == ["freedom", "blackhole"]
    assert config["outbounds"][0]["proxySettings"] == {"tag": "proxy-2", "transportLayer": True}


def test_xray_tui_rotate_configs_samples_and_chooses_fastest(monkeypatch, tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("groups:\n  - name: main\n    configs:\n      - link: direct://#a\n      - link: direct://#b\n")
    items = load_tui_config(config_path).groups[0].subgroups[0].configs
    options = parse_tui_args(["--config", str(config_path), "--sample=all"])

    def fake_test(item, options, *, proxy_chain=None):
        return {"config-id": item.id, "best-delay-ms": 2 if item.name == "a" else 1}

    monkeypatch.setattr("gost_trier.xray_tui.test_config_item", fake_test)

    assert rotate_configs(items, options)["config-id"] == items[1].id
    assert choose_fastest([None, {"best-delay-ms": 5}, {"best-delay-ms": 3}]) == {"best-delay-ms": 3}
    assert len(sample_configs(items, 1)) == 1


def test_xray_tui_session_manager_uses_tmux_and_cleans_up(monkeypatch, tmp_path):
    commands = []
    config_path = tmp_path / "config.yaml"
    config_path.write_text("groups:\n  - name: main\n    configs:\n      - link: direct://\n")
    item = load_tui_config(config_path).groups[0].subgroups[0].configs[0]
    options = parse_tui_args(["--config", str(config_path), "--tmux-session=test-session"])

    monkeypatch.setattr("gost_trier.xray_tui.TUI_CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr("gost_trier.xray_tui.shutil.which", lambda name: "/usr/bin/tmux" if name == "tmux" else None)
    monkeypatch.setattr("gost_trier.xray_tui.ensure_tmux_session", lambda session: commands.append(["ensure", session]))
    monkeypatch.setattr("gost_trier.xray_tui.ensure_xray_dependency", lambda verbose=0: "xray")
    monkeypatch.setattr("gost_trier.xray_tui.build_xray_config", lambda args, verbose=0: {"inbounds": [], "outbounds": [{"protocol": "freedom"}]})
    monkeypatch.setattr("gost_trier.xray_tui.subprocess.run", lambda command, **kwargs: commands.append(command) or subprocess.CompletedProcess(command, 0))

    manager = XraySessionManager(options)
    manager.start(item)
    manager.stop()

    assert ["ensure", "test-session"] in commands
    assert any(command[:4] == ["tmux", "new-window", "-t", "test-session"] for command in commands if isinstance(command, list))
    assert any(command[:3] == ["tmux", "kill-window", "-t"] for command in commands if isinstance(command, list))


def test_xray_tui_layout_and_true_color(monkeypatch):
    assert layout_mode(99) == "narrow"
    assert layout_mode(100) == "wide"
    monkeypatch.setenv("COLORTERM", "truecolor")
    assert true_color_enabled("auto")
    assert true_color_enabled("on")
    assert not true_color_enabled("off")
