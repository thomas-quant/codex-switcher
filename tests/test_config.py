from codex_switch.config import load_app_config
from codex_switch.models import AppConfig, ListFormat


def test_load_app_config_defaults_to_labelled_when_file_is_missing(tmp_path):
    config_file = tmp_path / "config.json"

    assert load_app_config(config_file) == AppConfig()


def test_load_app_config_defaults_to_labelled_when_json_is_invalid(tmp_path):
    config_file = tmp_path / "config.json"
    config_file.write_text("{not json", encoding="utf-8")

    assert load_app_config(config_file) == AppConfig()


def test_load_app_config_defaults_to_labelled_when_value_is_unknown(tmp_path):
    config_file = tmp_path / "config.json"
    config_file.write_text('{"list_format":"wide"}', encoding="utf-8")

    assert load_app_config(config_file) == AppConfig()


def test_load_app_config_reads_table_mode(tmp_path):
    config_file = tmp_path / "config.json"
    config_file.write_text('{"list_format":"table"}', encoding="utf-8")

    assert load_app_config(config_file) == AppConfig(list_format=ListFormat.TABLE)
