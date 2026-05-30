"""Control events emitted by DAG nodes — consumed by output adapters (Layer 3b).

No Home Assistant imports. Events are pure data.

Note: The original ``InverterActionEvent`` was removed when the inverter
control surface migrated from per-cycle TOU rewrites to a register-level
state machine (see ``docs/solis_control.md``). ``ControlEvent`` is retained
as the empty base type of the DAG event channel — every DagNode's
``_compute`` signature returns ``list[ControlEvent]``. Future event types
(e.g. external VPP triggers) can subclass it without further plumbing.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ControlEvent:
    """Base class for hardware control events emitted by DAG nodes."""
