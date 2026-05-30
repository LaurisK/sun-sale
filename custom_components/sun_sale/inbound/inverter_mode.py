"""InverterModeTranslator — reads the inverter's current StorageMode each cycle.

Pulls register 43110 readback + battery currents + RC active-power setpoint
from the ``InverterController`` and decodes them into an
``InverterModeReading`` (a primary type injected into ``NodeContext`` by the
coordinator). The decoded ``StorageMode`` feeds two consumers:

  - the persistent ``InverterModeHistory`` rolling log (managed by
    ``outbound/inverter_control_module.py``);
  - the diagnostic ``inverter_mode_now`` sensor attribute (current target vs
    observed).

The translator is resilient to missing or unparseable entities — any field
the controller cannot read becomes ``None`` and the decoded mode collapses
to ``StorageMode.UNKNOWN``.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from ..contract.models import InverterModeReading, SunSaleConfig
from ..outbound.inverter import InverterController
from ..pipeline.storage_mode_specs import decode_mode


class InverterModeTranslator:
    """Reads the inverter's StorageMode signals and produces an InverterModeReading."""

    output_type = InverterModeReading

    def __init__(self, inverter: InverterController) -> None:
        """Initialise with the platform-aware controller.

        Args:
            inverter: Controller providing the read-side helpers
                (``get_storage_control_word`` etc).
        """
        self._inverter = inverter

    def parse(self, hass: Any, now: datetime) -> InverterModeReading:
        """Synchronously read register 43110 + ancillary values and decode them.

        ``hass`` is accepted for parity with other translators but is not used
        directly here — all reads go through the cached ``InverterController``.

        Args:
            hass: Home Assistant instance (unused here).
            now: Cycle timestamp, used as ``InverterModeReading.timestamp``.

        Returns:
            An ``InverterModeReading`` capturing the current observed state.
        """
        del hass  # reads are routed through the controller
        reg = self._inverter.get_storage_control_word()
        charge_a = self._inverter.get_charge_current_a()
        discharge_a = self._inverter.get_discharge_current_a()
        rc_w = self._inverter.get_rc_setpoint_w()
        mode = decode_mode(reg, charge_a, discharge_a, rc_w)
        return InverterModeReading(
            timestamp=now,
            reg_43110_value=reg,
            mode=mode,
            charge_a=charge_a,
            discharge_a=discharge_a,
            rc_setpoint_w=rc_w,
        )

    async def translate(
        self, hass: Any, config: SunSaleConfig, raw_config: dict, now: datetime
    ) -> InverterModeReading:
        """Async wrapper around ``parse`` so this slots into ``run_translators``.

        Args:
            hass: Home Assistant instance.
            config: Structured SunSale config (unused here).
            raw_config: Raw config-entry dict (unused here).
            now: Cycle timestamp.

        Returns:
            ``InverterModeReading`` for this cycle.
        """
        del config, raw_config
        return self.parse(hass, now)
