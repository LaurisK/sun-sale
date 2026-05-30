"""DAG nodes — the computation tier of the sunSale pipeline.

Pure Python — no Home Assistant imports.
Each node declares its tier, output_type, and consumed types.
Observer wiring is auto-built by DagEngine._wire() based on these declarations.

Tier map:
  T1: PricingNode, BatteryStateNode, BatteryStatusNode, BaseLoadProfileNode
  T2: GenerationNode, ObservedGenerationNode, DegradationNode, BatteryRuntimeNode,
      MonthlyBillNode, ProfitabilityNode
  T3: ChargingProfileNode, ForecastAccuracyNode, LockoutNode
  T4: ScheduleNode
"""
from .tier1 import (
    BaseLoadProfileNode,
    BatteryStateNode,
    BatteryStatusNode,
    PricingNode,
)
from .tier2 import (
    BatteryRuntimeNode,
    DegradationNode,
    GenerationNode,
    MonthlyBillNode,
    ObservedGenerationNode,
    ProfitabilityNode,
)
from .tier3 import (
    ChargingProfileNode,
    ForecastAccuracyNode,
    LockoutNode,
)
from .tier4 import (
    ScheduleNode,
    make_last_ref,
)

__all__ = [
    "BaseLoadProfileNode",
    "BatteryRuntimeNode",
    "BatteryStateNode",
    "BatteryStatusNode",
    "ChargingProfileNode",
    "DegradationNode",
    "ForecastAccuracyNode",
    "GenerationNode",
    "LockoutNode",
    "MonthlyBillNode",
    "ObservedGenerationNode",
    "PricingNode",
    "ProfitabilityNode",
    "ScheduleNode",
    "make_last_ref",
]
