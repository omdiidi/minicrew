"""Entrypoint so `python -m worker.platform ...` dispatches to _main()."""
from worker.platform import _main

raise SystemExit(_main())
