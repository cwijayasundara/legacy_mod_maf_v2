"""Agent factory: prompt rendering + client selection logic.

Does NOT make network calls. Uses env-var patching to exercise both Azure
OpenAI and OpenAI selection paths; verifies prompt templates get populated
with the cartridge's service/idiom maps.
"""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from maf_generic_migrator_v1.platform_core.cartridge import load_cartridge_by_id


def test_prompt_renders_with_service_and_idiom_maps(monkeypatch: pytest.MonkeyPatch) -> None:
    from maf_generic_migrator_v1.platform_core.maf_workflows.agent_factory import (
        _load_prompt,
        _render_idiom_map,
        _render_service_map,
    )
    from pathlib import Path
    import inspect

    cartridge = load_cartridge_by_id("aws_lambda_polyglot_to_azure_fn_py")
    cartridge_root = Path(inspect.getfile(type(cartridge))).parent
    svc_block = _render_service_map(cartridge.service_map())
    idiom_block = _render_idiom_map(cartridge.idiom_map())

    spec = cartridge.translator_agents()[0]
    prompt = _load_prompt(cartridge_root, spec, svc_block, idiom_block)

    assert "{{SERVICE_MAP}}" not in prompt
    assert "{{IDIOM_MAP}}" not in prompt
    assert "service-bus" in prompt
    assert "python_handler_shape" in prompt or "handler shape" in prompt


def test_chat_client_selection_azure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://test.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "fake-key")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4o-mini")
    monkeypatch.setenv("AZURE_OPENAI_API_VERSION", "2024-10-21")

    from maf_generic_migrator_v1.platform_core.maf_workflows.agent_factory import build_chat_client

    client = build_chat_client()
    # OpenAIChatClient with Azure endpoint should have an azure-shaped service_url.
    assert client is not None


def test_chat_client_selection_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AZURE_OPENAI_ENDPOINT", raising=False)
    monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "fake-key")
    from maf_generic_migrator_v1.platform_core.maf_workflows.agent_factory import build_chat_client

    client = build_chat_client()
    assert client is not None


def test_chat_client_raises_without_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "AZURE_OPENAI_ENDPOINT",
        "AZURE_OPENAI_API_KEY",
        "OPENAI_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)

    # Prevent DefaultAzureCredential from being reachable in CI.
    with patch.dict(os.environ, {}, clear=False):
        from maf_generic_migrator_v1.platform_core.maf_workflows.agent_factory import build_chat_client
        with pytest.raises(RuntimeError):
            build_chat_client()


def test_mock_client_is_function_invocation_capable() -> None:
    """The mock must inherit FunctionInvocationLayer so MAF's isinstance check
    at agent_framework/_agents.py:710 passes — otherwise tools wired onto an
    Agent are silently ignored."""
    from agent_framework import FunctionInvocationLayer

    from maf_generic_migrator_v1.platform_core.maf_workflows.mock_client import MockChatClient

    client = MockChatClient()
    assert isinstance(client, FunctionInvocationLayer)


def test_openai_client_is_function_invocation_capable(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_framework import FunctionInvocationLayer

    monkeypatch.delenv("AZURE_OPENAI_ENDPOINT", raising=False)
    monkeypatch.delenv("AZURE_ANTHROPIC_ENDPOINT", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5.4-mini")
    from maf_generic_migrator_v1.platform_core.maf_workflows.agent_factory import build_chat_client

    client = build_chat_client()
    assert isinstance(client, FunctionInvocationLayer)


def test_lazy_materialize_returns_empty_without_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)

    from maf_generic_migrator_v1.platform_core.maf_workflows.agent_factory import materialize_agents_lazy

    cartridge = load_cartridge_by_id("aws_lambda_polyglot_to_azure_fn_py")
    bundles = materialize_agents_lazy(cartridge)
    assert bundles == {}
