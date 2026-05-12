from __future__ import annotations

import sys
from pathlib import Path
from threading import Lock
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, getproxies, proxy_bypass, urlopen

from tqdm import tqdm


DOWNLOAD_CHUNK_SIZE = 1024 * 1024
USER_AGENT = "gost-trier/0.1.0"
_PROXY_NOTICE_LOCK = Lock()
_PROXY_NOTICE_PRINTED = False
_PROGRESS_ENABLED = True


def set_download_progress_enabled(enabled: bool) -> None:
    global _PROGRESS_ENABLED

    _PROGRESS_ENABLED = enabled


def download_bytes(url: str, *, timeout: float = 30, desc: str | None = None) -> bytes:
    chunks: list[bytes] = []
    request = Request(url, headers={"User-Agent": USER_AGENT})
    print_proxy_notice(url)
    with urlopen(request, timeout=timeout) as response:
        progress = download_progress(response, desc or download_name(url))
        with progress:
            while chunk := response.read(DOWNLOAD_CHUNK_SIZE):
                chunks.append(chunk)
                progress.update(len(chunk))
    return b"".join(chunks)


def download_file(url: str, destination: Path, *, timeout: float = 120) -> None:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    print_proxy_notice(url)
    with urlopen(request, timeout=timeout) as response, destination.open("wb") as file:
        progress = download_progress(response, destination.name)
        with progress:
            while chunk := response.read(DOWNLOAD_CHUNK_SIZE):
                file.write(chunk)
                progress.update(len(chunk))


def download_progress(response: object, desc: str) -> tqdm:
    return tqdm(
        total=content_length(response),
        unit="B",
        unit_scale=True,
        unit_divisor=1024,
        desc=desc,
        file=sys.stderr,
        leave=False,
        disable=not _PROGRESS_ENABLED,
    )


def download_name(url: str) -> str:
    path = urlsplit(url).path
    return Path(path).name or "download"


def content_length(response: object) -> int | None:
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    raw = headers.get("Content-Length")
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


def print_proxy_notice(url: str) -> None:
    global _PROXY_NOTICE_PRINTED

    with _PROXY_NOTICE_LOCK:
        if _PROXY_NOTICE_PRINTED:
            return
        proxy = download_proxy_for_url(url)
        if proxy is not None:
            print(f"Using proxy for downloads: {redact_proxy_url(proxy)}", file=sys.stderr)
        _PROXY_NOTICE_PRINTED = True


def download_proxy_for_url(url: str) -> str | None:
    parsed = urlsplit(url)
    if parsed.hostname and proxy_bypass(parsed.hostname):
        return None
    proxies = getproxies()
    proxy = proxies.get(parsed.scheme) or proxies.get("all")
    return proxy or None


def redact_proxy_url(proxy: str) -> str:
    parsed = urlsplit(proxy)
    if not parsed.username and not parsed.password:
        return proxy
    host = parsed.hostname or ""
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, f"***:***@{host}", parsed.path, parsed.query, parsed.fragment))
