"""Materialize MAF ``Agent`` instances from cartridge ``AgentSpec`` declarations.

Responsibilities:
  * Pick the right chat client from environment (OpenAI, Azure OpenAI, Azure Claude).
  * Load the cartridge's system prompt, apply template substitutions
    (``{{SERVICE_MAP}}``, ``{{IDIOM_MAP}}``) from the cartridge's mappings.
  * Attach the platform's generic tools (``read_file``, ``grep``, etc.) to the
    agent; cartridges can augment via additional tool names.
  * Return a dict keyed by agent ``name`` so callers can look them up by role.

Provider selection (controlled by ``LEGACY_MOD_PROVIDER`` env var, or autodetected
from which credential env vars are set):

  * ``openai`` (default) — direct OpenAI API. Requires ``OPENAI_API_KEY``.
    Default model: ``gpt-5.4-mini`` (override via ``OPENAI_MODEL``).
  * ``azure-openai`` — Azure OpenAI Service. Requires ``AZURE_OPENAI_ENDPOINT``
    plus either ``AZURE_OPENAI_API_KEY`` or ``DefaultAzureCredential``.
    Deployment name via ``AZURE_OPENAI_DEPLOYMENT``.
  * ``azure-claude`` — Anthropic Claude via Azure AI Foundry. Requires
    ``AZURE_ANTHROPIC_ENDPOINT`` (Foundry resource) plus an API key or
    ``DefaultAzureCredential``. Model via ``AZURE_ANTHROPIC_MODEL``.
  * ``anthropic`` — Direct Anthropic API. Requires ``ANTHROPIC_API_KEY``.
    Model via ``ANTHROPIC_MODEL``.
  * ``ollama`` — Local inference via Ollama's OpenAI-compatible endpoint
    (``http://localhost:11434/v1`` by default). Model via ``OLLAMA_MODEL``
    (e.g. ``qwen3:7b``); ``ollama pull <model>`` must have been run first.

Autodetection order when ``LEGACY_MOD_PROVIDER`` is unset:
  AZURE_ANTHROPIC_ENDPOINT > AZURE_OPENAI_ENDPOINT > ANTHROPIC_API_KEY
  > OLLAMA_BASE_URL/OLLAMA_MODEL > OPENAI_API_KEY
"""
from __future__ import annotations

import inspect
import logging
import os
from pathlib import Path
from typing import Any, TYPE_CHECKING

from maf_generic_migrator_v1.platform_core.cartridge import (
    AgentSpec,
    IdiomMapping,
    MigrationCartridge,
    ServiceMapping,
)

if TYPE_CHECKING:
    from agent_framework import Agent

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Chat client factory
# --------------------------------------------------------------------------- #


DEFAULT_OPENAI_MODEL = "gpt-5.4-mini"
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-5"


def _detect_provider() -> str:
    explicit = os.getenv("LEGACY_MOD_PROVIDER")
    if explicit:
        return explicit.lower().strip()
    if os.getenv("AZURE_ANTHROPIC_ENDPOINT"):
        return "azure-claude"
    if os.getenv("AZURE_OPENAI_ENDPOINT"):
        return "azure-openai"
    if os.getenv("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.getenv("OLLAMA_BASE_URL") or os.getenv("OLLAMA_MODEL"):
        return "ollama"
    return "openai"


def build_chat_client(model: str | None = None):  # noqa: ANN201
    """Return a configured MAF chat client using env vars.

    See module docstring for the provider-selection matrix.
    """
    provider = _detect_provider()
    logger.info("chat-client: provider=%s", provider)

    if provider == "openai":
        return _build_openai(model)
    if provider == "azure-openai":
        return _build_azure_openai(model)
    if provider == "anthropic":
        return _build_anthropic(model)
    if provider == "azure-claude":
        return _build_azure_claude(model)
    if provider == "ollama":
        return _build_ollama(model)
    raise RuntimeError(f"unknown LEGACY_MOD_PROVIDER={provider!r}")


def _build_openai(model: str | None):  # noqa: ANN202
    from agent_framework.openai import OpenAIChatClient

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY is not set. Export it or switch LEGACY_MOD_PROVIDER."
        )
    resolved = model or os.getenv("OPENAI_MODEL", DEFAULT_OPENAI_MODEL)
    return OpenAIChatClient(model=resolved, api_key=api_key)


def _build_azure_openai(model: str | None):  # noqa: ANN202
    from agent_framework.openai import OpenAIChatClient

    endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
    if not endpoint:
        raise RuntimeError("AZURE_OPENAI_ENDPOINT not set.")
    deployment = (
        model
        or os.getenv("AZURE_OPENAI_DEPLOYMENT")
        or os.getenv("AZURE_OPENAI_MODEL")
        or DEFAULT_OPENAI_MODEL
    )
    api_version = os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21")
    api_key = os.getenv("AZURE_OPENAI_API_KEY")
    if api_key:
        return OpenAIChatClient(
            model=deployment,
            api_key=api_key,
            azure_endpoint=endpoint,
            api_version=api_version,
        )
    try:
        from azure.identity import DefaultAzureCredential

        cred = DefaultAzureCredential()
        return OpenAIChatClient(
            model=deployment,
            credential=cred,
            azure_endpoint=endpoint,
            api_version=api_version,
        )
    except ImportError as exc:
        raise RuntimeError(
            "AZURE_OPENAI_ENDPOINT set but neither AZURE_OPENAI_API_KEY nor "
            "azure-identity is available. `pip install 'legacy-mod-amf[azure]'`."
        ) from exc


