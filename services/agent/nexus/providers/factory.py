"""Provider factory: build the configured provider set from DB + environment.

Precedence:
1. Rows in the `providers` table (user-configured via API/UI), enabled only.
2. Environment-based fallback from Settings (NEXUS_PROVIDER etc.).
Secrets always come from SecretStore (env var or OS keyring), never the DB.
"""
from __future__ import annotations

import sqlite3

from ..core.config import Settings
from ..core.logging import get_logger
from ..core.secrets import SecretStore
from ..providers.base import AIProvider
from ..providers.mock import MockProvider
from ..providers.openai_compat import OpenAICompatibleProvider

log = get_logger("providers.factory")


def _secret_for(provider_id: str, secrets: SecretStore) -> str | None:
    env_var = f"NEXUS_{provider_id.upper().replace('-', '_')}_API_KEY"
    return secrets.get(provider_id, env_var=env_var, required=False)


def providers_from_db(conn: sqlite3.Connection, secrets: SecretStore) -> list[AIProvider]:
    out: list[AIProvider] = []
    for row in conn.execute(
            "SELECT id, kind, base_url, model_id, is_local FROM providers WHERE enabled=1"):
        pid, kind = row["id"], row["kind"]
        api_key = _secret_for(pid, secrets)
        if kind == "openai_compatible":
            out.append(OpenAICompatibleProvider(
                row["base_url"], row["model_id"], api_key,
                is_local=bool(row["is_local"])))
        elif kind == "ollama":
            out.append(OpenAICompatibleProvider(
                row["base_url"], row["model_id"], api_key, is_local=True))
        elif kind == "mock":
            out.append(MockProvider(is_local=True))
        else:
            log.warning("unknown provider kind '%s' for '%s' — skipped", kind, pid)
    return out


def default_providers(settings: Settings, secrets: SecretStore) -> list[AIProvider]:
    """Environment-based provider set used when the DB has none configured."""
    name = settings.default_provider.lower()
    if name == "mock":
        return [MockProvider(is_local=True)]
    if name in ("openai", "custom"):
        import os
        return [OpenAICompatibleProvider(
            os.environ.get("NEXUS_OPENAI_BASE_URL", "https://api.openai.com/v1"),
            os.environ.get("NEXUS_OPENAI_MODEL", "gpt-4o-mini"),
            _secret_for("openai", secrets),
            is_local=False)]
    if name == "ollama":
        import os
        return [OpenAICompatibleProvider(
            os.environ.get("NEXUS_OLLAMA_BASE_URL", "http://127.0.0.1:11434/v1"),
            os.environ.get("NEXUS_OLLAMA_MODEL", "llama3.1"),
            None, is_local=True)]
    log.warning("unknown NEXUS_PROVIDER '%s' — falling back to mock", name)
    return [MockProvider(is_local=True)]


def load_providers(conn: sqlite3.Connection, settings: Settings,
                   secrets: SecretStore) -> list[AIProvider]:
    providers = providers_from_db(conn, secrets)
    if not providers:
        providers = default_providers(settings, secrets)
    # Local-first ordering: local providers serve offline_only requests.
    providers.sort(key=lambda p: (not p.is_local,))
    log.info("loaded providers: %s", [p.name for p in providers])
    return providers
