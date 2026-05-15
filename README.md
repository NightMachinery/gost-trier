# gost-trier

`gost-trier` tries proxy configurations generated from text files and reports the working ones as JSON.

## Install

Install `uv` if needed; on Linux/macOS:

```sh
curl -LsSf https://astral.sh/uv/install.sh | INSTALLER_PRINT_VERBOSE=1 sh
```

On Windows, you can install `uv` using Powershell:
```
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

You need to restart (re-open) your terminal (shell) if you didn't have `uv` installed before.

To restart Powershell, you need to run:

```
exit
```

Then re-open the terminal.

---

Install the commands locally with `uv`:

```sh
uv tool install 'git+https://github.com/NightMachinery/gost-trier.git'
```

From an existing local checkout:

```sh
cd /path/to/gost-trier
uv tool install .
```

This installs `gost-trier`, `xray-trier`, `xray-run`, and `xray-tui`.

`xray-run` and `xray-trier` auto-bootstrap native helpers when needed. External helper metadata lives in `src/gost_trier/external_deps.toml`; `Xray-Link-Json` currently requires at least `v0.2.1` so bare proxy forwards such as `-F=socks5://127.0.0.1:10050` are handled by the converter. They first honor explicit binary overrides, then use cached release binaries for Xray and `Xray-Link-Json` under `~/.cache/gost-trier/bin/`, then fall back to binaries already on `PATH`. Non-override binaries below a configured minimum are skipped so a suitable release can be installed. Override binaries are still used, but print a warning if their version is too old or reports `dev`. Release archive downloads and remote candidate-list downloads show byte progress on stderr by default; use `--no-progress` to hide progress bars. If a proxy is active for downloads, the first download prints `Using proxy for downloads: ...` with credentials redacted. `Xray-Link-Json` falls back to `go install` only if the release download is not available. Advanced users can override these paths with `XRAY_BIN` and `XRAY_LINK_JSON_BIN`.

`xray-trier` performs Xray and `Xray-Link-Json` setup checks once before launching parallel config tests, so high `--jobs` values do not repeat native smoke tests for every sampled config.

To explicitly check for newer cached helper binaries and download missing latest releases, run:

```sh
xray-run update-binaries
```

Use `xray-run update-binaries --no-download` to check what would be downloaded without writing binaries.

To upgrade later, `uv tool upgrade` uses `uv`'s recorded tool requirement:

```sh
uv tool upgrade gost-trier
```

For a GitHub install, this should update from the Git source. If you want to force a fresh reinstall from GitHub, use:

```sh
uv tool install --reinstall git+https://github.com/NightMachinery/gost-trier.git
```

For a local checkout install, rerun the local install command from the checkout after pulling or editing:

```sh
cd /path/to/gost-trier
uv tool install --reinstall .
```

## Usage

The following will find the fastest config from the given URL https://raw.githubusercontent.com/Epodonios/v2ray-configs/main/Splitted-By-Protocol/trojan.txt, and
run it in tmux with the configured SOCKS and HTTP listeners:

```sh
xray-trier -o trier_results.json --timeout=5s --run-in-tmux=xray-1080 'https://raw.githubusercontent.com/Epodonios/v2ray-configs/main/Splitted-By-Protocol/trojan.txt' -- '-L=socks5://127.0.0.1:1080' '-L=http://127.0.0.1:2080' '-F=MAGIC_FILE_1'
```

The results of this investigation are written to `trier_results.json`.

After the initial scan, both triers confirm the fastest successful configs before choosing what to run. By default they retest the top `--top-n=20` configs for `--test-n=10` total trials each, clamped to the number of working configs found. Confirmed configs are sorted by `loss`, which combines average delay, delay standard deviation, and success rate:

```text
loss = (avg-delay-ms + --loss-std-weight * std-delay-ms) / success-rate
```

The default `--loss-std-weight` is `0.2`. `--min-success-rate=0.7` is a soft threshold: if at least one confirmed config meets it, configs below it receive a large penalty so they sort after reliable configs; if none meet it, any config with a positive success rate can still be selected.

If tmux is unavailable, `--run-in-tmux` falls back to managed detached processes. Reusing the same session name cleans up previously managed processes for that session before starting new ones.

`--run-top` defaults to `5`. For `xray-trier`, `--run-in-tmux --run-top=1` launches the single lowest-loss config. When `--run-top` is greater than 1, `xray-trier` launches one Xray process with the selected configs in a balancer pool. The default balancer strategy is `--balancer-strategy=leastLoad`; `random`, `roundRobin`, and `leastPing` are also accepted. For `gost-trier`, `--run-top` launches multiple gost processes; fixed `-L` listener ports can conflict in that mode, so omit `-L` if you want free ports assigned automatically.

Sample 100 configs from a larger subscription and stop early if a fast enough config is found:

```sh
xray-trier -o trier_results.json --timeout=5s --sample=100 --enough-delay-ms=200 --run-in-tmux=xray-1080 'https://raw.githubusercontent.com/Epodonios/v2ray-configs/refs/heads/main/All_Configs_Sub.txt' -- '-L=socks5://127.0.0.1:1080' '-L=http://127.0.0.1:2080' '-F=MAGIC_FILE_1'
```

If you want to test the configs with a specific URL, you can do so using `--test-url`:

```sh
xray-trier --test-url=https://aistudio.google.com -o trier_results.json --timeout=5s --sample=100 --enough-delay-ms=200 --run-in-tmux=xray-1080 'https://raw.githubusercontent.com/Epodonios/v2ray-configs/refs/heads/main/All_Configs_Sub.txt' -- '-L=socks5://127.0.0.1:1080' '-L=http://127.0.0.1:2080' '-F=MAGIC_FILE_1'
```

See [docs/usage.md](docs/usage.md) for more.

## xray-tui

`xray-tui` provides an interactive Textual UI for grouped Xray subscriptions and manual configs:

```sh
xray-tui --socks-port=1080 --http-port=2080
```

On first run it creates `~/.xray-tui/config.yaml`, prints a short help message, and exits. Edit the YAML, then rerun `xray-tui`. Each subscription is shown as its own subgroup, plus a `Manual configs` subgroup for explicit links and JSON files. Config names are optional: link fragments like `#My%20Node` are URL-decoded, then `host:port` is used as a fallback.

The config table is numbered and shows state/latency. Starting, testing, and refreshing run in the background with status-line progress so the UI remains responsive.

Groups may set `proxy_chain` to a reusable named chain from top-level `proxy_chains`, or to an inline list. Selecting, restarting, testing, and auto-rotating inside that group routes through `selected config -> chain entries in order -> internet`.

Startup prints progress messages until the Textual screen loads. If the previously selected subscription cache is stale, it is refreshed before the TUI takes over the terminal so slow downloads and large subscription parsing remain visible.

When tmux is available, the active Xray process runs in `--tmux-session=xray-tui-s{SOCKS_PORT}-h{HTTP_PORT}` so you can inspect it with `tmux attach -t xray-tui-s1080-h2080`. By default `xray-tui` stops that process on exit; pass `--no-stop-on-exit` to leave it running.
