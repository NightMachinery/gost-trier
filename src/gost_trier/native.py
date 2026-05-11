from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import stat
import sys
import tarfile
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from threading import Lock
from urllib.request import Request, urlopen


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
    request = Request(f"{GITHUB_API}/{repo}/releases/latest", headers={"User-Agent": "gost-trier/0.1.0"})
    with urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
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


def resolve_release_binary(
    *,
    tool: str,
    repo: str,
    executable_names: Sequence[str],
    asset_name: Callable[[str, str, str], str],
    env_var: str | None = None,
) -> Path:
    if env_var:
        env_path = os.environ.get(env_var)
        if env_path:
            return Path(env_path)

    for name in executable_names:
        path_binary = shutil.which(name)
        if path_binary:
            return Path(path_binary)

    with _RESOLVE_LOCK:
        goos, goarch = system_arch()
        release = latest_release(repo)
        cache_dir = DEFAULT_CACHE_ROOT / "bin" / tool / release.tag / f"{goos}-{goarch}"
        cached = find_executable(cache_dir, executable_names)
        if cached is not None:
            return cached

        wanted_name = asset_name(release.tag, goos, goarch)
        asset = next((item for item in release.assets if item.name == wanted_name), None)
        if asset is None:
            raise RuntimeError(f"no {tool} release asset for {goos}/{goarch}: expected {wanted_name}")

        archive_path = cache_dir / asset.name
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


def download_file(url: str, destination: Path) -> None:
    request = Request(url, headers={"User-Agent": "gost-trier/0.1.0"})
    with urlopen(request, timeout=120) as response, destination.open("wb") as file:
        shutil.copyfileobj(response, file)


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


def locate_xray_link_json() -> Path:
    suffix = executable_suffix()
    return resolve_release_binary(
        tool="Xray-Link-Json",
        repo="NightMachinery/Xray-Link-Json",
        executable_names=[f"Xray-Link-Json{suffix}", "Xray-Link-Json"],
        asset_name=xray_link_json_asset_name,
        env_var="XRAY_LINK_JSON",
    )


def locate_xray() -> Path:
    suffix = executable_suffix()
    return resolve_release_binary(
        tool="xray",
        repo="XTLS/Xray-core",
        executable_names=[f"xray{suffix}", "xray"],
        asset_name=xray_asset_name,
        env_var="XRAY_BIN",
    )
