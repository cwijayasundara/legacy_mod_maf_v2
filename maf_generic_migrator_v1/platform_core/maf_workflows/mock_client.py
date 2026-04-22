"""MockChatClient — canned ``BaseChatClient`` for tests and offline demos.

Lets the platform exercise the full LLM pipeline (prompt rendering, tool
wiring, response parsing, reviewer loop) without any network call or API key.

Composition follows MAF's real chat clients: ``FunctionInvocationLayer`` is
mixed in so MAF's tool-call loop runs transparently (``isinstance(client,
FunctionInvocationLayer)`` check at ``agent_framework/_agents.py:710``).

Usage::

    client = MockChatClient(
        responses_by_agent={
            "translator-python": [TRANSLATOR_RESPONSE_MD],
            "reviewer":          [REVIEWER_JSON],
            "tester":            ['{"status":"passed"}'],
            "security":          ['{"findings":[], "verdict":"pass"}'],
        }
    )

The ``materialize_mock_agents(cartridge, client)`` helper builds a full
``{name: AgentBundle}`` dict using this client so tests can bypass
``build_chat_client``.
"""
from __future__ import annotations

import logging
from collections.abc import Sequence

from agent_framework import (
    BaseChatClient,
    ChatResponse,
    FunctionInvocationLayer,
    Message,
    UsageDetails,
)

logger = logging.getLogger(__name__)


class MockChatClient(FunctionInvocationLayer, BaseChatClient):
    """A chat client that returns canned responses keyed by agent name.

    The agent's ``name`` is passed through in ``options['model']`` by MAF in
    some configurations; to make lookup reliable we also sniff the *first*
    system message for an agent name marker. Fallback is a round-robin over
    a default response list.
    """

    DEFAULT_EXCLUDE = set()

    def __init__(
        self,
        responses_by_agent: dict[str, list[str]] | None = None,
        default_responses: list[str] | None = None,
    ) -> None:
        super().__init__()
        self._responses = responses_by_agent or {}
        self._defaults = default_responses or ["(mock response)"]
        self._cursor: dict[str, int] = {}

    @property
    def service_url(self) -> str | None:  # type: ignore[override]
        return "mock://local"

    async def _inner_get_response(  # type: ignore[override]
        self,
        *,
        messages: Sequence[Message],
        stream: bool,
        options,
        **kwargs,
    ) -> ChatResponse:
        if stream:
            raise NotImplementedError("MockChatClient does not implement streaming")

        agent_key = self._pick_agent_key(messages, options)
        text = self._next_response(agent_key)

        return ChatResponse(
            messages=[Message(role="assistant", contents=[text])],
            finish_reason="stop",
            usage_details=UsageDetails(input_token_count=50, output_token_count=120, total_token_count=170),
        )

    # ------------------------------------------------------------------ #
    # Response routing
    # ------------------------------------------------------------------ #

    def _pick_agent_key(self, messages: Sequence[Message], options) -> str:
        """Find which canned response bucket to use.

        Agents built by ``materialize_mock_agents`` inject the literal marker
        ``[agent:<name>]`` at the top of the ``instructions`` prompt, which MAF
        passes through in ``options["instructions"]``. We scan that (plus any
        system messages) for any of the configured response keys.
        """
        candidates: list[str] = []
        if isinstance(options, dict):
            instr = options.get("instructions")
            if isinstance(instr, str):
                candidates.append(instr)
        for m in messages:
            if getattr(m, "role", None) == "system":
                candidates.append(_message_text(m))

        # Only match the explicit ``[agent:<name>]`` marker. Falling back to
        # substring matching is unsafe because prompts can casually mention
        # other roles (e.g. "Security reviewer" contains "reviewer").
        keys = sorted(self._responses, key=len, reverse=True)
        for text in candidates:
            for key in keys:
                if f"[agent:{key}]" in text:
                    return key
        return next(iter(self._responses), "default")

    def _next_response(self, key: str) -> str:
        bucket = self._responses.get(key) or self._defaults
        i = self._cursor.get(key, 0) % len(bucket)
        self._cursor[key] = i + 1
        return bucket[i]


def _message_text(msg: Message) -> str:
    bits: list[str] = []
    for c in getattr(msg, "contents", []) or []:
        text = getattr(c, "text", None) or (c if isinstance(c, str) else None)
        if text:
            bits.append(text)
    return "\n".join(bits)


# --------------------------------------------------------------------------- #
# Convenience: build a full AgentBundle dict from a cartridge using this mock
# --------------------------------------------------------------------------- #


def materialize_mock_agents(cartridge, client: MockChatClient) -> dict:
    """Build AgentBundle dict like ``materialize_agents`` but with the mock client."""
    import inspect
    from pathlib import Path

    from agent_framework import Agent

    from .agent_factory import (
        AgentBundle,
        _build_tool_registry,
        _load_prompt,
        _render_idiom_map,
        _render_service_map,
    )

    cartridge_root = Path(inspect.getfile(type(cartridge))).parent
    svc_block = _render_service_map(cartridge.service_map())
    idiom_block = _render_idiom_map(cartridge.idiom_map())

    bundles: dict = {}
    tool_registry = _build_tool_registry()
    all_specs = (
        list(cartridge.translator_agents())
        + list(cartridge.reviewer_agents())
        + list(cartridge.tester_agents())
        + list(cartridge.security_agents())
    )
    for spec in all_specs:
        prompt = _load_prompt(cartridge_root, spec, svc_block, idiom_block)
        prompt = f"[agent:{spec.name}]\n" + prompt  # inject marker for mock routing
        tools = [tool_registry[t] for t in spec.tools if t in tool_registry] or None
        agent = Agent(
            client=client,
            instructions=prompt,
            name=spec.name,
            description=f"{cartridge.id} / {spec.role}",
            tools=tools,
        )
        bundles[spec.name] = AgentBundle(spec=spec, agent=agent, cartridge_root=cartridge_root)
    return bundles
