from __future__ import annotations

import re
from collections.abc import Sequence


PLACEHOLDER_RE = re.compile(r"MAGIC_FILE_(\d+)")


def substitute_placeholders(args: Sequence[str], values: Sequence[str]) -> list[str]:
    def replace(match: re.Match[str]) -> str:
        index = int(match.group(1)) - 1
        if index < 0 or index >= len(values):
            raise ValueError(f"{match.group(0)} has no matching input file")
        return values[index]

    return [PLACEHOLDER_RE.sub(replace, arg) for arg in args]
