# Usage

Install and run with uv:

```sh
uv run gost-trier [--test-url=https://api.ipify.org] [--test-url=https://myip.wtf/json] [--shuffle] [--timeout=20s] [--jobs=1] [-o RESULTS.json] FILE [FILE ...] -- GOST_ARGS...
```

Example:

```sh
uv run gost-trier --test-url=https://myip.wtf/json --shuffle trojan.txt -- '-L=socks5://127.0.0.1:1050' '-F=MAGIC_FILE_1'
```

During tests, any `-L` arguments after `--` are ignored and replaced with a temporary free local listener:

```sh
-L=socks5://127.0.0.1:<free-port>
```

The original `-L` arguments are still preserved for `--run-in-tmux`, where they are treated as the run-phase listener.

Multiple files map to matching placeholders. `MAGIC_FILE_1` uses the first file, `MAGIC_FILE_2` uses the second, and so on. Combinations are tried as a Cartesian product:

```sh
uv run gost-trier chains.txt exits.txt -- '-F=MAGIC_FILE_1' '-F=MAGIC_FILE_2' '-F=direct://'
```

Proxy chains are preserved in argument order.

Candidate sources can be local files or `http(s)` URLs:

```sh
uv run xray-trier 'https://raw.githubusercontent.com/Epodonios/v2ray-configs/main/Splitted-By-Protocol/trojan.txt' -- '-F=MAGIC_FILE_1'
```

Downloaded and local candidate lists may be plain text or base64-encoded subscription text. Blank lines and lines whose trimmed form starts with `#` are ignored.

## Xray

Installing this repo provides two Xray commands as well:

```sh
uv run xray-run json '-L=socks5://127.0.0.1:1050' '-F=trojan://...'
uv run xray-run exec '-L=socks5://127.0.0.1:1050' '-F=trojan://...'
uv run xray-trier --timeout=20s trojan.txt -- '-L=socks5://127.0.0.1:1050' '-F=MAGIC_FILE_1'
uv run xray-tui --socks-port=1080 --http-port=2080
```

`xray-run json` prints the generated Xray JSON config. `xray-run exec` writes the config to a temporary file and runs:

```sh
xray run -c <temp-file>
```

Use `-o` / `--output` to write final JSON to a file instead of stdout. `-` means stdout and is the default. Missing parent directories are created automatically.

```sh
uv run xray-run json -o xray-run-debug.json '-L=socks5://127.0.0.1:1050' '-F=vless://...'
uv run xray-trier -o results/xray-working.json --timeout=5s trojan.txt -- '-F=MAGIC_FILE_1'
uv run gost-trier -o results/gost-working.json trojan.txt -- '-F=MAGIC_FILE_1'
```

All commands accept repeatable `-v` / `--verbose` flags. For `xray-run` you may put them either before or after the subcommand:

```sh
uv run xray-run -v json '-F=vless://...'
uv run xray-run json -vvv '-L=socks5://127.0.0.1:1060' '-F=vless://...'
uv run xray-trier -vv --timeout=5s trojan.txt -- '-F=MAGIC_FILE_1'
uv run gost-trier -v trojan.txt -- '-F=MAGIC_FILE_1'
```

Verbosity levels:

1. `-v` prints selected native helper paths, Xray version, and native smoke-test results.
2. `-vv` also prints subprocess commands, return codes, stdout, and stderr for converter and Xray validation steps.
3. `-vvv` also prints raw share links and the full generated Xray JSON before validation/execution.

Verbose diagnostics are raw and may include proxy UUIDs, hostnames, generated config, and local paths. On Windows, `xray-run json -vvv ...` is usually the best first command because it validates the generated config without replacing the current process with Xray.

The `xray-run` interface accepts the same `-L` and `-F` shapes used in the examples above. If `-L` is omitted, it picks a free local socks port and logs it to stderr. Multiple `-L` listeners are supported. Xray listeners may be `socks5://`, `socks5h://`, `socks://`, or `http://`; listener username/password auth is supported for HTTP and SOCKS. A missing listener host defaults to `0.0.0.0`, for example:

```sh
xray-run exec '-L=socks5://127.0.0.1:1060' '-L=http://user:password@:2060' '-F=vless://...'
```

Xray share links are converted with `Xray-Link-Json`. External binary metadata is declared in `src/gost_trier/external_deps.toml`; `Xray-Link-Json` requires at least `v0.2.1`, which includes support for bare proxy forwards such as `socks5://127.0.0.1:10050`. Discovery order is:

1. `XRAY_LINK_JSON_BIN`
2. cached release binaries from `NightMachinery/Xray-Link-Json` that satisfy the configured minimum version
3. `Xray-Link-Json` on `PATH` if `--version` satisfies the configured minimum, or reports `dev`
4. downloaded GitHub release binaries from `NightMachinery/Xray-Link-Json`
5. automatic `go install github.com/NightMachinery/Xray-Link-Json@latest`
6. the local clone at `~/.base/Xray-Link-Json`

Environment overrides are trusted and used even when their version is old or unverifiable; `xray-run` prints a warning in that case. A `dev` version also prints a warning but is allowed.

Xray itself is discovered in this order:

1. `XRAY_BIN`
2. cached release binaries from `XTLS/Xray-core`
3. `xray` on `PATH`
4. downloaded GitHub release binaries from `XTLS/Xray-core`

Release archive downloads and remote candidate-list downloads show byte progress on stderr by default. Use `--no-progress` to hide progress bars, or `--progress` to make the default explicit. If Python's active proxy settings route a download through a proxy, the first download prints `Using proxy for downloads: ...` with proxy credentials redacted.

Normal `xray-run` and `xray-trier` startup uses an explicit environment override, cached helper binary, or existing `PATH` binary before making GitHub release API requests. To explicitly check GitHub for newer helper binaries, run `xray-run update-binaries`; add `--no-download` to report the latest available release assets without downloading them.

