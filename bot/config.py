import os
from pathlib import Path

import yaml


def load_config(path: str = "config.yaml") -> dict:
    with open(path) as f:
        config = yaml.safe_load(f)
    config["hledger"]["journal_path"] = os.path.expanduser(
        config["hledger"]["journal_path"]
    )
    return config


def journal_dir(config: dict) -> Path:
    return Path(config["hledger"]["journal_path"]).parent


def state_path(config: dict) -> Path:
    return journal_dir(config) / "state.json"


def merchant_map_path(config: dict) -> Path:
    return journal_dir(config) / "merchant_map.json"
