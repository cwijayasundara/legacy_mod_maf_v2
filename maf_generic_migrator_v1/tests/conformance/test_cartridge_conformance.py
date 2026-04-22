"""Every cartridge must satisfy the minimum contract."""
from __future__ import annotations

import pytest

from maf_generic_migrator_v1.platform_core.cartridge import MigrationCartridge, list_cartridges, load_cartridge_by_id


@pytest.mark.parametrize("cartridge_id", list_cartridges())
def test_loads(cartridge_id: str) -> None:
    c = load_cartridge_by_id(cartridge_id)
    assert isinstance(c, MigrationCartridge)
    assert c.id == cartridge_id


@pytest.mark.parametrize("cartridge_id", list_cartridges())
def test_has_signatures(cartridge_id: str) -> None:
    c = load_cartridge_by_id(cartridge_id)
    assert c.source.language
    assert c.target.language


@pytest.mark.parametrize("cartridge_id", list_cartridges())
def test_declares_at_least_one_adapter(cartridge_id: str) -> None:
    c = load_cartridge_by_id(cartridge_id)
    assert c.adapters(), "cartridge must declare >=1 language adapter"


@pytest.mark.parametrize("cartridge_id", list_cartridges())
def test_declares_translator_agents(cartridge_id: str) -> None:
    c = load_cartridge_by_id(cartridge_id)
    agents = c.translator_agents()
    assert agents, "cartridge must declare >=1 translator agent"
    for a in agents:
        assert a.system_prompt_path, "each translator needs a prompt path"


@pytest.mark.parametrize("cartridge_id", list_cartridges())
def test_prompt_files_exist(cartridge_id: str) -> None:
    import inspect
    from pathlib import Path

    c = load_cartridge_by_id(cartridge_id)
    cartridge_root = Path(inspect.getfile(type(c))).parent
    for agent in c.translator_agents() + c.reviewer_agents() + c.tester_agents() + c.security_agents():
        prompt = cartridge_root / agent.system_prompt_path
        assert prompt.is_file(), f"missing prompt: {prompt}"
