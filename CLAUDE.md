# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**sunSale** is a Home Assistant custom integration that automates electricity buying/selling and EV charging decisions for households with solar panels and battery storage. The core assumption is that household consumption is negligible — the system optimizes purely around Nordpool spot prices, solar generation forecasts, battery state, tariff costs, and EV charging demand.

### What it does

- Monitors Nordpool electricity prices (via existing HA integration)
- Estimates solar generation (via existing HA integration, e.g., Forecast.Solar)
- Reads battery state (capacity, SoC, health) from inverter or BMS
- Controls the inverter to charge/discharge the battery
- Decides when to buy (charge from grid) and sell (discharge to grid) based on price optimization
- Controls EV charger to minimize charging cost by scheduling charging during cheapest price windows
- Accounts for real costs: grid tariffs, distribution fees, taxes, and battery degradation

### Key domain concepts

- **Tariff formula**: The actual price of electricity includes Nordpool spot price + distribution fees + taxes + markup. This formula is configurable per electricity provider/country.
- **Battery degradation cost**: Each charge/discharge cycle has a cost derived from battery purchase price, rated cycle life, and current capacity. A trade is only profitable if the price spread exceeds degradation cost + tariff overhead.
- **Capacity estimation**: The system starts with a configured nominal battery capacity and refines its estimate of actual usable capacity over time based on observed charge/discharge cycles.
- **Optimization window**: Decisions are made over a rolling window of known Nordpool prices (typically 12-36 hours ahead).
- **EV charging optimization**: When the EV is plugged in, the system schedules charging into the cheapest available hours while respecting a target SoC and departure time. It can use grid, solar, or battery — whichever is cheapest at the time.

## Architecture

This is a standard Home Assistant custom integration, installed via HACS or manually into `custom_components/sun_sale/`.

```
custom_components/sun_sale/
├── __init__.py          # Integration setup, coordinator
├── manifest.json        # HA integration metadata, dependencies
├── config_flow.py       # UI-based configuration
├── const.py             # Constants, defaults
├── coordinator.py       # DataUpdateCoordinator — central data refresh
├── sensor.py            # Sensor entities (recommended action, profit, capacity estimate, etc.)
├── switch.py            # Enable/disable automation
├── services.yaml        # Custom service definitions
├── strings.json         # UI strings
├── translations/        # Localization
├── optimizer.py         # Core price optimization / scheduling logic
├── tariff.py            # Tariff formula calculation
├── battery.py           # Degradation model, capacity tracker
├── inverter.py          # Abstraction layer for inverter control
└── ev_charger.py        # EV charger abstraction and charge scheduling
```

### Data flow

1. **Coordinator** periodically fetches: Nordpool prices, solar forecast, battery SoC/health, inverter state, EV charger state
2. **Optimizer** takes all inputs and produces a charge/discharge schedule for the price window, including EV charging slots
3. **Inverter abstraction** executes the battery schedule by calling HA services on the actual inverter integration
4. **EV charger abstraction** executes EV charging schedule by controlling the charger via HA services
5. **Sensors** expose: current action, next action, estimated profit, battery health, effective capacity, EV charge plan

### External HA integrations used (read via HA state machine)

- **Nordpool** (`sensor.nordpool_*`): hourly electricity prices
- **Solar forecast** (e.g., Forecast.Solar, Solcast): predicted generation
- **Inverter** (e.g., SolarEdge, Huawei Solar, GoodWe): battery SoC, grid import/export, charge/discharge control
- **EV charger** (e.g., OpenEVSE, Easee, Wallbox): plug state, current SoC, start/stop/set current control

## Development

### Setup

```bash
# Python 3.12+ required (match your HA version)
python -m venv venv
source venv/bin/activate
pip install -r requirements_dev.txt
```

### Run tests

```bash
pytest tests/
pytest tests/test_optimizer.py             # single file
pytest tests/test_optimizer.py::test_name  # single test
```

### Lint and type check

```bash
ruff check custom_components/sun_sale/
ruff format custom_components/sun_sale/
mypy custom_components/sun_sale/
```

### Test in Home Assistant

Copy or symlink `custom_components/sun_sale/` into your HA `config/custom_components/` directory and restart HA. For development, use a HA dev container.

## Design Decisions

- **No direct API calls to external services** — all external data comes through existing HA integrations. This keeps sunSale focused on optimization logic and avoids duplicating authentication/polling.
- **Inverter abstraction** — the integration controls inverters via HA service calls, not vendor APIs directly. A thin abstraction layer maps generic commands (charge, discharge, idle) to integration-specific service calls.
- **Pure optimization core** — `optimizer.py` is a pure function: prices in, schedule out. No HA dependencies, easy to unit test.
- **Battery model learns over time** — initial capacity is user-configured; the system refines this estimate by observing actual energy throughput vs SoC changes, stored persistently.
