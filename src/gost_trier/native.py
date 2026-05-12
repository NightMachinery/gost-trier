from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import stat
import sys
import tarfile
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from pathlib import Path
from threading import Lock
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .downloads import download_file, url_error_message

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10
    import tomli as tomllib


GITHUB_API = "https://api.github.com/repos"
DEFAULT_CACHE_ROOT = Path.home() / ".cache" / "gost-trier"
_RESOLVE_LOCK = Lock()


@dataclass(frozen=True)
class ReleaseAsset:
    name: str
    download_url: str
    digest: str | None = None


@dataclass(frozen=True)
class Release:
    tag: str
    assets: list[ReleaseAsset]


@dataclass(frozen=True)
class BinaryUpdateResult:
    tool: str
    tag: str
    asset_name: str
    cached: Path | None
    installed: Path | None
    download_url: str


@dataclass(frozen=True)
class ReleaseBinary:
    tool: str
    repo: str
    executable_names: Sequence[str]
    asset_name: Callable[[str, str, str], str]
    env_var: str | None = None
    min_version: str | None = None


def system_arch() -> tuple[str, str]:
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "darwin":
        goos = "darwin"
    elif system == "windows":
        goos = "windows"
    elif system == "linux":
        goos = "linux"
    else:
        raise RuntimeError(f"unsupported OS for automatic binary install: {system}")

    if machine in {"x86_64", "amd64"}:
        goarch = "amd64"
    elif machine in {"aarch64", "arm64"}:
        goarch = "arm64"
    else:
        raise RuntimeError(f"unsupported CPU architecture for automatic binary install: {machine}")
    return goos, goarch


def executable_suffix(goos: str | None = None) -> str:
    resolved_goos = goos or system_arch()[0]
    return ".exe" if resolved_goos == "windows" else ""


@lru_cache(maxsize=None)
def latest_release(repo: str) -> Release:
    url = f"{GITHUB_API}/{repo}/releases/latest"
    request = Request(url, headers={"User-Agent": "gost-trier/0.1.0"})
    try:
        with urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError) as exc:
        raise RuntimeError(url_error_message("failed to fetch latest release metadata from", url, exc)) from exc
    assets = [
        ReleaseAsset(
            name=item["name"],
            download_url=item["browser_download_url"],
            digest=item.get("digest"),
        )
        for item in payload.get("assets", [])
        if item.get("name") and item.get("browser_download_url")
    ]
    return Release(tag=payload["tag_name"], assets=assets)


@lru_cache(maxsize=1)
def external_deps() -> dict[str, dict[str, str]]:
    with resources.files("gost_trier").joinpath("external_deps.toml").open("rb") as handle:
        payload = tomllib.load(handle)
    if not isinstance(payload, dict):
        raise RuntimeError("external_deps.toml must contain dependency tables")
    deps: dict[str, dict[str, str]] = {}
    for tool, raw_spec in payload.items():
        if not isinstance(raw_spec, dict):
            raise RuntimeError(f"external_deps.toml dependency must be a table: {tool}")
        spec = {key: value for key, value in raw_spec.items() if isinstance(key, str) and isinstance(value, str)}
        if not spec.get("repo"):
            raise RuntimeError(f"external_deps.toml dependency missing repo: {tool}")
        deps[tool] = spec
    return deps


def resolve_release_binary(
    *,
    tool: str,
    repo: str,
    executable_names: Sequence[str],
    asset_name: Callable[[str, str, str], str],
    env_var: str | None = None,
    min_version: str | None = None,
) -> Path:
    if env_var:
        env_path = os.environ.get(env_var)
        if env_path:
            path = Path(env_path)
            warn_if_binary_version_unsatisfied(tool, path, min_version, source=f"{env_var} override")
            return path

    with _RESOLVE_LOCK:
        goos, goarch = system_arch()
        platform_cache_dir = DEFAULT_CACHE_ROOT / "bin" / tool
        cached = find_cached_executable(platform_cache_dir, goos, goarch, executable_names, min_version=min_version)
        if cached is not None:
            return cached

        path_binary = find_path_executable(executable_names)
        if path_binary is not None:
            if binary_version_satisfies(tool, path_binary, min_version):
                return path_binary
            warn_binary_version_unsatisfied(tool, path_binary, min_version, source="PATH")

        release = latest_release(repo)
        require_release_version(tool, release.tag, min_version)
        cache_dir = platform_cache_dir / release.tag / f"{goos}-{goarch}"
        cached = find_executable(cache_dir, executable_names)
        if cached is not None:
            return cached

        wanted_name = asset_name(release.tag, goos, goarch)
        asset = next((item for item in release.assets if item.name == wanted_name), None)
        if asset is None:
            raise RuntimeError(f"no {tool} release asset for {goos}/{goarch}: expected {wanted_name}")

        archive_path = cache_dir / asset.name
        return install_release_asset(
            tool=tool,
            asset=asset,
            archive_path=archive_path,
            cache_dir=cache_dir,
            executable_names=executable_names,
        )


