import os
from dataclasses import dataclass, field


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass
class Settings:
    db_path: str = field(default_factory=lambda: _env("HB_DB_PATH", "data/horizonbook.db"))
    replay_dir: str = field(default_factory=lambda: _env("HB_REPLAY_DIR", "data/replay"))

    # Tinybird: Tinybird Local by default (token auto-discovered), Tinybird Cloud when TINYBIRD_TOKEN is set.
    tinybird_host: str = field(default_factory=lambda: _env("TINYBIRD_HOST", "http://localhost:7181"))
    tinybird_token: str = field(default_factory=lambda: _env("TINYBIRD_TOKEN"))

    # Liquid: any OpenAI-compatible endpoint serving an LFM model. Default is the local llama.cpp container.
    liquid_base_url: str = field(default_factory=lambda: _env("LIQUID_BASE_URL", "http://localhost:8081/v1"))
    liquid_api_key: str = field(default_factory=lambda: _env("LIQUID_API_KEY"))
    liquid_model: str = field(default_factory=lambda: _env("LIQUID_MODEL", "LFM2.5-1.2B-Instruct"))

    nimble_base_url: str = field(default_factory=lambda: _env("NIMBLE_BASE_URL", "https://sdk.nimbleway.com/v2"))
    nimble_api_key: str = field(default_factory=lambda: _env("NIMBLE_API_KEY"))

    sandbox_url: str = field(default_factory=lambda: _env("SANDBOX_URL", "http://localhost:8000"))

    watch_interval: float = field(default_factory=lambda: float(_env("HB_WATCH_INTERVAL", "1.0")))
    tick_interval: float = field(default_factory=lambda: float(_env("HB_TICK_INTERVAL", "1.5")))
    # Minimum on-screen duration of each agent step, so a human can follow the demo.
    step_delay: float = field(default_factory=lambda: float(_env("HB_STEP_DELAY", "0.8")))
    autostart: bool = field(default_factory=lambda: _env("HB_AUTOSTART", "1") == "1")
