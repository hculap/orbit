"""Load `config/override.yaml` (optional) for label/icon/order overrides."""
from __future__ import annotations
import os
from pathlib import Path

import yaml

# Allow override path via env (default: <repo>/config/override.yaml)
DEFAULT_OVERRIDE = Path(__file__).parent.parent.parent / "config" / "override.yaml"
OVERRIDE_PATH = Path(os.environ.get("DASHBOARD_OVERRIDE", str(DEFAULT_OVERRIDE)))


def load_overrides() -> dict:
    if not OVERRIDE_PATH.is_file():
        return {}
    try:
        text = OVERRIDE_PATH.read_text()
        data = yaml.safe_load(text) or {}
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}
