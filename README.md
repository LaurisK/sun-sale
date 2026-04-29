# sunSale

A Home Assistant custom integration that automates electricity **buying, selling, and EV charging** decisions for households with solar panels and battery storage. It optimises the battery and EV charger purely around Nordpool spot prices, your tariff formula, solar generation, battery state, and battery degradation cost.

> **Status: alpha (v0.1.2).** Structurally complete and unit-tested. Huawei Solar and Solis have dedicated inverter branches; SolarEdge and GoodWe fall through to a generic `number.set_value` call and will likely need adjustment. Use the **Automation** master switch to run in observation mode first.

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/)

---

## What it does

- Reads Nordpool prices from the [Nordpool HA integration](https://github.com/custom-components/nordpool).
- Reads the solar generation forecast from any integration that exposes a `forecast` attribute (Forecast.Solar, Solcast, …).
- Reads battery SoC / power and grid power from your inverter integration.
- Computes the **effective** buy/sell price per hour (spot + distribution + tax + markup).
- Runs a greedy pair-matching optimiser to pick the most profitable charge/discharge hours, subject to:
  - SoC limits and round-trip efficiency,
  - Battery degradation cost (derived from purchase price, rated cycle life, and learned capacity).
- Drives the inverter via HA service calls to charge from grid, discharge to grid, or idle.
- Optionally schedules EV charging into the cheapest hours before a configured departure time.
- Learns the battery's actual usable capacity over time from observed SoC deltas and energy throughput.

## What it does NOT do

- It does not model household consumption — the assumption is that consumption is negligible relative to trading volumes.
- It does not call any external API directly. Everything flows through other HA integrations.
- It does not implement vendor-specific Modbus / inverter logic. The inverter abstraction maps generic commands onto whatever entity you point it at.

---

## Requirements

| Dependency | Why |
| --- | --- |
| Home Assistant 2024.x or newer (Python 3.12+) | Core runtime |
| [Nordpool integration](https://github.com/custom-components/nordpool) | Hourly spot prices (`today` / `tomorrow` attributes) |
| An inverter / battery integration | SoC, battery power, grid power, and a charge-control entity |
| Solar forecast integration *(optional)* | Forecast.Solar, Solcast, etc. |
| EV charger integration *(optional)* | OpenEVSE, Easee, Wallbox, or any switch-controllable charger |

The integration declares no Python `requirements` of its own — it consumes data via the HA state machine.

### Supported inverter platforms

Selectable in the config flow:

- `huawei_solar` — dedicated branch: writes signed watt values to the charge-control `number` entity (positive = charge, negative = discharge).
- `solis_modbus` — dedicated branch: uses the [Pho3niX90/solis_modbus](https://github.com/Pho3niX90/solis_modbus) integration's native Time-of-Use (TOU) scheduling model. Rather than a single setpoint, sunSale writes charge/discharge amps and slot-1 start/end times each cycle, and toggles the TOU/self-use mode switches. The config flow shows pre-filled defaults for the canonical solis_modbus entity IDs; you only need to change them if your instance uses a custom prefix. Configure the **Nominal DC bus voltage** on the Battery step (48 V for most low-voltage residential packs; ~400 V for Pylontech HV and similar high-voltage systems) — this is required for correct kW→A conversion.
- `solaredge`, `goodwe`, `generic` — share the **generic** code path: write signed kW to the configured `number` entity. **Verify this matches what your inverter integration accepts** before enabling automation.

### Supported EV charger platforms

- `openevse` — sets charge current via a `number` entity, then `switch.turn_on`.
- `easee` — calls the `easee.start_charging` / `easee.stop_charging` services.
- `wallbox` — calls the `wallbox.start_charging` / `wallbox.stop_charging` services.
- `generic` — plain `switch.turn_on` / `switch.turn_off`.

---

## Installation

### Option A — HACS (recommended)

sunSale ships with `hacs.json` and is installable as a HACS **custom repository** (it is not in the default HACS index yet).

1. Make sure [HACS](https://hacs.xyz/) is installed.
2. In HACS, open **Integrations → ⋮ (top right) → Custom repositories**.
3. Add the repository URL: `https://github.com/LaurisK/sun-sale`
4. Set **Category** to **Integration**, then click **Add**.
5. Find **sunSale** in the HACS integrations list and click **Download**.
6. **Restart Home Assistant.**
7. Go to **Settings → Devices & Services → Add Integration → sunSale**.

### Option B — Manual install

1. Copy the `custom_components/sun_sale/` directory from this repository into your Home Assistant `config/custom_components/` directory:

   ```
   config/
   └── custom_components/
       └── sun_sale/
           ├── __init__.py
           ├── manifest.json
           └── …
   ```

2. Restart Home Assistant.

3. Go to **Settings → Devices & Services → Add Integration → sunSale**.

### Updating

- **HACS:** updates appear in HACS when a new tag is published in the repository. Click **Update**, then restart Home Assistant.
- **Manual:** re-copy the `custom_components/sun_sale/` directory and restart.

---

## Configuration

The config flow has five steps. Have your entity IDs ready before you start.

### 1. Tariff

| Field | Meaning | Example |
| --- | --- | --- |
| Distribution fee (buy) | EUR/kWh added to spot when buying | `0.05` |
| Tax/VAT rate (buy) | Fractional, applied to (spot + fee + markup) | `0.21` |
| Retailer markup (buy) | EUR/kWh added by retailer | `0.005` |
| Distribution fee (sell) | EUR/kWh deducted when selling | `0.01` |
| Tax rate (sell) | Fractional, deducted from sell revenue | `0.0` |
| Retailer deduction (sell) | EUR/kWh retailer keeps when selling | `0.0` |

Effective prices used by the optimiser:

```
buy_price  = (spot + distribution_fee + markup) * (1 + tax_rate)
sell_price = (spot - sell_distribution_fee - sell_markup) * (1 - sell_tax_rate)
```

### 2. Battery

| Field | Meaning |
| --- | --- |
| Nominal capacity (kWh) | Manufacturer-rated capacity. Refined automatically over time. |
| Purchase price (EUR) | Used for degradation cost. |
| Rated cycle life | Number of full cycles before end-of-life (default `6000`). |
| Max charge / discharge power (kW) | Hard cap per hour. |
| Min / max SoC | Operating range, e.g. `0.10` and `0.95`. |
| Round-trip efficiency | e.g. `0.90` for 90 %. |

Degradation cost per stored kWh = `purchase_price / (rated_cycle_life × estimated_capacity × 2)`.
A trade is only chosen when `sell × efficiency − buy − 2 × degradation > 0`.

### 3. Inverter

Pick the platform. The next step depends on the selection:

**For all non-Solis platforms**, supply four entity IDs:

- **Battery SoC** — sensor in % or 0–1.
- **Battery power** — kW, positive = charging.
- **Grid power** — kW, positive = importing.
- **Charge control** — the `number`/`switch` entity sunSale will write to.

**For `solis_modbus`**, the step shows the full TOU entity set with defaults pre-filled. You typically only need to change these if your solis_modbus instance uses a non-default entity prefix. Required entities:

| Entity | Default ID |
| --- | --- |
| Battery SoC | `sensor.solis_battery_soc` |
| Battery power | `sensor.solis_battery_power` |
| Grid power | `sensor.solis_ac_grid_port_power` |
| Charge current (slot 1) | `number.solis_time_charging_charge_current` |
| Discharge current (slot 1) | `number.solis_time_charging_discharge_current` |
| Charge start time (slot 1) | `time.solis_time_charging_charge_start_slot_1` |
| Charge end time (slot 1) | `time.solis_time_charging_charge_end_slot_1` |
| Discharge start time (slot 1) | `time.solis_time_charging_discharge_start_slot_1` |
| Discharge end time (slot 1) | `time.solis_time_charging_discharge_end_slot_1` |
| TOU mode switch | `switch.solis_time_of_use_mode` |
| Allow-grid-charge switch | `switch.solis_allow_grid_to_charge_the_battery` |
| Self-use mode switch | `switch.solis_self_use_mode` |

### 4. EV charger *(optional)*

Toggle off if you don't have one. Otherwise pick a platform and provide:

- Plug-state binary sensor.
- EV SoC sensor *(optional)*, target SoC sensor *(optional)*, departure-time sensor *(optional)*.
- Charger switch / charger ID.
- Battery capacity (kWh), max / min charge power (kW).

### 5. Data sources

- **Nordpool entity** — typically `sensor.nordpool_kwh_<area>_eur_3_10_0` or similar. The integration prefers `raw_today` / `raw_tomorrow` (15-min slots with timestamps) and falls back to legacy hourly `today` / `tomorrow` arrays when only those are present.
- **Solar forecast entity** *(optional)* — must expose a `forecast` attribute as a list of `{time, pv_estimate | energy}` entries.

Tariff parameters can be edited later via **Configure** on the integration card.

---

## Provided entities

Created under a single `sunSale` device:

| Entity | Type | Description |
| --- | --- | --- |
| `sensor.sunsale_current_action` | sensor | `idle` / `charge_from_grid` / `discharge_to_grid` / `charge_from_solar` |
| `sensor.sunsale_next_action` | sensor | The next action that differs from the current one |
| `sensor.sunsale_next_action_time` | timestamp | When that next action starts |
| `sensor.sunsale_expected_profit_today` | EUR | Sum of expected profit for today's slots |
| `sensor.sunsale_degradation_cost` | EUR/kWh | Per-kWh battery wear cost |
| `sensor.sunsale_estimated_battery_capacity` | kWh | Learned usable capacity |
| `sensor.sunsale_current_buy_price` | EUR/kWh | Effective buy price right now |
| `sensor.sunsale_current_sell_price` | EUR/kWh | Effective sell price right now |
| `sensor.sunsale_ev_charging` | `on` / `off` | Whether EV should charge in the current hour |
| `sensor.sunsale_ev_charge_cost` | EUR | Total cost of the planned EV session |
| `sensor.sunsale_schedule` | sensor | Current action; full hourly plan in `extra_state_attributes.schedule` |
| `sensor.sunsale_inverter_mode` | sensor | Current inverter mode label (recorded in HA history for the dashboard's past-mode band) |
| `sensor.sunsale_dashboard` | sensor | Pre-built 15-min slots and frozen solar forecast consumed by the side-panel chart |
| `switch.sunsale_automation` | switch | Master kill-switch — when off, schedules are still computed but no commands are sent |

### Service

- `sun_sale.force_recalculate` — re-runs the optimiser immediately for all entries.

The coordinator otherwise refreshes every **5 minutes**.

---

## Recommended rollout

1. **Install and configure** with the **Automation switch off**. Watch `sensor.sunsale_schedule` and the action sensors for a day or two.
2. **Verify** that the `Charge control` entity you mapped does what you expect when you manually set its value.
3. **Turn the Automation switch on** once you trust the schedule and the inverter mapping.
4. Adjust tariff / battery parameters via **Configure** as your understanding of real-world cost improves.

---

## Development

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements_dev.txt

pytest tests/                         # full suite, optimiser + tariff + battery + EV scheduler + entity smoke
pytest tests/test_optimizer.py        # one file
ruff check custom_components/sun_sale/
mypy custom_components/sun_sale/
```

The optimisation core (`optimizer.py`, `tariff.py`, `battery.py`, `ev_scheduler.py`, `models.py`) is pure Python with no Home Assistant imports, so it runs fully under plain `pytest` without an HA test harness.

### Layout

```
.
├── hacs.json            # HACS custom-repository metadata
├── README.md
└── custom_components/sun_sale/
├── __init__.py          # entry setup, force_recalculate service
├── manifest.json
├── const.py
├── config_flow.py       # 5-step UI flow + options flow
├── coordinator.py       # 5-minute refresh, reads HA state, runs optimiser, executes commands
├── models.py            # dataclasses (no HA deps)
├── tariff.py            # spot → effective buy/sell price
├── battery.py           # degradation cost + capacity learner
├── optimizer.py         # greedy pair-matching schedule
├── ev_scheduler.py      # cheapest-hours EV plan
├── inverter.py          # platform abstraction
├── ev_charger.py        # platform abstraction
├── sensor.py            # 13 sensor entities
├── switch.py            # automation kill-switch
├── debug_view.py        # /api/sun_sale/debug JSON snapshot view
├── dashboard.py         # 15-min future slot builder for the side-panel chart
├── www/sun-sale-panel.js # custom HA sidebar panel
├── services.yaml        # force_recalculate
├── strings.json
└── translations/en.json
```

---

## Known limitations

- `huawei_solar` and `solis_modbus` have dedicated inverter command paths; `solaredge`, `goodwe`, and `generic` share a `number.set_value` writer that may not match every integration.
- Household consumption is assumed negligible — there is no load forecast.
- Greedy pair-matching is not globally optimal; it gives a good schedule fast but can occasionally be improved by an LP/ILP approach.
- Capacity learning only runs when SoC delta between two 5-minute snapshots exceeds 5 %, so it converges over days, not minutes.
- No GUI for editing entity mappings after setup — remove and re-add the integration to change them.

## License

Not yet specified. Treat the code as "all rights reserved" until a license file is added.
