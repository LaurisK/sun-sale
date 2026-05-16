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
    """Emitted by OptimizerNode when the current-slot action changes."""
    action: Action
    power_kw: float


@dataclass(frozen=True)
class EVActionEvent(ControlEvent):
    """Emitted by EVSchedulerNode when the current EV charge action changes."""
    charge_power_kw: float  # 0.0 = stop charging
