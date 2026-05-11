# Usage

Install and run with uv:

```sh
uv run gost-trier [--test-url=https://api.ipify.org] [--test-url=https://myip.wtf/json] [--shuffle] [--timeout=20s] [--jobs=1] FILE [FILE ...] -- GOST_ARGS...
```

Example:

```sh
uv run gost-trier --test-url=https://myip.wtf/json --shuffle trojan.txt -- -L=socks5://127.0.0.1:1050 -F=MAGIC_FILE_1
```

During tests, any `-L` arguments after `--` are ignored and replaced with a temporary free local listener:

```sh
-L=socks5://127.0.0.1:<free-port>
```

The original `-L` arguments are still preserved for `--run-in-tmux`, where they are treated as the run-phase listener.

Multiple files map to matching placeholders. `MAGIC_FILE_1` uses the first file, `MAGIC_FILE_2` uses the second, and so on. Combinations are tried as a Cartesian product:

```sh
uv run gost-trier chains.txt exits.txt -- -F=MAGIC_FILE_1 -F=MAGIC_FILE_2 -F=direct://
```

Proxy chains are preserved in argument order.

Candidate sources can be local files or `http(s)` URLs:

```sh
uv run xray-trier 'https://raw.githubusercontent.com/Epodonios/v2ray-configs/main/Splitted-By-Protocol/trojan.txt' -- -F=MAGIC_FILE_1
```

Downloaded and local candidate lists may be plain text or base64-encoded subscription text. Blank lines and lines whose trimmed form starts with `#` are ignored.

## Xray

Installing this repo provides two Xray commands as well:

```sh
uv run xray-run json -L=socks5://127.0.0.1:1050 -F='trojan://...'
uv run xray-run exec -L=socks5://127.0.0.1:1050 -F='trojan://...'
uv run xray-trier --timeout=20s trojan.txt -- -L=socks5://127.0.0.1:1050 -F=MAGIC_FILE_1
```

`xray-run json` prints the generated Xray JSON config. `xray-run exec` writes the config to a temporary file and runs:

```sh
xray run -c <temp-file>
```

The `xray-run` interface accepts the same `-L` and `-F` shapes used in the examples above. If `-L` is omitted, it picks a free local socks port and logs it to stderr. Multiple `-L` listeners are supported. Xray listeners may be `socks5://`, `socks5h://`, `socks://`, or `http://`; listener username/password auth is supported for HTTP and SOCKS. A missing listener host defaults to `0.0.0.0`, for example:

```sh
xray-run exec -L=socks5://127.0.0.1:1060 -L='http://user:password@:2060' -F='vless://...'
```

Xray share links are converted with `Xray-Link-Json`. Discovery order is:

1. `XRAY_LINK_JSON`
2. `Xray-Link-Json` on `PATH`
3. automatic `go install github.com/NightMachinery/Xray-Link-Json@latest`
4. the local clone at `~/.base/Xray-Link-Json`

For multiple `-F` values, `xray-run` creates best-effort chained Xray outbounds with `proxySettings`, preserving CLI order.

If no `-F` is provided to `xray-run`, it uses a direct connection (`direct://` / Xray `freedom` outbound).

`xray-trier` defaults to `--jobs=50`; `gost-trier` defaults to `--jobs=1`. Both commands default to trying `https://api.ipify.org` first, then `https://myip.wtf/json`.

To launch the fastest working configs in tmux after testing:

```sh
uv run gost-trier --run-in-tmux=gost --run-top=3 trojan.txt -- -L=socks5://127.0.0.1:1050 -F=MAGIC_FILE_1
uv run xray-trier --run-in-tmux=xray --run-top=3 trojan.txt -- -L=socks5://127.0.0.1:1050 -F=MAGIC_FILE_1
```

Progress is written to stderr. The final stdout is a JSON array sorted by `best-delay-ms`:

```json
[
  {
    "best-delay-ms": 312,
    "config": ["-L=socks5://127.0.0.1:1050", "-F=..."],
    "tests": [
      {
        "url": "https://myip.wtf/json",
        "delay-ms": 312,
        "result": "ok",
        "result-http-code": 200,
        "bytes": 123
      }
    ]
  }
]
```
