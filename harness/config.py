import os
import re
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


# Browser name → (playwright engine, optional channel flag for real browser binaries)
BROWSER_ENGINES: dict[str, tuple[str, str | None]] = {
    "chrome": ("chromium", "chrome"),
    "chromium": ("chromium", None),
    "edge": ("chromium", "msedge"),
    "firefox": ("firefox", None),
    "safari": ("webkit", None),
    "webkit": ("webkit", None),
}


@dataclass(frozen=True)
class UserCreds:
    email: str
    password: str

    @property
    def slug(self) -> str:
        return re.sub(r"[^a-z0-9]+", "_", self.email.lower()).strip("_")


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

    # Multi-user credentials. `brya_user` always runs; `non_brya_users` alternate per test.
    brya_user: UserCreds
    non_brya_users: list[UserCreds] = field(default_factory=list)

    # Legacy single-user fields (back-compat; same data as brya_user).
    login_email: str = ""
    login_password: str = ""

    storage_state_dir: Path = Path(".auth")
    conversations_dir: Path = Path("conversations")

    # Which browsers to run. Empty = let per-directory config decide.
    # Set via --browsers CLI flag or BROWSERS env var.
    browsers_override: list[str] = field(default_factory=list)
    # "primary" | "secondary" | "all" | "" (empty means explicit list via browsers_override)
    browser_tier: str = ""

    def storage_path_for(self, user: UserCreds, browser: str = "") -> Path:
        if browser:
            return self.storage_state_dir / browser / f"{user.slug}.json"
        return self.storage_state_dir / f"{user.slug}.json"


def load_config() -> Config:
    def env(name: str, default: str = "") -> str:
        return os.environ.get(name, default)

    shared_password = env("LOGIN_PASSWORD")
    brya_email = env("LOGIN_EMAIL_BRYA") or env("LOGIN_EMAIL")
    brya_user = UserCreds(email=brya_email, password=shared_password)

    non_brya: list[UserCreds] = []
    for n in ("1", "2", "3"):
        email = env(f"LOGIN_EMAIL_NONBRYA_{n}")
        if email:
            non_brya.append(UserCreds(email=email, password=shared_password))

    # Browser selection: comma-separated list ("chrome,firefox") or tier ("primary"/"secondary"/"all").
    browsers_raw = env("BROWSERS", "")
    browser_tier = ""
    browsers_override: list[str] = []
    if browsers_raw in ("primary", "secondary", "all"):
        browser_tier = browsers_raw
    elif browsers_raw:
        browsers_override = [b.strip().lower() for b in browsers_raw.split(",") if b.strip()]

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
        brya_user=brya_user,
        non_brya_users=non_brya,
        login_email=brya_email,
        login_password=shared_password,
        storage_state_dir=Path(env("STORAGE_STATE_DIR", ".auth")),
        conversations_dir=Path(env("CONVERSATIONS_DIR", "conversations")),
        browsers_override=browsers_override,
        browser_tier=browser_tier,
    )
