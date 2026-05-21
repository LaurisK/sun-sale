"""Control events emitted by DAG nodes — consumed by output adapters (Layer 3b).

No Home Assistant imports. Events are pure data.
"""
from __future__ import annotations

from dataclasses import dataclass

from .models import Action


@dataclass(frozen=True)
class ControlEvent:
    """Base class for all hardware control events."""


@dataclass(frozen=True)
class InverterActionEvent(ControlEvent):
    """Emitted by ScheduleNode when the current-slot action changes."""
    action: Action
    power_kw: float
