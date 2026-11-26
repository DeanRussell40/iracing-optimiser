"""Writes iroptimizer/_build.py so the exe can show which build it is, and checks a release tag matches the version.

Run by the GitHub Actions build. Reads GITHUB_RUN_NUMBER, GITHUB_SHA and GITHUB_REF.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from iroptimizer import __version__  # noqa: E402

ref = os.environ.get("GITHUB_REF", "")
if ref.startswith("refs/tags/v") and ref[len("refs/tags/v"):] != __version__:
    raise SystemExit(f"Tag {ref} doesn't match iroptimizer.__version__ {__version__}: bump the version first.")
info = {"number": os.environ.get("GITHUB_RUN_NUMBER", "0"), "commit": os.environ.get("GITHUB_SHA", ""),
        "date": datetime.now(timezone.utc).strftime("%d %b %Y").lstrip("0")}
(ROOT / "iroptimizer" / "_build.py").write_text(f"BUILD = {info!r}\n", encoding="utf-8")
print("Build info:", __version__, info)
