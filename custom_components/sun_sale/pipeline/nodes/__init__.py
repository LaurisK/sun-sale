"""DAG nodes — the computation tier of the sunSale pipeline.

Pure Python — no Home Assistant imports.
Each node declares its tier, output_type, and consumed types.
Observer wiring is auto-built by DagEngine._wire() based on these declarations.

Tier map:
  T1: PricingNode, BatteryStateNode, BatteryStatusNode, BaseLoadProfileNode
  T2: GenerationNode, ObservedGenerationNode, ObservedGridNode, DegradationNode,
      BatteryRuntimeNode, ProfitabilityNode
  T3: ForecastAccuracyNode, LockoutNode, MonthlyBillNode
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
    ObservedConsumptionNode,
    ObservedGenerationNode,
    ObservedGridNode,
    ObservedLossesNode,
    ProfitabilityNode,
)
from .tier3 import (
    ForecastAccuracyNode,
    LockoutNode,
    MonthlyBillNode,
)
from .tier4 import ScheduleNode

__all__ = [
    "BaseLoadProfileNode",
    "BatteryRuntimeNode",
    "BatteryStateNode",
    "BatteryStatusNode",
    "DegradationNode",
    "ForecastAccuracyNode",
    "GenerationNode",
    "LockoutNode",
    "MonthlyBillNode",
    "ObservedConsumptionNode",
    "ObservedGenerationNode",
    "ObservedGridNode",
    "ObservedLossesNode",
    "PricingNode",
    "ProfitabilityNode",
    "ScheduleNode",
]
