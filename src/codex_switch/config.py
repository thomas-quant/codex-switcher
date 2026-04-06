from __future__ import annotations

import json
from pathlib import Path

from codex_switch.models import AppConfig, ListFormat


def load_app_config(config_file: Path) -> AppConfig:
    try:
        text = config_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        return AppConfig()
    except OSError:
        return AppConfig()

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return AppConfig()

    if not isinstance(payload, dict):
        return AppConfig()

    raw_list_format = payload.get("list_format", ListFormat.LABELLED.value)
    if raw_list_format == ListFormat.TABLE.value:
        return AppConfig(list_format=ListFormat.TABLE)
    return AppConfig()
