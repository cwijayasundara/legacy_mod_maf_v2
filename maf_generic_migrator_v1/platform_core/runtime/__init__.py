"""Runtime concerns: CLI entry point, `.env` loading, process config.

Nothing in here should be imported by library code; it's for the
``legacy-mod`` executable and example scripts.
"""
from maf_generic_migrator_v1.platform_core.runtime.env_loader import load_env

__all__ = ["load_env"]
