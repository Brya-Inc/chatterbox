"""
Per-directory config file loader.

Each `conversations/<category>/config.yaml` tells the harness which users and
browsers to run each test with. Format:

    users:
      always:
        - email: mike@brya.com
      rotate:
        - email: mikekutliroff.is13@gmail.com
        - email: ariannalmark@gmail.com

    rotation: alternating      # strict A, B, A, B across tests in this directory
    failure_handling: run_all  # run all user variants regardless of failures

    browsers:
      primary:
        - chrome
        - safari
      secondary:
        - firefox
        - edge
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .config import BROWSER_ENGINES, Config, UserCreds

DEFAULT_PRIMARY = ["chrome", "safari"]
DEFAULT_SECONDARY = ["firefox", "edge"]


@dataclass
class DirConfig:
    always: list[str] = field(default_factory=list)   # emails
    rotate: list[str] = field(default_factory=list)   # emails
    rotation: str = "alternating"
    failure_handling: str = "run_all"
    browsers_primary: list[str] = field(default_factory=lambda: list(DEFAULT_PRIMARY))
    browsers_secondary: list[str] = field(default_factory=lambda: list(DEFAULT_SECONDARY))

    def users_for_test(self, test_index: int, cfg: Config) -> list[UserCreds]:
        """Return the ordered list of users to run test N against."""
        by_email = {u.email: u for u in [cfg.brya_user, *cfg.non_brya_users]}
        out: list[UserCreds] = []

        for email in self.always:
            if email in by_email:
                out.append(by_email[email])

        if self.rotate:
            picked = self.rotate[test_index % len(self.rotate)]
            if picked in by_email:
                out.append(by_email[picked])

        return out

    def browsers_to_run(self, cfg: Config, test_index: int = 0) -> list[str]:
        """
        Resolve which browsers to run for test N in this directory.

        Default behavior: ALL primary browsers always run, plus one alternating
        secondary browser (strict rotation by test_index).
        So per test: len(primary) + 1 browser runs.

        CLI/env overrides:
          - cfg.browsers_override (explicit list) → exactly those, no rotation
          - cfg.browser_tier="all" → primary + every secondary (no rotation)
          - cfg.browser_tier="primary" → just primary (no secondary)
          - cfg.browser_tier="secondary" → just the rotating secondary (no primary)
        """
        if cfg.browsers_override:
            return [b for b in cfg.browsers_override if b in BROWSER_ENGINES]

        tier = cfg.browser_tier
        if tier == "all":
            return list(self.browsers_primary) + list(self.browsers_secondary)
        if tier == "primary":
            return list(self.browsers_primary)
        if tier == "secondary":
            if not self.browsers_secondary:
                return []
            return [self.browsers_secondary[test_index % len(self.browsers_secondary)]]

        # Default: primary always + one rotating secondary
        out = list(self.browsers_primary)
        if self.browsers_secondary:
            out.append(self.browsers_secondary[test_index % len(self.browsers_secondary)])
        return out


def load_dir_config(dir_path: Path) -> DirConfig:
    """Load config.yaml from a directory. Return defaults if missing."""
    cfg_path = dir_path / "config.yaml"
    if not cfg_path.exists():
        return DirConfig()

    with cfg_path.open() as f:
        data = yaml.safe_load(f) or {}

    users = data.get("users") or {}
    always = [u["email"] for u in (users.get("always") or []) if isinstance(u, dict) and "email" in u]
    rotate = [u["email"] for u in (users.get("rotate") or []) if isinstance(u, dict) and "email" in u]

    browsers = data.get("browsers") or {}
    primary = [b.lower() for b in (browsers.get("primary") or []) if isinstance(b, str)]
    secondary = [b.lower() for b in (browsers.get("secondary") or []) if isinstance(b, str)]

    return DirConfig(
        always=always,
        rotate=rotate,
        rotation=data.get("rotation", "alternating"),
        failure_handling=data.get("failure_handling", "run_all"),
        browsers_primary=primary or list(DEFAULT_PRIMARY),
        browsers_secondary=secondary or list(DEFAULT_SECONDARY),
    )
