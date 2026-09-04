"""Runtime registry of live agent instances.

``install_creative_routes`` registers the server's primary agent here so
late-bound tools (self-developed tools, creative tools) can be injected
into the *running* executor — the agent rebuilds its tool descriptions
from ``agent._tools`` on every turn, so injection takes effect on the
very next message.
"""

from __future__ import annotations

import threading
from typing import Any, Dict, Iterator

_LOCK = threading.RLock()
_AGENTS: Dict[str, Any] = {}


def register_agent(agent_id: str, agent: Any) -> None:
    if not agent_id or agent is None:
        return
    with _LOCK:
        _AGENTS[agent_id] = agent


def unregister_agent(agent_id: str) -> None:
    with _LOCK:
        _AGENTS.pop(agent_id, None)


def running_agents() -> Dict[str, Any]:
    with _LOCK:
        return dict(_AGENTS)


def iter_agents() -> Iterator[tuple[str, Any]]:
    with _LOCK:
        yield from _AGENTS.items()


__all__ = ["register_agent", "unregister_agent", "running_agents", "iter_agents"]