def locate_release_binary(binary: ReleaseBinary) -> Path:
    return resolve_release_binary(
        tool=binary.tool,
        repo=binary.repo,
        executable_names=binary.executable_names,
        asset_name=binary.asset_name,
        env_var=binary.env_var,
        min_version=binary.min_version,
    )


def update_release_binary(
    *,
    tool: str,
    repo: str,
    executable_names: Sequence[str],
    asset_name: Callable[[str, str, str], str],
    min_version: str | None = None,
    no_download: bool = False,
) -> BinaryUpdateResult:
    goos, goarch = system_arch()
    release = latest_release(repo)
    require_release_version(tool, release.tag, min_version)
    cache_dir = DEFAULT_CACHE_ROOT / "bin" / tool / release.tag / f"{goos}-{goarch}"
    cached = find_executable(cache_dir, executable_names)
    wanted_name = asset_name(release.tag, goos, goarch)
    asset = next((item for item in release.assets if item.name == wanted_name), None)
    if asset is None:
        raise RuntimeError(f"no {tool} release asset for {goos}/{goarch}: expected {wanted_name}")
    if cached is not None or no_download:
        return BinaryUpdateResult(
            tool=tool,
            tag=release.tag,
            asset_name=asset.name,
            cached=cached,
            installed=None,
            download_url=asset.download_url,
        )

    installed = install_release_asset(
        tool=tool,
        asset=asset,
        archive_path=cache_dir / asset.name,
        cache_dir=cache_dir,
        executable_names=executable_names,
    )
    return BinaryUpdateResult(
        tool=tool,
        tag=release.tag,
        asset_name=asset.name,
        cached=None,
        installed=installed,
        download_url=asset.download_url,
    )


def update_release_binary_from_spec(binary: ReleaseBinary, *, no_download: bool = False) -> BinaryUpdateResult:
    return update_release_binary(
        tool=binary.tool,
        repo=binary.repo,
        executable_names=binary.executable_names,
        asset_name=binary.asset_name,
        min_version=binary.min_version,
        no_download=no_download,
    )


def install_release_asset(
    *,
    tool: str,
    asset: ReleaseAsset,
    archive_path: Path,
    cache_dir: Path,
    executable_names: Sequence[str],
) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    print(f"{tool}: downloading {asset.name}", file=sys.stderr)
    download_file(asset.download_url, archive_path)
    verify_digest(archive_path, asset.digest)

    extract_dir = cache_dir / "extract"
    if extract_dir.exists():
        shutil.rmtree(extract_dir)
    extract_dir.mkdir(parents=True)
    extract_archive(archive_path, extract_dir)

    binary = find_executable(extract_dir, executable_names)
    if binary is None:
        raise RuntimeError(f"{tool} archive did not contain one of: {', '.join(executable_names)}")
    make_executable(binary)
    installed = cache_dir / binary.name
    shutil.copy2(binary, installed)
    make_executable(installed)
    return installed


def verify_digest(path: Path, digest: str | None) -> None:
    if not digest or not digest.startswith("sha256:"):
        return
    expected = digest.split(":", 1)[1]
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != expected:
        raise RuntimeError(f"checksum mismatch for {path.name}")


def extract_archive(archive_path: Path, destination: Path) -> None:
    if archive_path.suffix == ".zip":
        with zipfile.ZipFile(archive_path) as archive:
            archive.extractall(destination)
        return
    if archive_path.name.endswith(".tar.gz"):
        with tarfile.open(archive_path, "r:gz") as archive:
            archive.extractall(destination)
        return
    raise RuntimeError(f"unsupported archive format: {archive_path.name}")


def find_executable(root: Path, executable_names: Sequence[str]) -> Path | None:
    if not root.exists():
        return None
    allowed = set(executable_names)
    for path in root.rglob("*"):
        if path.is_file() and path.name in allowed:
            return path
    return None


def find_path_executable(executable_names: Sequence[str]) -> Path | None:
    for name in executable_names:
        path_binary = shutil.which(name)
        if path_binary:
            return Path(path_binary)
    return None


def find_cached_executable(
    root: Path,
    goos: str,
    goarch: str,
    executable_names: Sequence[str],
    *,
    min_version: str | None = None,
) -> Path | None:
    if not root.exists():
        return None
    platform_suffix = f"{goos}-{goarch}"
    cache_dirs = sorted(root.glob(f"*/{platform_suffix}"), key=lambda path: parse_semver_tag(path.parent.name) or (-1, -1, -1), reverse=True)
    for cache_dir in cache_dirs:
        tag = cache_dir.parent.name
        if not version_satisfies(tag, min_version):
            continue
        binary = find_executable(cache_dir, executable_names)
        if binary is not None:
            return binary
    return None


def parse_semver_tag(value: str | None) -> tuple[int, int, int] | None:
    if not value:
        return None
    match = re.search(r"\bv?(\d+)\.(\d+)\.(\d+)\b", value)
    if match is None:
        return None
    return tuple(int(part) for part in match.groups())