def _build_ollama(model: str | None):  # noqa: ANN202
    """Local inference via Ollama's OpenAI-compatible endpoint.

    Requires Ollama running locally (``ollama serve``) with the target model
    already pulled (``ollama pull qwen3:7b`` etc.). The API key is ignored
    by Ollama but MAF requires a non-empty placeholder.
    """
    from agent_framework.openai import OpenAIChatClient

    base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1").rstrip("/")
    if not base_url.endswith("/v1"):
        base_url = f"{base_url}/v1"
    resolved = model or os.getenv("OLLAMA_MODEL") or os.getenv("OPENAI_MODEL", "qwen3")
    api_key = os.getenv("OLLAMA_API_KEY", "ollama")
    logger.info("ollama: base_url=%s model=%s", base_url, resolved)
    return OpenAIChatClient(model=resolved, api_key=api_key, base_url=base_url)


def _build_anthropic(model: str | None):  # noqa: ANN202
    from agent_framework.anthropic import AnthropicClient

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set.")
    resolved = model or os.getenv("ANTHROPIC_MODEL", DEFAULT_ANTHROPIC_MODEL)
    return AnthropicClient(api_key=api_key, model=resolved)


def _build_azure_claude(model: str | None):  # noqa: ANN202
    """Anthropic Claude via Azure AI Foundry."""
    from agent_framework.anthropic import AnthropicFoundryClient

    endpoint = os.getenv("AZURE_ANTHROPIC_ENDPOINT")
    if not endpoint:
        raise RuntimeError("AZURE_ANTHROPIC_ENDPOINT not set.")
    resource = os.getenv("AZURE_ANTHROPIC_RESOURCE")  # Foundry resource name
    api_key = os.getenv("AZURE_ANTHROPIC_API_KEY")
    resolved = model or os.getenv("AZURE_ANTHROPIC_MODEL", DEFAULT_ANTHROPIC_MODEL)
    if api_key:
        return AnthropicFoundryClient(
            model=resolved,
            api_key=api_key,
            resource=resource,
            base_url=endpoint,
        )
    try:
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider

        cred = DefaultAzureCredential()
        token_provider = get_bearer_token_provider(
            cred, "https://cognitiveservices.azure.com/.default"
        )
        return AnthropicFoundryClient(
            model=resolved,
            azure_ad_token_provider=token_provider,
            resource=resource,
            base_url=endpoint,
        )
    except ImportError as exc:
        raise RuntimeError(
            "AZURE_ANTHROPIC_ENDPOINT set but neither AZURE_ANTHROPIC_API_KEY nor "
            "azure-identity is available. `pip install 'legacy-mod-amf[azure]'`."
        ) from exc


# --------------------------------------------------------------------------- #
# AgentBundle — everything the unit_worker needs to invoke one agent role
# --------------------------------------------------------------------------- #


class AgentBundle:
    """A materialized MAF agent plus metadata needed to invoke it."""

    def __init__(
        self,
        spec: AgentSpec,
        agent: "Agent",
        cartridge_root: Path,
        *,
        resolved_model: str | None = None,
    ) -> None:
        self.spec = spec
        self.agent = agent
        self.cartridge_root = cartridge_root
        # Model the chat client was actually built with (after spec → env-var
        # fallback). Used by budget tracking so the per-model cost breakdown
        # isn't "unknown" when AgentSpec.model is left blank.
        self.resolved_model = resolved_model

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def role(self) -> str:
        return self.spec.role


# --------------------------------------------------------------------------- #
# Agent materialization
# --------------------------------------------------------------------------- #


def materialize_agents(cartridge: MigrationCartridge) -> dict[str, AgentBundle]:
    """Build a name-keyed dict of every agent the cartridge declares.

    Includes translators, reviewers, testers, and security agents.
    """
    from agent_framework import Agent

    cartridge_root = Path(inspect.getfile(type(cartridge))).parent

    service_map_block = _render_service_map(cartridge.service_map())
    idiom_map_block = _render_idiom_map(cartridge.idiom_map())

    bundles: dict[str, AgentBundle] = {}
    all_specs = (
        list(cartridge.translator_agents())
        + list(cartridge.reviewer_agents())
        + list(cartridge.tester_agents())
        + list(cartridge.security_agents())
    )
    for spec in all_specs:
        prompt = _load_prompt(cartridge_root, spec, service_map_block, idiom_map_block)
        client = build_chat_client(model=spec.model)
        tools = _resolve_tools(spec.tools)
        agent = Agent(
            client=client,
            instructions=prompt,
            name=spec.name,
            description=f"{cartridge.id} / {spec.role}",
            tools=tools if tools else None,
        )
        # MAF chat clients expose the deployed model on ``.model`` (Anthropic
        # + OpenAI wrappers agree). Use spec.model first, else read it back
        # from the client so budget tracking gets the concrete model ID.
        resolved_model = spec.model or getattr(client, "model", None)
        bundles[spec.name] = AgentBundle(
            spec=spec,
            agent=agent,
            cartridge_root=cartridge_root,
            resolved_model=resolved_model,
        )
    return bundles


