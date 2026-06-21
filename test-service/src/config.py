import sys
from dataclasses import dataclass, field
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib


BASE_DIR = Path.home() / ".langstash-tester"


@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 5289


@dataclass
class GitConfig:
    repo_url: str = ""
    local_repo: str = str(BASE_DIR / "repo")
    worktree_dir: str = str(BASE_DIR / "worktrees")


@dataclass
class StorageConfig:
    db_path: str = str(BASE_DIR / "data" / "langstash-tester.db")
    log_dir: str = str(BASE_DIR / "logs")


@dataclass
class E2EConfig:
    default_test_dir: str = "tests/e2e"
    max_concurrent: int = 1
    default_timeout_seconds: int = 1800
    result_retention_days: int = 30
    same_branch_policy: str = "replace"


@dataclass
class WebhookConfig:
    retry_count: int = 3
    retry_delays: list[int] = field(default_factory=lambda: [5, 15, 30])
    timeout_seconds: int = 10


@dataclass
class Config:
    server: ServerConfig = field(default_factory=ServerConfig)
    git: GitConfig = field(default_factory=GitConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    e2e: E2EConfig = field(default_factory=E2EConfig)
    webhook: WebhookConfig = field(default_factory=WebhookConfig)


def _apply_section(target, data: dict) -> None:
    for key, value in data.items():
        if hasattr(target, key):
            current = getattr(target, key)
            if isinstance(current, list) and isinstance(value, list):
                setattr(target, key, value)
            elif not isinstance(current, (dict, list)) or isinstance(value, (str, int, float, bool)):
                setattr(target, key, value)


def load_config(config_path: Path | None = None) -> Config:
    cfg = Config()

    if config_path is None:
        config_path = BASE_DIR / "config" / "config.toml"

    if config_path.exists():
        with open(config_path, "rb") as f:
            raw = tomllib.load(f)

        if "server" in raw:
            _apply_section(cfg.server, raw["server"])
        if "git" in raw:
            _apply_section(cfg.git, raw["git"])
        if "storage" in raw:
            _apply_section(cfg.storage, raw["storage"])
        if "e2e" in raw:
            _apply_section(cfg.e2e, raw["e2e"])
        if "webhook" in raw:
            _apply_section(cfg.webhook, raw["webhook"])

    if not cfg.git.repo_url:
        print("ERROR: git.repo_url is required. Set it in the config file.", file=sys.stderr)
        sys.exit(1)

    if cfg.e2e.same_branch_policy not in ("replace", "queue", "reject"):
        print(f"ERROR: e2e.same_branch_policy must be replace|queue|reject, got '{cfg.e2e.same_branch_policy}'",
              file=sys.stderr)
        sys.exit(1)

    return cfg


def read_version() -> str:
    for candidate in [
        Path(__file__).resolve().parent.parent.parent / "VERSION",
        Path(__file__).resolve().parent.parent / "VERSION",
    ]:
        if candidate.exists():
            return candidate.read_text().strip()
    return "unknown"
