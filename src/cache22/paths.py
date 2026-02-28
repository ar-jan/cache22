from __future__ import annotations

import os
from pathlib import Path


def config_home() -> Path:
    xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
    if xdg_config_home:
        return Path(xdg_config_home)

    return Path.home() / ".config"


def config_dir() -> Path:
    return config_home() / "cache22"


def config_file() -> Path:
    return config_dir() / "config.toml"
