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

This installs `gost-trier`, `xray-trier`, and `xray-run`.

`xray-run` and `xray-trier` auto-bootstrap native helpers when needed. They first honor explicit binary overrides, then use cached release binaries for Xray and `Xray-Link-Json` under `~/.cache/gost-trier/bin/`, then fall back to binaries already on `PATH`. Release archive downloads and remote candidate-list downloads show byte progress on stderr by default; use `--no-progress` to hide progress bars. If a proxy is active for downloads, the first download prints `Using proxy for downloads: ...` with credentials redacted. `Xray-Link-Json` falls back to `go install` only if the release download is not available. Advanced users can override these paths with `XRAY_BIN` and `XRAY_LINK_JSON_BIN`.

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

If tmux is unavailable, `--run-in-tmux` falls back to managed detached processes. Reusing the same session name cleans up previously managed processes for that session before starting new ones.

Sample 100 configs from a larger subscription and stop early if a fast enough config is found:

```sh
xray-trier -o trier_results.json --timeout=5s --sample=100 --enough-delay-ms=200 --run-in-tmux=xray-1080 'https://raw.githubusercontent.com/Epodonios/v2ray-configs/refs/heads/main/All_Configs_Sub.txt' -- '-L=socks5://127.0.0.1:1080' '-L=http://127.0.0.1:2080' '-F=MAGIC_FILE_1'
```

See [docs/usage.md](docs/usage.md) for more.
