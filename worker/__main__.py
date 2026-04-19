"""Entry point: `python -m worker` dispatches to cli.main()."""
from __future__ import annotations

from worker.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
