from __future__ import annotations

import json
import shlex
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any


def verbose_log(verbose: int, level: int, message: str) -> None:
    if verbose >= level:
        print(f"xray-run: {message}", file=sys.stderr)


def command_text(command: Sequence[str | Path]) -> str:
    return " ".join(shlex.quote(str(part)) for part in command)


def run_logged(
    command: Sequence[str | Path],
    *,
    verbose: int = 0,
    log_prefix: str = "xray-run",
    **kwargs: Any,
) -> subprocess.CompletedProcess[str]:
    if verbose >= 2:
        print(f"{log_prefix}: running: {command_text(command)}", file=sys.stderr)
    completed = subprocess.run(
        [str(part) for part in command],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        **kwargs,
    )
    if verbose >= 2:
        print(f"{log_prefix}: return code: {completed.returncode}", file=sys.stderr)
        print_stream(log_prefix, "stdout", completed.stdout)
        print_stream(log_prefix, "stderr", completed.stderr)
    return completed


def print_stream(log_prefix: str, name: str, value: str) -> None:
    if value:
        print(f"{log_prefix}: {name}:\n{value.rstrip()}", file=sys.stderr)
    else:
        print(f"{log_prefix}: {name}: <empty>", file=sys.stderr)


def completed_process_details(
    completed: subprocess.CompletedProcess[str],
    *,
    command: Sequence[str | Path],
) -> str:
    return "\n".join(
        [
            f"command: {command_text(command)}",
            f"return code: {completed.returncode}",
            f"stdout:\n{completed.stdout.rstrip() or '<empty>'}",
            f"stderr:\n{completed.stderr.rstrip() or '<empty>'}",
        ]
    )


def dump_json_for_debug(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True)
