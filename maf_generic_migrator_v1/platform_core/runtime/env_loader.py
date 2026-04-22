"""Inject keys (OPENAI_API_KEY, AZURE_*, ANTHROPIC_API_KEY, ...) from `.env`.

Entry points (CLI, example scripts) call :func:`load_env` early so that
``os.getenv(...)`` calls in :mod:`maf_generic_migrator_v1.platform_core.maf_workflows.agent_factory`
see the credentials defined in the project-root ``.env`` file.

Existing process environment variables always win over ``.env`` values, so
shell exports continue to override the file.
"""
from __future__ import annotations

import logging
from pathlib import Path

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]


def load_env(path: Path | None = None) -> bool:
    """Load `.env` into ``os.environ``. Returns True if a file was found."""
    target = path if path is not None else _REPO_ROOT / ".env"
    found = load_dotenv(dotenv_path=target, override=False)
    if found:
        logger.debug("loaded env from %s", target)
    return found