def materialize_agents_lazy(cartridge: MigrationCartridge) -> dict[str, AgentBundle]:
    """Best-effort materialization. Logs and returns {} if MAF/creds missing.

    Use this when the platform must remain operational in environments without
    chat credentials (e.g. unit tests of the deterministic stages).
    """
    try:
        return materialize_agents(cartridge)
    except Exception as exc:  # noqa: BLE001
        logger.info("agent materialization skipped: %s", exc)
        return {}


# --------------------------------------------------------------------------- #
# Prompt rendering
# --------------------------------------------------------------------------- #


def _load_prompt(
    cartridge_root: Path,
    spec: AgentSpec,
    service_map_block: str,
    idiom_map_block: str,
) -> str:
    prompt_path = cartridge_root / spec.system_prompt_path
    text = prompt_path.read_text(encoding="utf-8")
    text = text.replace("{{SERVICE_MAP}}", service_map_block)
    text = text.replace("{{IDIOM_MAP}}", idiom_map_block)
    return text


def _render_service_map(mappings: list[ServiceMapping]) -> str:
    if not mappings:
        return "(empty)"
    lines = ["| source | target | notes |", "| --- | --- | --- |"]
    for m in mappings:
        src = m.source_kind + (f".{m.source_operation}" if m.source_operation else "")
        tgt = m.target_kind + (f".{m.target_operation}" if m.target_operation else "")
        lines.append(f"| `{src}` | `{tgt}` | {m.notes or ''} |")
    return "\n".join(lines)


def _render_idiom_map(mappings: list[IdiomMapping]) -> str:
    if not mappings:
        return "(empty)"
    parts = []
    for im in mappings:
        parts.append(f"### {im.name}\n- **from:** `{im.source_pattern}`\n- **to:** `{im.target_pattern}`\n- {im.explanation}")
    return "\n\n".join(parts)


# --------------------------------------------------------------------------- #
# Tool resolution
# --------------------------------------------------------------------------- #


_PLATFORM_TOOLS: dict[str, Any] | None = None


def _resolve_tools(tool_names: list[str]) -> list[Any]:
    """Map cartridge-declared tool names to callable tool functions.

    Unknown names are logged and skipped; the agent still works (MAF handles
    absence of tools cleanly) but we prefer explicit diagnostics.
    """
    global _PLATFORM_TOOLS
    if _PLATFORM_TOOLS is None:
        _PLATFORM_TOOLS = _build_tool_registry()

    out: list[Any] = []
    for name in tool_names:
        tool = _PLATFORM_TOOLS.get(name)
        if tool is None:
            logger.info("unknown tool '%s' — skipping", name)
            continue
        out.append(tool)
    return out


def _build_tool_registry() -> dict[str, Any]:
    """Wrap platform tools with the ``@tool`` decorator so MAF can call them."""
    from agent_framework import tool as maf_tool

    from maf_generic_migrator_v1.platform_core.tools import file_tools

    @maf_tool
    def read_file(path: str) -> str:
        """Read a source file and return its contents as text."""
        return file_tools.read_file(path)

    @maf_tool
    def write_file(path: str, content: str) -> str:
        """Write ``content`` to ``path``, creating parent dirs if needed."""
        file_tools.write_file(path, content)
        return f"wrote {len(content)} bytes to {path}"

    @maf_tool
    def glob(pattern: str, root: str = ".") -> list[str]:
        """List files matching a glob pattern under ``root``."""
        return file_tools.glob_files(pattern, root)

    @maf_tool
    def grep(pattern: str, root: str = ".", ignore_case: bool = False) -> list[dict]:
        """Search for a regex ``pattern`` across files under ``root``."""
        return file_tools.grep(pattern, root, ignore_case=ignore_case)

    @maf_tool
    def run_shell(command: str, cwd: str = ".") -> dict:
        """Run a shell command and return {exit_code, stdout, stderr}. Use for tests."""
        import subprocess

        proc = subprocess.run(
            command, cwd=cwd, shell=True, check=False, capture_output=True, text=True, timeout=300
        )
        return {"exit_code": proc.returncode, "stdout": proc.stdout[-4000:], "stderr": proc.stderr[-2000:]}

    return {
        "read_file": read_file,
        "write_file": write_file,
        "glob": glob,
        "grep": grep,
        "run_shell": run_shell,
    }
