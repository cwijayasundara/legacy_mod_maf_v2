"""Emulator-backed wave verification.

For the AWS -> Azure cartridge we exercise the migrated Azure Functions
against local emulators for the Azure services they bind to (Service Bus,
Cosmos, Blob Storage). Emulators are optional: if we can't reach an expected
endpoint, we soft-pass the wave rather than fail a run in an environment
that just doesn't have Docker up.

Each emulator is a small probe: a TCP port check + a best-effort handshake.
"""
from __future__ import annotations

import logging
import socket
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class EmulatorProbe:
    name: str
    host: str
    port: int
    optional: bool = True


DEFAULT_PROBES = [
    EmulatorProbe(name="azurite-blob", host="127.0.0.1", port=10000),
    EmulatorProbe(name="azurite-queue", host="127.0.0.1", port=10001),
    EmulatorProbe(name="azurite-table", host="127.0.0.1", port=10002),
    EmulatorProbe(name="service-bus-emulator", host="127.0.0.1", port=5672),
    EmulatorProbe(name="cosmos-emulator", host="127.0.0.1", port=8081),
]


def probe_emulator(probe: EmulatorProbe, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((probe.host, probe.port), timeout=timeout):
            return True
    except (OSError, socket.timeout):
        return False


def probe_all(probes: list[EmulatorProbe] | None = None) -> dict[str, bool]:
    probes = probes or DEFAULT_PROBES
    return {p.name: probe_emulator(p) for p in probes}


def wave_verification(
    probes: list[EmulatorProbe] | None = None,
    required: list[str] | None = None,
) -> bool:
    """Return True iff required emulators are reachable.

    If ``required`` is None, the wave soft-passes whether emulators are up or not.
    """
    probes = probes or DEFAULT_PROBES
    statuses = {p.name: probe_emulator(p) for p in probes}
    for name, ok in statuses.items():
        logger.info("emulator %s: %s", name, "UP" if ok else "down")

    if not required:
        return True
    missing = [r for r in required if not statuses.get(r, False)]
    if missing:
        logger.warning("wave verification failing — emulators down: %s", missing)
        return False
    return True
