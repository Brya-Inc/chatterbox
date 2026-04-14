import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


@dataclass(frozen=True)
class Config:
    base_url: str
    headless: bool
    response_timeout_ms: int
    stable_ms: int
    response_selector: str

    openai_api_key: str
    judge_model: str

    playwright_login_url: str
    playwright_auth_key: str
    login_email: str
    login_password: str

    storage_state_path: Path
    conversations_dir: Path


def load_config() -> Config:
    def env(name: str, default: str = "") -> str:
        return os.environ.get(name, default)

    return Config(
        base_url=env("BASE_URL", "https://www.dev.brya.com").rstrip("/"),
        headless=env("HEADLESS", "1") != "0",
        response_timeout_ms=int(env("RESPONSE_TIMEOUT_MS", "20000")),
        stable_ms=int(env("STABLE_MS", "1500")),
        response_selector=env("RESPONSE_SELECTOR", ".cs-message-list .bg-tertiary"),
        openai_api_key=env("OPENAI_API_KEY"),
        judge_model=env("JUDGE_MODEL", "gpt-4o-mini"),
        playwright_login_url=env("PLAYWRIGHT_LOGIN_URL"),
        playwright_auth_key=env("PLAYWRIGHT_AUTH_KEY"),
        login_email=env("LOGIN_EMAIL"),
        login_password=env("LOGIN_PASSWORD"),
        storage_state_path=Path(env("STORAGE_STATE_PATH", ".auth/state.json")),
        conversations_dir=Path(env("CONVERSATIONS_DIR", "conversations")),
    )
