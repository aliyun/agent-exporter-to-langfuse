import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


INSTALL_DIR = Path.home() / ".agent-exporter-to-langfuse"
CONFIG_FILE = INSTALL_DIR / "config" / "config.toml"


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 5288


@dataclass
class LangfuseConfig:
    public_key: str = ""
    secret_key: str = ""
    base_url: str = "https://us.cloud.langfuse.com"


@dataclass
class StorageConfig:
    data_dir: str = str(INSTALL_DIR / "data")
    max_size_gb: float = 20.0
    retention_days: int = 30


@dataclass
class SenderConfig:
    interval_seconds: int = 5
    max_backoff_seconds: int = 300
    batch_size: int = 10
    timeout_seconds: int = 30


@dataclass
class UpdateConfig:
    include_prerelease: bool = False


@dataclass
class Config:
    server: ServerConfig = field(default_factory=ServerConfig)
    langfuse: LangfuseConfig = field(default_factory=LangfuseConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    sender: SenderConfig = field(default_factory=SenderConfig)
    update: UpdateConfig = field(default_factory=UpdateConfig)


def load_config(path: Path | None = None) -> Config:
    path = path or CONFIG_FILE
    cfg = Config()
    if not path.exists():
        return cfg

    with open(path, "rb") as f:
        raw = tomllib.load(f)

    if "server" in raw:
        for k, v in raw["server"].items():
            if hasattr(cfg.server, k):
                setattr(cfg.server, k, v)

    if "langfuse" in raw:
        for k, v in raw["langfuse"].items():
            if hasattr(cfg.langfuse, k):
                setattr(cfg.langfuse, k, v)

    if "storage" in raw:
        for k, v in raw["storage"].items():
            if hasattr(cfg.storage, k):
                setattr(cfg.storage, k, v)

    if "sender" in raw:
        for k, v in raw["sender"].items():
            if hasattr(cfg.sender, k):
                setattr(cfg.sender, k, v)

    if "update" in raw:
        for k, v in raw["update"].items():
            if hasattr(cfg.update, k):
                setattr(cfg.update, k, v)

    data_dir = cfg.storage.data_dir
    if data_dir.startswith("~"):
        cfg.storage.data_dir = str(Path(data_dir).expanduser())

    return cfg


def set_config_value(section: str, key: str, value: object,
                     path: Path | None = None) -> None:
    path = path or CONFIG_FILE
    path.parent.mkdir(parents=True, exist_ok=True)

    if isinstance(value, bool):
        toml_val = "true" if value else "false"
    elif isinstance(value, (int, float)):
        toml_val = str(value)
    elif isinstance(value, str):
        toml_val = f'"{value}"'
    else:
        toml_val = str(value)

    new_line = f"{key} = {toml_val}"
    text = path.read_text() if path.exists() else ""

    section_re = re.compile(
        rf"^\[{re.escape(section)}\]\s*$", re.MULTILINE
    )
    key_re = re.compile(
        rf"^{re.escape(key)}\s*=\s*.*$", re.MULTILINE
    )

    m_sec = section_re.search(text)
    if m_sec:
        next_sec = re.search(r"^\[", text[m_sec.end():], re.MULTILINE)
        sec_end = m_sec.end() + next_sec.start() if next_sec else len(text)
        sec_body = text[m_sec.end():sec_end]
        m_key = key_re.search(sec_body)
        if m_key:
            abs_start = m_sec.end() + m_key.start()
            abs_end = m_sec.end() + m_key.end()
            text = text[:abs_start] + new_line + text[abs_end:]
        else:
            insert_pos = m_sec.end()
            text = text[:insert_pos] + "\n" + new_line + text[insert_pos:]
    else:
        sep = "\n" if text and not text.endswith("\n") else ""
        extra_nl = "\n" if text else ""
        text = text + sep + extra_nl + f"[{section}]\n" + new_line + "\n"

    path.write_text(text)