`xray-trier` runs Xray and `Xray-Link-Json` resolution plus native smoke checks once before starting parallel config tests. The checks are cached for the rest of the process, including direct `xray-run` setup calls.

For multiple `-F` values, `xray-run` creates best-effort chained Xray outbounds with `proxySettings`, preserving CLI order. Chained outbounds set `transportLayer: true` so each outbound keeps its `streamSettings` when it dials through the next outbound.

If no `-F` is provided to `xray-run`, it uses a direct connection (`direct://` / Xray `freedom` outbound).

`xray-trier` defaults to `--jobs=50`; `gost-trier` defaults to `--jobs=1`. Both commands default to trying `https://api.ipify.org` first, then `https://myip.wtf/json`.

To launch the fastest working configs in tmux after testing:

```sh
uv run gost-trier --run-in-tmux=gost --run-top=3 trojan.txt -- '-L=socks5://127.0.0.1:1050' '-F=MAGIC_FILE_1'
uv run xray-trier --run-in-tmux=xray --run-top=3 trojan.txt -- '-L=socks5://127.0.0.1:1050' '-F=MAGIC_FILE_1'
```

When `--run-in-tmux` is used, the command prints the tmux session name, an attach command when tmux is available, and curl commands for testing the launched listeners. On Windows these test commands use `curl.exe` to avoid PowerShell aliases. If `tmux` is missing, it attempts a best-effort install using the available system package manager, including common Linux package managers, Homebrew on macOS, and Scoop/Chocolatey/Winget on Windows. If tmux still is not available, it falls back to managed detached processes. Reusing the same session name cleans up previously managed processes for that session before launching new ones.

Progress is written to stderr. The final JSON output is a JSON array sorted by `best-delay-ms`:

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

To keep the generated config for inspection, write JSON to a file with `-o` / `--output`; missing parent directories are created automatically:

```powershell
xray-run json -o generated_config.json '-L=socks5://127.0.0.1:1060' '-L=http://127.0.0.1:2060' '-F=vless://...'
```

## Verbosity

For native helper problems, add repeatable `-v` flags. `-v` prints selected helper paths, versions, and smoke-test results; `-vv` also prints subprocess commands, return codes, stdout, and stderr; `-vvv` prints raw share links and the full generated Xray JSON. The verbose output is intentionally unredacted.

```powershell
xray-run json -vvv '-L=socks5://127.0.0.1:1060' '-L=http://127.0.0.1:2060' '-F=vless://...'
```

## xray-tui

`xray-tui` is an interactive Textual UI over Xray subscriptions and explicit configs:

```sh
uv run xray-tui --address=127.0.0.1 --socks-port=1080 --http-port=2080
```

If the YAML config is missing, `xray-tui` creates `~/.xray-tui/config.yaml` and exits. Parent directories are created automatically. A minimal config looks like this:

```yaml
groups:
  - name: default
    subscriptions:
      - url: https://example.com/sub.txt
      - name: backup
        url: https://example.com/backup.txt
    configs:
      - link: direct://
      - link: vless://00000000-0000-0000-0000-000000000000@example.com:443?security=tls#decoded%20name
      - path: ~/xray-configs/example.json
```

The UI shows top-level YAML groups. Each subscription becomes its own subgroup, and explicit `configs` are shown under `Manual configs`. Config `name` fields are optional. For share links, `xray-tui` uses the URL-decoded fragment after `#`, then `host:port`, then a stable generated name. The table also shows each config protocol from the link scheme, or from the first JSON outbound protocol for file configs.

Subscriptions are cached under `~/.cache/gost-trier/xray-tui/`. Refresh first tries a direct no-proxy download, then retries with normal `HTTP_PROXY` / `HTTPS_PROXY` environment handling. If refresh fails, the old cache is kept.

During startup, `xray-tui` prints progress messages to stderr until the Textual screen starts. If the previously selected subscription cache is stale, that refresh happens before the TUI takes over the terminal so slow downloads or large subscription parsing are visible instead of looking like a blank-screen hang. Malformed share links from subscriptions are still listed with generated fallback names when possible; they may fail later if selected or tested and the Xray converter rejects them.

The active Xray process uses both listeners:

```text
socks5://127.0.0.1:1080
http://127.0.0.1:2080
```

For JSON config files, `xray-tui` replaces `inbounds` with those listeners so the CLI ports always apply. For share links, it reuses the same conversion path as `xray-run`.

When tmux is available, `xray-tui` runs the active Xray config in:

```sh
tmux attach -t xray-tui-s1080-h2080
```

The default session template is `--tmux-session=xray-tui-s{SOCKS_PORT}-h{HTTP_PORT}`. By default the active Xray process is stopped when the TUI exits; use `--no-stop-on-exit` to leave it running.

Important default keys:

```text
j/k or down/up       move rows
[/]                  previous/next subgroup
{/}                  previous/next group
enter                select config and restart Xray
r                    refresh current subscription subgroup or group
t                    test sampled configs now
a                    toggle auto-rotate
SPC r a              refresh all subscriptions
SPC r g              refresh current group
SPC r s              refresh current subscription subgroup
SPC t a              test sampled configs now
SPC x r              restart active Xray
SPC x a              show tmux attach info
q                    quit
```

Hotkeys can be customized with trusted executable Python in `~/.xray-tui/config.py` by default:

```python
def configure(hotkeys):
    hotkeys["restart_xray"] = "SPC x r"
    hotkeys["quit"] = "ctrl+q"
    hotkeys["refresh_all"] = None
```

Assign `None` to disable an action. Unknown action names and duplicate key bindings are treated as config errors. Only use `config.py` files you trust.
