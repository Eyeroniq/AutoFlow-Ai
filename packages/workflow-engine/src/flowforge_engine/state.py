"""State a node keeps between runs of the same workflow (e.g. which RSS items it has seen).

Nodes read their state with `await context.services.state.load(node_id)` and write it with
`save`. The API stores it per workflow and node, tied to the execution that wrote it, and
`load` returns what the most recent *successful* run saved: if a later node of a run fails,
the next run sees the same items again instead of losing them.
"""

from __future__ import annotations

import copy
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class NodeStateStore(Protocol):
    async def load(self, node_id: str) -> dict[str, Any] | None:
        """The state saved by the last successful run of this node, or None."""
        ...

    async def save(self, node_id: str, state: dict[str, Any]) -> None:
        """Record this run's state (it counts once the run succeeds)."""
        ...


class MemoryStateStore:
    """In-process store: what one ExecutionServices instance saved (tests, one-off runs)."""

    def __init__(self) -> None:
        self._states: dict[str, dict[str, Any]] = {}

    async def load(self, node_id: str) -> dict[str, Any] | None:
        state = self._states.get(node_id)
        return copy.deepcopy(state) if state is not None else None

    async def save(self, node_id: str, state: dict[str, Any]) -> None:
        self._states[node_id] = copy.deepcopy(state)


class ReadOnlyStateStore:
    """Loads through another store but never saves (a single-node test run must not move
    a feed's "since last run" position)."""

    def __init__(self, inner: NodeStateStore) -> None:
        self._inner = inner

    async def load(self, node_id: str) -> dict[str, Any] | None:
        return await self._inner.load(node_id)

    async def save(self, node_id: str, state: dict[str, Any]) -> None:
        return None
