"""Secret storage.

Resolution order for a secret named NAME:
1. OS secure credential storage via `keyring`, if installed.
2. Environment variable (documented in .env.example).
3. Raise a helpful error telling the user exactly what to set.

API keys are NEVER written to the SQLite database, logs, or disk by this module.
"""
from __future__ import annotations

import os

from .errors import ConfigError
from .logging import get_logger

log = get_logger("secrets")


class SecretStore:
    def __init__(self, service: str = "nexus-ai"):
        self.service = service
        try:
            import keyring  # type: ignore
            self._keyring = keyring
        except ImportError:
            self._keyring = None

    def get(self, name: str, env_var: str | None = None, required: bool = True) -> str | None:
        env_var = env_var or name
        if self._keyring is not None:
            try:
                value = self._keyring.get_password(self.service, name)
                if value:
                    log.info("secret '%s' loaded from OS credential storage", name)
                    return value
            except Exception as e:  # keyring backends can fail; fall through
                log.warning("keyring lookup failed for '%s': %s", name, e)
        value = os.environ.get(env_var)
        if value:
            return value
        if required:
            raise ConfigError(
                f"Missing secret '{name}'. Set the {env_var} environment variable "
                f"(see .env.example) or store it in your OS credential manager."
            )
        return None

    def set_os_secret(self, name: str, value: str) -> bool:
        """Persist to OS credential storage. Returns False if keyring is unavailable."""
        if self._keyring is None:
            return False
        self._keyring.set_password(self.service, name, value)
        return True
