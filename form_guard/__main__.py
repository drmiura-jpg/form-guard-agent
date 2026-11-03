from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):  # python form_guard or python form_guard/__main__.py
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from form_guard.cli import main
else:
    from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
