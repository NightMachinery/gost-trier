# gost-trier

`gost-trier` tries proxy configurations generated from text files and reports the working ones as JSON.

## Install

Install the commands locally with uv:

```sh
uv tool install git+https://github.com/NightMachinery/gost-trier.git
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

To upgrade later:

```sh
uv tool upgrade gost-trier
```

See [docs/usage.md](docs/usage.md) for CLI usage.
