from __future__ import annotations

import sys
from collections.abc import Sequence

from .common import parse_trier_args, run_trier
from .placeholders import substitute_placeholders
from .xray import run_xray_in_tmux, run_xray_test


def main(argv: Sequence[str] | None = None) -> int:
    try:
        options = parse_trier_args(
            sys.argv[1:] if argv is None else argv,
            prog="xray-trier",
            description="Try Xray configs by replacing MAGIC_FILE_N placeholders from text files.",
            default_jobs=50,
        )
        return run_trier(
            options,
            substitute=substitute_placeholders,
            run_test=run_xray_test,
            run_tmux=run_xray_in_tmux,
        )
    except BrokenPipeError:
        return 1
    except Exception as exc:
        print(f"xray-trier: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
