"""Load ``config/data.yaml`` and resolve project paths."""
# `from __future__ import annotations` makes type hints (the `: str` / `-> dict` parts) be stored
# as text instead of evaluated at import time. It lets us write modern hints like `str | None`
# ("a string or None") without worrying about Python version details.
from __future__ import annotations

from pathlib import Path  # Path objects are a friendlier way to build file paths than strings
from typing import Any

import yaml  # PyYAML: reads the human-editable config file

# __file__ is this file's path. .resolve() makes it absolute; .parents[3] walks up 3 folders:
# data -> mm -> src -> project root. Everything else is located relative to this root.
PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "data.yaml"  # `/` joins path pieces on Path objects


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """Read the YAML config (defaults to ``config/data.yaml``)."""
    # `with open(...) as fh` is a context manager: it guarantees the file is closed afterwards,
    # even if an error happens while reading.
    # `path or DEFAULT_CONFIG_PATH` means "use path if given, otherwise the default".
    with open(path or DEFAULT_CONFIG_PATH, "r", encoding="utf-8") as fh:
        # safe_load turns YAML into plain dicts/lists/numbers. (Never use plain `load` on
        # untrusted files: it can run code.)
        return yaml.safe_load(fh)


def raw_dir(cfg: dict, live: bool = False) -> Path:
    """Folder for untouched files (archive zips, or raw live websocket JSON)."""
    p = cfg["paths"]
    # Conditional expression: `A if condition else B`. Archive and live data live in different
    # sub-folders so the two never get mixed up.
    return PROJECT_ROOT / p["raw_dir"] / (p["live_name"] if live else p["archive_name"])


def processed_dir(cfg: dict, live: bool = False) -> Path:
    """Folder for normalized parquet (archive-derived, or live-recorded)."""
    p = cfg["paths"]
    # Note: if cfg["paths"]["processed_dir"] is an ABSOLUTE path, Path's `/` operator discards
    # PROJECT_ROOT. The unit tests rely on this to redirect output to a temporary folder.
    return PROJECT_ROOT / p["processed_dir"] / (p["live_name"] if live else p["archive_name"])