def is_dev_version(value: str | None) -> bool:
    return bool(value and re.search(r"\bdev\b", value, flags=re.IGNORECASE))


def version_satisfies(version: str | None, min_version: str | None) -> bool:
    if not min_version:
        return True
    parsed_version = parse_semver_tag(version)
    parsed_min = parse_semver_tag(min_version)
    if parsed_min is None:
        raise RuntimeError(f"invalid minimum version: {min_version}")
    if parsed_version is None:
        return False
    return parsed_version >= parsed_min


def binary_version_output(path: Path) -> str | None:
    try:
        completed = subprocess_run_version(path)
    except Exception:
        return None
    output = "\n".join(part for part in [completed.stdout, completed.stderr] if part)
    if completed.returncode != 0:
        return output or None
    return output or None


def subprocess_run_version(path: Path):
    import subprocess

    return subprocess.run(
        [str(path), "--version"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        timeout=10,
    )


def binary_version_satisfies(tool: str, path: Path, min_version: str | None) -> bool:
    if not min_version:
        return True
    output = binary_version_output(path)
    if is_dev_version(output):
        print(f"{tool}: warning: {path} reports a dev version; required minimum {min_version} cannot be verified", file=sys.stderr)
        return True
    return version_satisfies(output, min_version)


def warn_if_binary_version_unsatisfied(tool: str, path: Path, min_version: str | None, *, source: str) -> None:
    if not min_version:
        return
    output = binary_version_output(path)
    if is_dev_version(output):
        print(f"{tool}: warning: {source} {path} reports a dev version; required minimum {min_version} cannot be verified", file=sys.stderr)
        return
    if not version_satisfies(output, min_version):
        detail = first_line(output) or "version unavailable"
        print(f"{tool}: warning: {source} {path} does not satisfy minimum {min_version}: {detail}", file=sys.stderr)


def warn_binary_version_unsatisfied(tool: str, path: Path, min_version: str | None, *, source: str) -> None:
    if not min_version:
        return
    output = binary_version_output(path)
    detail = first_line(output) or "version unavailable"
    print(f"{tool}: warning: ignoring {source} binary {path}; requires at least {min_version}: {detail}", file=sys.stderr)


def first_line(value: str | None) -> str | None:
    if not value:
        return None
    for line in value.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return None


def require_release_version(tool: str, tag: str, min_version: str | None) -> None:
    if not version_satisfies(tag, min_version):
        raise RuntimeError(f"{tool} latest release {tag} is lower than required minimum {min_version}")


def make_executable(path: Path) -> None:
    if platform.system().lower() == "windows":
        return
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def xray_link_json_asset_name(tag: str, goos: str, goarch: str) -> str:
    extension = "zip" if goos == "windows" else "tar.gz"
    return f"Xray-Link-Json_{tag}_{goos}_{goarch}.{extension}"


def xray_asset_name(tag: str, goos: str, goarch: str) -> str:
    del tag
    if goos == "linux" and goarch == "amd64":
        return "Xray-linux-64.zip"
    if goos == "linux" and goarch == "arm64":
        return "Xray-linux-arm64-v8a.zip"
    if goos == "darwin" and goarch == "amd64":
        return "Xray-macos-64.zip"
    if goos == "darwin" and goarch == "arm64":
        return "Xray-macos-arm64-v8a.zip"
    if goos == "windows" and goarch == "amd64":
        return "Xray-windows-64.zip"
    if goos == "windows" and goarch == "arm64":
        return "Xray-windows-arm64-v8a.zip"
    raise RuntimeError(f"unsupported Xray platform: {goos}/{goarch}")


def xray_link_json_binary() -> ReleaseBinary:
    suffix = executable_suffix()
    spec = external_deps()["Xray-Link-Json"]
    return ReleaseBinary(
        tool="Xray-Link-Json",
        repo=spec["repo"],
        executable_names=[f"Xray-Link-Json{suffix}", "Xray-Link-Json"],
        asset_name=xray_link_json_asset_name,
        env_var=spec.get("env_var"),
        min_version=spec.get("min_version"),
    )


def xray_binary() -> ReleaseBinary:
    suffix = executable_suffix()
    spec = external_deps()["xray"]
    return ReleaseBinary(
        tool="xray",
        repo=spec["repo"],
        executable_names=[f"xray{suffix}", "xray"],
        asset_name=xray_asset_name,
        env_var=spec.get("env_var"),
        min_version=spec.get("min_version"),
    )


def locate_xray_link_json() -> Path:
    return locate_release_binary(xray_link_json_binary())


def update_xray_link_json(*, no_download: bool = False) -> BinaryUpdateResult:
    return update_release_binary_from_spec(xray_link_json_binary(), no_download=no_download)


def locate_xray() -> Path:
    return locate_release_binary(xray_binary())


def update_xray(*, no_download: bool = False) -> BinaryUpdateResult:
    return update_release_binary_from_spec(xray_binary(), no_download=no_download)
