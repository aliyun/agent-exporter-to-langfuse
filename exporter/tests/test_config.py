"""Tests for src.config — load_config and set_config_value."""

from pathlib import Path

from src.config import Config, load_config, set_config_value


class TestLoadConfig:
    def test_returns_defaults_when_file_missing(self, tmp_path: Path) -> None:
        cfg = load_config(tmp_path / "nonexistent.toml")
        assert isinstance(cfg, Config)
        assert cfg.server.host == "127.0.0.1"
        assert cfg.server.port == 5288
        assert cfg.langfuse.public_key == ""
        assert cfg.storage.max_size_gb == 20.0
        assert cfg.sender.interval_seconds == 5
        assert cfg.update.include_prerelease is False

    def test_loads_full_config(self, tmp_path: Path) -> None:
        toml_file = tmp_path / "config.toml"
        toml_file.write_text(
            '[server]\nhost = "0.0.0.0"\nport = 9999\n\n'
            '[langfuse]\npublic_key = "pk-test"\nsecret_key = "sk-test"\n'
            'base_url = "https://custom.langfuse.com"\n\n'
            '[storage]\nmax_size_gb = 5.0\nretention_days = 7\n\n'
            '[sender]\ninterval_seconds = 10\nbatch_size = 20\n\n'
            '[update]\ninclude_prerelease = true\n'
        )
        cfg = load_config(toml_file)
        assert cfg.server.host == "0.0.0.0"
        assert cfg.server.port == 9999
        assert cfg.langfuse.public_key == "pk-test"
        assert cfg.langfuse.secret_key == "sk-test"
        assert cfg.langfuse.base_url == "https://custom.langfuse.com"
        assert cfg.storage.max_size_gb == 5.0
        assert cfg.storage.retention_days == 7
        assert cfg.sender.interval_seconds == 10
        assert cfg.sender.batch_size == 20
        assert cfg.update.include_prerelease is True

    def test_partial_config_keeps_defaults(self, tmp_path: Path) -> None:
        toml_file = tmp_path / "config.toml"
        toml_file.write_text('[sender]\nbatch_size = 50\n')
        cfg = load_config(toml_file)
        assert cfg.sender.batch_size == 50
        assert cfg.sender.interval_seconds == 5  # default preserved
        assert cfg.server.host == "127.0.0.1"    # other section default

    def test_unknown_keys_ignored(self, tmp_path: Path) -> None:
        toml_file = tmp_path / "config.toml"
        toml_file.write_text('[server]\nhost = "localhost"\nfoo = "bar"\n')
        cfg = load_config(toml_file)
        assert cfg.server.host == "localhost"
        assert not hasattr(cfg.server, "foo")

    def test_tilde_expansion_in_data_dir(self, tmp_path: Path) -> None:
        toml_file = tmp_path / "config.toml"
        toml_file.write_text('[storage]\ndata_dir = "~/my-data"\n')
        cfg = load_config(toml_file)
        assert "~" not in cfg.storage.data_dir
        assert cfg.storage.data_dir == str(Path("~/my-data").expanduser())


class TestSetConfigValue:
    def test_creates_new_file_with_section_and_key(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        set_config_value("update", "include_prerelease", True, path)
        assert path.exists()
        cfg = load_config(path)
        assert cfg.update.include_prerelease is True

    def test_writes_bool_false(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        set_config_value("update", "include_prerelease", False, path)
        cfg = load_config(path)
        assert cfg.update.include_prerelease is False

    def test_writes_int_value(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        set_config_value("sender", "batch_size", 42, path)
        cfg = load_config(path)
        assert cfg.sender.batch_size == 42

    def test_writes_float_value(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        set_config_value("storage", "max_size_gb", 10.5, path)
        cfg = load_config(path)
        assert cfg.storage.max_size_gb == 10.5

    def test_writes_string_value(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        set_config_value("langfuse", "public_key", "pk-abc123", path)
        cfg = load_config(path)
        assert cfg.langfuse.public_key == "pk-abc123"

    def test_updates_existing_key(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        set_config_value("sender", "batch_size", 10, path)
        set_config_value("sender", "batch_size", 99, path)
        cfg = load_config(path)
        assert cfg.sender.batch_size == 99

    def test_adds_key_to_existing_section(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        set_config_value("sender", "batch_size", 10, path)
        set_config_value("sender", "interval_seconds", 30, path)
        cfg = load_config(path)
        assert cfg.sender.batch_size == 10
        assert cfg.sender.interval_seconds == 30

    def test_adds_new_section_to_existing_file(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        set_config_value("sender", "batch_size", 5, path)
        set_config_value("update", "include_prerelease", True, path)
        cfg = load_config(path)
        assert cfg.sender.batch_size == 5
        assert cfg.update.include_prerelease is True

    def test_creates_parent_directories(self, tmp_path: Path) -> None:
        path = tmp_path / "deep" / "nested" / "config.toml"
        set_config_value("server", "port", 1234, path)
        assert path.exists()
        cfg = load_config(path)
        assert cfg.server.port == 1234


class TestBatchSizeDefault:
    def test_default_is_10(self, tmp_path: Path) -> None:
        cfg = load_config(tmp_path / "nonexistent.toml")
        assert cfg.sender.batch_size == 10

    def test_no_max_payload_bytes(self, tmp_path: Path) -> None:
        cfg = load_config(tmp_path / "nonexistent.toml")
        assert not hasattr(cfg.sender, "max_payload_bytes")
