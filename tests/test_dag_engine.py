"""Tests for the DAG engine's per-cycle fault isolation.

These cover the resilience contract that keeps one flaky translator or DAG
node from aborting the whole coordinator cycle — which would otherwise flip
every sensor unavailable and, worse, skip the inverter-control dispatch tick
at a schedule-slot boundary.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import pytest

from custom_components.sun_sale.contract.models import SunSaleConfig
from custom_components.sun_sale.pipeline.dag_engine import (
    DagEngine,
    DagNode,
    NodeContext,
    run_translators,
)

from tests.conftest import default_battery_config, default_tariff_config

NOW = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)


def _config() -> SunSaleConfig:
    """Build a minimal SunSaleConfig for engine runs."""
    return SunSaleConfig(
        tariff=default_tariff_config(), battery=default_battery_config()
    )


# --- Output marker types used to wire the test DAG -------------------------

class _AOut:
    """Healthy tier-0 producer output."""


class _BOut:
    """Failing tier-0 producer output (never deposited)."""


class _COut:
    """Tier-1 consumer output; depends on _BOut."""


class _ProducerA(DagNode):
    """Tier-0 node that always succeeds."""

    tier = 0
    output_type = _AOut
    consumes: list = []

    async def _compute(self, ctx):
        """Return a fresh _AOut and no events."""
        return _AOut(), []


class _FailingProducer(DagNode):
    """Tier-0 node whose compute always raises."""

    tier = 0
    output_type = _BOut
    consumes: list = []

    async def _compute(self, ctx):
        """Raise to exercise per-node isolation."""
        raise RuntimeError("boom")


class _ConsumerOfB(DagNode):
    """Tier-1 node that consumes the failing node's output."""

    tier = 1
    output_type = _COut
    consumes = [_BOut]

    def __init__(self) -> None:
        """Track whether _compute ever ran."""
        super().__init__()
        self.ran = False

    async def _compute(self, ctx):
        """Require _BOut (absent when upstream failed) and produce _COut."""
        self.ran = True
        ctx.require(_BOut)
        return _COut(), []


# --- DagEngine.run node isolation ------------------------------------------

@pytest.mark.asyncio
async def test_failing_node_isolated_sibling_survives(caplog):
    """A raising node is omitted; its healthy tier-sibling still deposits."""
    a, b, c = _ProducerA(), _FailingProducer(), _ConsumerOfB()
    engine = DagEngine([a, b, c])

    with caplog.at_level(logging.WARNING):
        secondary, events = await engine.run({}, _config(), NOW)

    assert _AOut in secondary          # healthy sibling survived
    assert _BOut not in secondary      # failed node produced nothing
    assert _COut not in secondary      # downstream degraded gracefully
    assert c.ran is False              # consumer never became ready
    assert events == []
    assert "_FailingProducer" in caplog.text


@pytest.mark.asyncio
async def test_failing_node_does_not_leave_stale_satisfied_flag():
    """After a node fails, a second run with the same failure still skips C.

    Readiness is read straight from ctx.secondary, so a node skipped (or failed)
    in one cycle carries no leftover state into the next — the consumer is
    re-evaluated from scratch and must not run on a phantom dependency.
    """
    a, b, c = _ProducerA(), _FailingProducer(), _ConsumerOfB()
    engine = DagEngine([a, b, c])

    await engine.run({}, _config(), NOW)
    assert c.ran is False              # B never deposited, so C never ready

    # Second cycle: B still fails, so C must again be skipped (not run on a
    # leftover flag) and must not raise MissingDependencyError.
    secondary, _ = await engine.run({}, _config(), NOW)
    assert _COut not in secondary
    assert c.ran is False


# --- Cross-cycle readiness leak (the bug this guards against) --------------

class _ToggleProducer(DagNode):
    """Tier-0 producer that deposits its output only while `active` is True.

    When inactive it raises, so the engine isolates it and deposits nothing —
    mirroring a source that is simply absent that cycle.
    """

    tier = 0
    consumes: list = []

    def __init__(self, output_type: type, active: bool) -> None:
        """Bind this producer to an output type and an initial active flag."""
        super().__init__()
        self.output_type = output_type
        self.active = active

    async def _compute(self, ctx):
        """Deposit a fresh output when active; otherwise raise to deposit nothing."""
        if not self.active:
            raise RuntimeError("inactive this cycle")
        return self.output_type(), []


class _TwoDepConsumer(DagNode):
    """Tier-1 node that requires both _AOut and _BOut to be present."""

    tier = 1
    output_type = _COut
    consumes = [_AOut, _BOut]

    def __init__(self) -> None:
        """Count how many times _compute actually runs."""
        super().__init__()
        self.run_count = 0

    async def _compute(self, ctx):
        """Require both deps (raising if either is absent) and produce _COut."""
        self.run_count += 1
        ctx.require(_AOut)
        ctx.require(_BOut)
        return _COut(), []


