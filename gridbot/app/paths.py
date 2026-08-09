"""Path resolution for the GridBot app: dev mode vs PyInstaller onefile.

Frozen (``sys.frozen``): user data lives under ``%LOCALAPPDATA%/GridBot``
(subfolders ``state/``, ``logs/``, ``data_cache/``, file ``config.json``);
the bundled ``web/`` assets are unpacked into ``sys._MEIPASS`` under
``gridbot/app/web`` (see packaging spec datas).

Dev: data lives in the existing folders inside the ``gridbot/`` package
(``gridbot/state``, ``gridbot/logs``, ``gridbot/data_cache``,
``gridbot/config.json``); ``web/`` is next to this file.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "GridBot"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def base_data_dir() -> Path:
    """Writable base folder for state/logs/cache/config.

    ``GRIDBOT_DATA_DIR`` env var overrides everything.  Frozen exe and a
    source snapshot launched via the desktop shortcut must share data, so
    a launch outside the dev repo also uses ``%LOCALAPPDATA%/GridBot``
    (the dev checkout is recognised by its ``.git``/``tests`` neighbours).
    """
    override = os.environ.get("GRIDBOT_DATA_DIR")
    if override:
        return Path(override)
    pkg_dir = Path(__file__).resolve().parent.parent
    dev_checkout = (pkg_dir / "tests").is_dir()
    if not is_frozen() and dev_checkout:
        # dev: the gridbot/ package folder (state/, logs/, data_cache/)
        return pkg_dir
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) if local else Path.home() / "AppData" / "Local"
    return base / APP_NAME


def state_dir() -> Path:
    return base_data_dir() / "state"


def logs_dir() -> Path:
    return base_data_dir() / "logs"


def data_cache_dir() -> Path:
    return base_data_dir() / "data_cache"


def config_path() -> Path:
    return base_data_dir() / "config.json"


def web_dir() -> Path:
    """Folder with index.html / styles.css / app.js."""
    if is_frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass) / "gridbot" / "app" / "web"
    return Path(__file__).resolve().parent / "web"
