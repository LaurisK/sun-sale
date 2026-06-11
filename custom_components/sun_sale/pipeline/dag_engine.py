"""DAG Task Engine — tiered observer-pattern directed acyclic graph executor.

Architecture:
  - Each DagNode declares its tier, output_type, and consumed types.
  - DagEngine._wire() validates the tier constraint: a node may only consume
    outputs produced by strictly-lower-tier nodes (this prevents cycles and
    deadlocks). Same- or higher-tier dependencies are rejected.
  - On each run cycle the engine executes tiers in ascending order; within a tier
    all eligible nodes run in parallel (asyncio.gather). Each tier runs to
    completion before the next begins, so every lower-tier output is present in
    NodeContext.secondary by the time a node's tier is evaluated.
  - Node readiness is therefore derived directly from ctx.secondary. There is no
    cross-node notification or per-node "satisfied" state, so nothing can leak
    between cycles. When a node completes it deposits its result in
    NodeContext.secondary.

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
    """Raised when a node consumes the output of a same- or higher-tier node."""


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

        Primary takes precedence whenever it holds the key, even if the stored
        value is falsy (None, empty collection, a NamedTuple). Membership — not
        truthiness — decides the fallthrough, so a deliberately-stored primary
        value is never shadowed by a stale secondary one.

        Args:
            t: The type key to look up.

        Returns:
            The stored value, or None.
        """
        if t in self.primary:
            return self.primary[t]
        return self.secondary.get(t)

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

    def all_secondary_deps_satisfied(self, ctx: NodeContext) -> bool:
        """Return True when every secondary dependency declared in consumes is available.

        Readiness is derived purely from ctx.secondary: because a node may only
        consume strictly-lower-tier outputs and each tier runs to completion
        before the next begins, every secondary dependency it declares is
        already present in ctx.secondary by the time its own tier is evaluated.
        There is no per-node "satisfied" state, so nothing can leak across cycles.

        Args:
            ctx: Current DAG run context.

        Returns:
            True if every non-primary consumed type is present in ctx.secondary.
        """
        return all(
            t in ctx.secondary
            for t in self.consumes
            if t not in ctx.primary
        )

    # ── Execution ────────────────────────────────────────────────────────────

    async def run(self, ctx: NodeContext) -> list[ControlEvent]:
        """Execute this node: compute and deposit the result into ctx.secondary.

        Args:
            ctx: Shared run context; result is deposited into ctx.secondary.

        Returns:
            List of ControlEvents emitted by this node (may be empty).

        Raises:
            Exception: Anything raised by _compute propagates so the engine can
                isolate the failure. The node simply deposits nothing, so its
                downstream consumers stay unready (absent from ctx.secondary)
                and degrade gracefully — there is no cross-cycle state to reset.
        """
        result, events = await self._compute(ctx)
        if result is not None and self.output_type is not None:
            ctx.secondary[self.output_type] = result
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
        """Validate the tier constraint for every producer→consumer dependency.

        For each consumed secondary type, finds the node that produces it and
        enforces that the consumer sits at a strictly higher tier. This is the
        invariant that lets readiness be read straight from ctx.secondary
        (see DagNode.all_secondary_deps_satisfied) and prevents cycles/deadlocks.

        Args:
            nodes: All nodes to validate; each node's consumes list is inspected.

        Raises:
            TierViolationError: When a consumer's tier <= the tier of a node it consumes.
        """
        producer: dict[type, DagNode] = {
            n.output_type: n for n in nodes if n.output_type is not None
        }
        for node in nodes:
            for dep_type in node.consumes:
                p = producer.get(dep_type)
                if p is not None and node.tier <= p.tier:
                    raise TierViolationError(
                        f"{type(node).__name__} (T{node.tier}) cannot consume "
                        f"{type(p).__name__} (T{p.tier}): consumer tier must be "
                        f"strictly higher"
                    )

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
