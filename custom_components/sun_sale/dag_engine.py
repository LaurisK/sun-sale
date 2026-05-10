"""DAG Task Engine — tiered observer-pattern directed acyclic graph executor.

Architecture:
  - Each DagNode declares its tier, output_type, and consumed types.
  - DagEngine.wire() auto-builds observer relationships by matching output_type → consumes.
    It enforces that observers must have a strictly higher tier than their subject
    (tier constraint prevents cycles and deadlocks).
  - On each run cycle the engine executes tiers in ascending order; within a tier
    all eligible nodes run in parallel (asyncio.gather).
  - When a node completes it deposits its result in NodeContext.secondary and
    notifies its registered observers (_on_upstream_ready), which record the
    satisfied dependency so they are ready when their tier is reached.

No Home Assistant imports — pure Python.
"""
from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any, TypeVar

from .events import ControlEvent
from .models import SunSaleConfig

_LOGGER = logging.getLogger(__name__)

T = TypeVar("T")


class TierViolationError(Exception):
    """Raised when a node registers as observer of a same- or higher-tier node."""


class MissingDependencyError(KeyError):
    """Raised when NodeContext.require() cannot find the requested type."""


@dataclass
class NodeContext:
    """Shared mutable context for one DAG run cycle.

    primary   — translation-layer outputs (HA state → domain types).
                Pre-populated before the DAG starts; never modified during the run.
    secondary — node outputs, keyed by output_type.
                Grows as nodes complete during the run.
    config    — structured user configuration, read-only.
    now       — cycle timestamp.
    """
    primary: dict[type, Any]
    secondary: dict[type, Any]
    config: SunSaleConfig
    now: datetime

    def get(self, t: type[T]) -> T | None:
        """Return the value for type t from primary or secondary, or None."""
        return self.primary.get(t) or self.secondary.get(t)

    def require(self, t: type[T]) -> T:
        """Return the value for type t; raise MissingDependencyError if absent."""
        v = self.get(t)
        if v is None:
            raise MissingDependencyError(t)
        return v


class DagNode(ABC):
    """Abstract base for all DAG computation nodes.

    Subclass contract (class-level attributes):
      tier:        int        — execution tier; node may only observe lower-tier nodes.
      output_type: type|None — type deposited into ctx.secondary (None = sink node).
      consumes:    list[type] — all types this node reads from NodeContext
                               (both primary and secondary types).
    """

    tier: int
    output_type: type | None
    consumes: list[type]

    def __init__(self) -> None:
        self._observers: list[DagNode] = []
        self._satisfied: set[type] = set()

    # ── Observer wiring ──────────────────────────────────────────────────────

    def add_observer(self, node: DagNode) -> None:
        """Register node as an observer of this node's output.

        Raises TierViolationError if node.tier <= self.tier.
        """
        if node.tier <= self.tier:
            raise TierViolationError(
                f"{type(node).__name__} (T{node.tier}) cannot observe "
                f"{type(self).__name__} (T{self.tier}): observer tier must be strictly higher"
            )
        self._observers.append(node)

    def _notify_observers(self) -> None:
        """Inform registered observers that this node's output_type is now available."""
        if self.output_type is not None:
            for obs in self._observers:
                obs._on_upstream_ready(self.output_type)

    def _on_upstream_ready(self, data_type: type) -> None:
        """Called by an upstream node after depositing its result."""
        self._satisfied.add(data_type)

    def all_secondary_deps_satisfied(self, ctx: NodeContext) -> bool:
        """True when every secondary dependency declared in consumes is present."""
        return all(
            t in self._satisfied or t in ctx.secondary
            for t in self.consumes
            if t not in ctx.primary
        )

    # ── Execution ────────────────────────────────────────────────────────────

    async def run(self, ctx: NodeContext) -> list[ControlEvent]:
        """Compute, deposit result into ctx.secondary, notify observers, reset state."""
        result, events = await self._compute(ctx)
        if result is not None and self.output_type is not None:
            ctx.secondary[self.output_type] = result
        self._notify_observers()
        self._satisfied.clear()
        return events

    @abstractmethod
    async def _compute(
        self, ctx: NodeContext
    ) -> tuple[Any | None, list[ControlEvent]]:
        """Implement node logic. Return (result, events)."""


class DagEngine:
    """Tiered observer DAG executor.

    Construction:
      DagEngine(nodes) — nodes are auto-wired via _wire() and grouped by tier.

    Execution:
      await engine.run(primary, config, now) → (secondary, events)

    Tiers execute in ascending order; within a tier all ready nodes run in parallel.
    """

    def __init__(self, nodes: list[DagNode]) -> None:
        self._nodes_by_tier: dict[int, list[DagNode]] = {}
        for node in nodes:
            self._nodes_by_tier.setdefault(node.tier, []).append(node)
        self._wire(nodes)

    def _wire(self, nodes: list[DagNode]) -> None:
        """Auto-wire: for each consumed secondary type, register the consuming node
        as an observer of the producing node. Raises TierViolationError on bad tiers."""
        producer: dict[type, DagNode] = {
            n.output_type: n for n in nodes if n.output_type is not None
        }
        for node in nodes:
            for dep_type in node.consumes:
                p = producer.get(dep_type)
                if p is not None:
                    p.add_observer(node)

    async def run(
        self,
        primary: dict[type, Any],
        config: SunSaleConfig,
        now: datetime,
    ) -> tuple[dict[type, Any], list[ControlEvent]]:
        """Execute all tiers in order; return (secondary outputs, control events)."""
        ctx = NodeContext(primary=primary, secondary={}, config=config, now=now)
        all_events: list[ControlEvent] = []

        for tier_num in sorted(self._nodes_by_tier):
            ready = [
                n for n in self._nodes_by_tier[tier_num]
                if n.all_secondary_deps_satisfied(ctx)
            ]
            if not ready:
                continue
            results = await asyncio.gather(*[n.run(ctx) for n in ready])
            for events in results:
                all_events.extend(events)

        return ctx.secondary, all_events


async def run_translators(
    translators: list,
    hass: Any,
    config: SunSaleConfig,
    raw_config: dict,
    now: datetime,
) -> dict[type, Any]:
    """Run all translators in parallel; skip None results (optional translators)."""
    results = await asyncio.gather(*[
        t.translate(hass, config, raw_config, now) for t in translators
    ])
    return {
        t.output_type: result
        for t, result in zip(translators, results)
        if result is not None
    }