@pytest.mark.asyncio
async def test_partial_deps_do_not_leak_into_next_cycle():
    """A consumer half-satisfied one cycle must not falsely run the next.

    Cycle 1 deposits _AOut but not _BOut, so the consumer is skipped. Cycle 2
    flips it — _BOut is deposited but _AOut is now absent. The consumer must
    stay unready: under the old per-node satisfied-set it would retain the
    _AOut flag from cycle 1, falsely become ready, run, and blow up in
    ctx.require.
    """
    a = _ToggleProducer(_AOut, active=True)
    b = _ToggleProducer(_BOut, active=False)
    c = _TwoDepConsumer()
    engine = DagEngine([a, b, c])

    sec1, _ = await engine.run({}, _config(), NOW)
    assert _AOut in sec1
    assert _BOut not in sec1
    assert _COut not in sec1
    assert c.run_count == 0            # only one dep present → skipped

    # Flip which producer is live: _BOut now arrives, _AOut no longer does.
    a.active = False
    b.active = True
    sec2, _ = await engine.run({}, _config(), NOW)
    assert _BOut in sec2
    assert _AOut not in sec2
    assert _COut not in sec2           # still missing a dep → must not produce
    assert c.run_count == 0            # the leak would have run it here


# --- NodeContext.get precedence (membership, not truthiness) ---------------

def _ctx(primary: dict, secondary: dict) -> NodeContext:
    """Build a NodeContext over the given primary/secondary maps."""
    return NodeContext(
        primary=primary, secondary=secondary, config=_config(), now=NOW
    )


def test_get_returns_falsy_primary_without_falling_through():
    """A falsy value in primary wins over a secondary value of the same type.

    The lookup must key off membership, not truthiness: an empty-collection or
    NamedTuple primary (both falsy) must not be shadowed by a stale secondary
    deposit. Guards the old ``primary.get(t) or secondary.get(t)`` tripwire.
    """
    empty = ()                          # falsy, but deliberately stored
    ctx = _ctx({tuple: empty}, {tuple: ("stale",)})
    assert ctx.get(tuple) is empty


def test_get_returns_none_when_primary_holds_none_explicitly():
    """A primary key bound to None returns None instead of consulting secondary."""
    ctx = _ctx({tuple: None}, {tuple: ("stale",)})
    assert ctx.get(tuple) is None


def test_get_falls_through_to_secondary_when_primary_absent():
    """Absent-from-primary still reads secondary — the normal node-output path."""
    out = _AOut()
    ctx = _ctx({}, {_AOut: out})
    assert ctx.get(_AOut) is out


# --- run_translators isolation ---------------------------------------------

class _GoodTranslator:
    """Translator that yields a value for output_type _AOut."""

    output_type = _AOut

    async def translate(self, hass, config, raw_config, now):
        """Return a fresh _AOut instance."""
        return _AOut()


class _NoneTranslator:
    """Translator that has no data this cycle (returns None)."""

    output_type = _COut

    async def translate(self, hass, config, raw_config, now):
        """Return None to signal an unavailable source."""
        return None


class _RaisingTranslator:
    """Translator whose translate always raises."""

    output_type = _BOut

    async def translate(self, hass, config, raw_config, now):
        """Raise to exercise per-translator isolation."""
        raise RuntimeError("nordpool exploded")


class _CancelledTranslator:
    """Translator whose translate is cancelled."""

    output_type = _COut

    async def translate(self, hass, config, raw_config, now):
        """Raise CancelledError to verify it is propagated, not swallowed."""
        raise asyncio.CancelledError()


@pytest.mark.asyncio
async def test_run_translators_omits_raising_translator(caplog):
    """A raising translator is logged and omitted; healthy ones survive."""
    translators = [_GoodTranslator(), _RaisingTranslator(), _NoneTranslator()]

    with caplog.at_level(logging.WARNING):
        primary = await run_translators(translators, None, _config(), {}, NOW)

    assert _AOut in primary            # healthy translator survived
    assert _BOut not in primary        # raising translator omitted
    assert _COut not in primary        # None result skipped as before
    assert "_RaisingTranslator" in caplog.text


@pytest.mark.asyncio
async def test_run_translators_propagates_cancellation():
    """A genuine task cancellation must not be swallowed as a source failure."""
    translators = [_GoodTranslator(), _CancelledTranslator()]

    with pytest.raises(asyncio.CancelledError):
        await run_translators(translators, None, _config(), {}, NOW)
