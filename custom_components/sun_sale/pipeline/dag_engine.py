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

from ..contract.events import ControlEvent
from ..contract.models import SunSaleConfig

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
        """Look up type t in primary then secondary context; return None if absent.

        Args:
            t: The type key to look up.

        Returns:
            The stored value, or None.
        """
        return self.primary.get(t) or self.secondary.get(t)

    def require(self, t: type[T]) -> T:
        """Return the value for type t; raise MissingDependencyError if absent.

        Args:
            t: The type key to look up.

        Returns:
            The stored value (never None).

        Raises:
            MissingDependencyError: When t is not found in primary or secondary.
        """
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
        """Initialise empty observer list and satisfied-dependency set."""
        self._observers: list[DagNode] = []
        self._satisfied: set[type] = set()

    # ── Observer wiring ──────────────────────────────────────────────────────

    def add_observer(self, node: DagNode) -> None:
        """Register node as an observer of this node's output.

        Args:
            node: Downstream node to notify when this node completes.

        Raises:
            TierViolationError: When node.tier <= self.tier.
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
        """Record that an upstream dependency of the given type is now available.

        Args:
            data_type: The output_type that was just deposited by the upstream node.
        """
        self._satisfied.add(data_type)

    def all_secondary_deps_satisfied(self, ctx: NodeContext) -> bool:
        """Return True when every secondary dependency declared in consumes is available.

        Args:
            ctx: Current DAG run context.

        Returns:
            True if every non-primary consumed type is in ctx.secondary or _satisfied.
        """
        return all(
            t in self._satisfied or t in ctx.secondary
            for t in self.consumes
            if t not in ctx.primary
        )

    # ── Execution ────────────────────────────────────────────────────────────

    async def run(self, ctx: NodeContext) -> list[ControlEvent]:
        """Execute this node: compute, deposit into ctx.secondary, notify observers.

        Args:
            ctx: Shared run context; result is deposited into ctx.secondary.

        Returns:
            List of ControlEvents emitted by this node (may be empty).

        Raises:
            Exception: Anything raised by _compute propagates so the engine can
                isolate the failure; the satisfied-dependency set is cleared
                first via the finally block so a failed node leaves no stale
                flags for the next cycle.
        """
        try:
            result, events = await self._compute(ctx)
        finally:
            # Clear even when _compute raises: a leftover satisfied flag would
            # make all_secondary_deps_satisfied() lie next cycle, marking the
            # node ready before its (now isolated) upstream has re-deposited.
            self._satisfied.clear()
        if result is not None and self.output_type is not None:
            ctx.secondary[self.output_type] = result
        self._notify_observers()
        return events

    @abstractmethod
    async def _compute(
        self, ctx: NodeContext
    ) -> tuple[Any | None, list[ControlEvent]]:
        """Implement node-specific computation logic.

        Args:
            ctx: Shared run context with all upstream outputs available.

        Returns:
            Tuple of (result, events); result is deposited as output_type in
            ctx.secondary (ignored when output_type is None).
        """


class DagEngine:
    """Tiered observer DAG executor.

    Construction:
      DagEngine(nodes) — nodes are auto-wired via _wire() and grouped by tier.

    Execution:
      await engine.run(primary, config, now) → (secondary, events)

    Tiers execute in ascending order; within a tier all ready nodes run in parallel.
    """

    def __init__(self, nodes: list[DagNode]) -> None:
        """Initialise engine, group nodes by tier, and auto-wire dependencies.

        Args:
            nodes: All DAG nodes; tiers and output_types must be set correctly.
        """
        self._nodes_by_tier: dict[int, list[DagNode]] = {}
        for node in nodes:
            self._nodes_by_tier.setdefault(node.tier, []).append(node)
        self._wire(nodes)

    def _wire(self, nodes: list[DagNode]) -> None:
        """Auto-wire observer relationships by matching output_type to consumed types.

        For each consumed secondary type, registers the consuming node as an
        observer of the node that produces it.

        Args:
            nodes: All nodes to wire; each node's consumes list is inspected.

        Raises:
            TierViolationError: When a consumer has a tier <= its producer's tier.
        """
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
        """Execute all tiers in ascending order; within each tier run ready nodes in parallel.

        Args:
            primary: Translation-layer outputs (HA state → domain types).
            config: Structured user configuration, passed through to all nodes.
            now: Cycle timestamp.

        Returns:
            Tuple of (secondary_outputs_dict, all_control_events). A node that
            raises is isolated: its output is omitted and downstream consumers
            that depend on it simply never become ready, degrading the same way
            they would for a missing primary input.
        """
        ctx = NodeContext(primary=primary, secondary={}, config=config, now=now)
        all_events: list[ControlEvent] = []

        for tier_num in sorted(self._nodes_by_tier):
            ready = [
                n for n in self._nodes_by_tier[tier_num]
                if n.all_secondary_deps_satisfied(ctx)
            ]
            if not ready:
                continue
            # return_exceptions so one flaky node can't abort the whole tier
            # (and with it the cycle). A failed node deposits nothing and
            # notifies no observers, so its consumers stay unsatisfied and skip.
            results = await asyncio.gather(
                *[n.run(ctx) for n in ready], return_exceptions=True
            )
            for node, result in zip(ready, results):
                if isinstance(result, BaseException):
                    if isinstance(result, asyncio.CancelledError):
                        raise result
                    _LOGGER.warning(
                        "DAG node %s (tier %d) failed; output omitted, "
                        "downstream consumers degrade this cycle: %s",
                        type(node).__name__, node.tier, result,
                    )
                    continue
                all_events.extend(result)

        return ctx.secondary, all_events


async def run_translators(
    translators: list,
    hass: Any,
    config: SunSaleConfig,
    raw_config: dict,
    now: datetime,
) -> dict[type, Any]:
    """Run all translators concurrently and collect their outputs into a primary dict.

    Translators that return None (optional / unavailable sources) are silently
    skipped. A translator that *raises* is isolated: the failure is logged and
    its output is omitted from the primary dict, so one flaky source can't abort
    the whole cycle. The DAG degrades gracefully for the missing input, and the
    control-module dispatch downstream still runs.

    Args:
        translators: List of translator objects exposing output_type and translate().
        hass: Home Assistant instance passed through to each translator.
        config: Structured SunSale configuration.
        raw_config: Raw config-entry dict forwarded to translators that need it.
        now: Cycle timestamp.

    Returns:
        Dict mapping each surviving translator's output_type to its result.

    Raises:
        asyncio.CancelledError: Re-raised if a translator task was cancelled
            (genuine cancellation must not be swallowed as a per-source failure).
    """
    results = await asyncio.gather(
        *[t.translate(hass, config, raw_config, now) for t in translators],
        return_exceptions=True,
    )
    primary: dict[type, Any] = {}
    for t, result in zip(translators, results):
        if isinstance(result, BaseException):
            if isinstance(result, asyncio.CancelledError):
                raise result
            _LOGGER.warning(
                "Translator %s failed; omitting %s from primary inputs this "
                "cycle: %s",
                type(t).__name__,
                getattr(t.output_type, "__name__", t.output_type),
                result,
            )
            continue
        if result is not None:
            primary[t.output_type] = result
    return primary
