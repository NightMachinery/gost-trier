# gost-trier

`gost-trier` tries proxy configurations generated from text files and reports the working ones as JSON.

## Install

Install uv if needed; on Linux/macOS:

```sh
curl -LsSf https://astral.sh/uv/install.sh | INSTALLER_PRINT_VERBOSE=1 sh
```

On Windows, you can install uv using Powershell:
```
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Install the commands locally with uv:

```sh
uv tool install 'git+https://github.com/NightMachinery/gost-trier.git'
```

From an existing local checkout:

```sh
cd /path/to/gost-trier
uv tool install .
```

This installs `gost-trier`, `xray-trier`, and `xray-run` so they can be run without `uv run`:

```sh
xray-trier --timeout=5s 'https://raw.githubusercontent.com/Epodonios/v2ray-configs/main/Splitted-By-Protocol/trojan.txt' -- -F=MAGIC_FILE_1
```

Run the fastest result in tmux with SOCKS and HTTP listeners:

```sh
xray-trier --timeout=5s --run-in-tmux=xray-1080 'https://raw.githubusercontent.com/Epodonios/v2ray-configs/main/Splitted-By-Protocol/trojan.txt' -- -L=socks5://127.0.0.1:1080 -L=http://127.0.0.1:2080 -F=MAGIC_FILE_1
```

To upgrade later, `uv tool upgrade` uses uv's recorded tool requirement:

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

See [docs/usage.md](docs/usage.md) for CLI usage.
