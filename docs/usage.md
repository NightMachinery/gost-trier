# Usage

Install and run with uv:

```sh
uv run gost-trier [--test-url=https://myip.wtf/json] [--shuffle] [--timeout=20s] [--jobs=1] FILE [FILE ...] -- GOST_ARGS...
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

To launch the fastest working configs in tmux after testing:

```sh
uv run gost-trier --run-in-tmux=gost --run-top=3 trojan.txt -- -L=socks5://127.0.0.1:1050 -F=MAGIC_FILE_1
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
