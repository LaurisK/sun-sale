# Inbound — Pricing module

Reference for `custom_components/sun_sale/inbound/pricing.py`.

The pricing module owns the **72h yesterday→today→tomorrow `PriceSeries`** that every downstream consumer (calculator, optimizer, dashboard, sensors) reads. It is pure Python with no Home Assistant imports.

## Summary

`NordpoolTranslator` reads `raw_today`/`raw_tomorrow` (or legacy flat arrays) with auto-detected resolution. `build_price_series_72h(nordpool, yesterday, config, now)` combines `YesterdayPrices.entries + NordpoolData.entries`, applies the tariff formula, and yields the 72h `PriceSeries` `PricingNode` returns.

- **Exposes:** `NordpoolData` (translator), `PriceSeries` (helper).
- **Depends on:** `contract.models`, `pipeline.tariff`.
- **Tests:** `tests/test_pricing.py` (helper) + `tests/test_coordinator.py::test_nordpool_*` (translator). Full per-test coverage table in §8.

## Contents

1. [Responsibilities](#1-responsibilities)
2. [Public API](#2-public-api)
3. [Inputs](#3-inputs)
4. [Output: `PriceSeries`](#4-output-priceseries)
5. [Tariff formula](#5-tariff-formula)
6. [Resolution handling](#6-resolution-handling)
7. [Integration in the DAG](#7-integration-in-the-dag)
8. [Tests](#8-tests)

---

## 1. Responsibilities

- Combine **yesterday entries** (from persistent store, supplied by orchestration) with **today + tomorrow entries** (from the Nordpool translator) into a single contiguous slot list.
- Apply the buy/sell tariff formula to every slot.
- Produce a `PriceSeries` keyed by the slot resolution detected by the Nordpool translator (15-min or 1h).
- Provide a thin per-list helper (`build_price_series`) used by tests and any caller that already has a pre-stitched entry list.

Yesterday-stitching used to live in the coordinator as an in-place mutation of `NordpoolData.entries`. It is now an explicit, testable function call in this module; the coordinator only supplies the persisted entries as a primary input.

---

## 2. Public API

```python
def build_price_series(
    prices: list[PriceEntry],
    config: TariffConfig,
    now: datetime | None = None,
    resolution: timedelta | None = None,
) -> PriceSeries
```

Apply tariff formulas to a flat list of `PriceEntry`. If `resolution` is omitted it is derived from the first two slots (defaults to 1h for single-slot input). `now` defaults to `datetime.now(timezone.utc)` and is recorded as `PriceSeries.computed_at`.

```python
def build_price_series_72h(
    nordpool: NordpoolData,
    yesterday: YesterdayPrices,
    config: TariffConfig,
    now: datetime | None = None,
) -> PriceSeries
```

Assemble the full yesterday→today→tomorrow series. Concatenates `yesterday.entries + nordpool.entries`, then delegates to `build_price_series` with `resolution=nordpool.resolution`. This is the function the DAG's `PricingNode` calls.

---

## 3. Inputs

**`NordpoolData`** (`contract/models.py`) — produced by `inbound.translators.NordpoolTranslator`. Covers today + tomorrow only. `resolution` is auto-detected from the source attribute timestamps (`raw_today[1].start - raw_today[0].start`); the integration's HA Nordpool sensor exposes a single resolution stream per cycle and tomorrow is zero-filled until the day-ahead market publishes.

**`YesterdayPrices`** (`contract/models.py`) — a frozen wrapper around `tuple[PriceEntry, ...]`. Supplied by `orchestration.coordinator` from the `STORAGE_KEY_YESTERDAY` `Store`. The coordinator gates the value: if the stored date is not exactly yesterday, an empty tuple is passed (no stale stitching).

**`TariffConfig`** — user-configured fees, taxes, and markups, built once at coordinator setup from the config entry.

---

## 4. Output: `PriceSeries`

```python
@dataclass(frozen=True)
class PriceSeries:
    slots: tuple[PriceSlot, ...]
    resolution: timedelta
    computed_at: datetime

    def slot_at(self, t: datetime) -> PriceSlot | None
    def window(self, t1: datetime, t2: datetime) -> tuple[PriceSlot, ...]
```

Each `PriceSlot` carries:

| Field | Meaning |
|---|---|
| `start`, `end` | UTC slot boundaries |
| `buy_eur_kwh` | Effective grid-import price (always ≥ 0 in practice) |
| `sell_eur_kwh` | Effective grid-export revenue — **can be negative** |
| `spot_eur_kwh` | Raw Nordpool price, retained for provenance |
| `sources` | `("nordpool", "tariff")` for diagnostics |

The pricing module emits raw buy/sell price data only. Whether a slot is sellable (the "sell allowed" decision) is not a pricing concern — it belongs to the charging-profile stage, which decides what to do with generated energy based on `sell_eur_kwh`.

---

## 5. Tariff formula

Delegated to `pipeline/tariff.py`:

```
buy  = (spot + distribution_fee + markup) * (1 + tax_rate)
sell = (spot - sell_distribution_fee - sell_markup) * (1 - sell_tax_rate)
```

`sell_eur_kwh` can be negative (high sell fees against low spot prices) or exactly zero. Downstream consumers — primarily the charging profile — apply the strict `> 0` sellability check; the pricing module itself does not flag this.

---

## 6. Resolution handling

- `NordpoolTranslator` is the single source of truth: resolution is detected from sensor data and recorded on `NordpoolData.resolution`.
- `build_price_series_72h` forwards that value to `build_price_series` via the `resolution` argument — **no rederivation** from slot deltas.
- `build_price_series` only falls back to slot-delta derivation when called without an explicit `resolution` (test helpers, direct callers).

This matters when yesterday has fewer slots than today, or when the input is sparse: trusting `NordpoolData.resolution` avoids contradictions between the two layers.

---

## 7. Integration in the DAG

`PricingNode` (`pipeline/nodes.py`):

```python
tier = 1
output_type = PriceSeries
consumes = [NordpoolData, YesterdayPrices]
```

Wired automatically by `DagEngine._wire()`. Both inputs are primary (no upstream node produces them), so `PricingNode` runs in T1 in parallel with `BatteryStateNode`.

Coordinator flow (`orchestration/coordinator.py:_async_update_data`):

1. Run translators → `primary[NordpoolData]`.
2. Build `YesterdayPrices` from the persistent store (empty tuple if the stored date is not yesterday) → `primary[YesterdayPrices]`.
3. `DagEngine.run(primary, ...)` → `PricingNode` calls `build_price_series_72h(...)` → `secondary[PriceSeries]`.
4. After the cycle, today's entries (filtered out of `NordpoolData.entries` by date) are written to the store as the next cycle's yesterday.

The coordinator no longer mutates `NordpoolData.entries`; its only role is to load/save the persistent yesterday slice.

---

## 8. Tests

All pricing tests live in `tests/test_pricing.py` and run under pure-Python pytest (no HA harness needed). Shared fixtures come from `tests/conftest.py` — `BASE_DT`, `make_price()`, and `default_tariff_config()`.

Run them with:

```bash
./venv/bin/python -m pytest tests/test_pricing.py -v
```

### Coverage

| Group | Test | What it checks |
|---|---|---|
| Round-trip basics | `test_empty_prices_returns_empty_series` | Empty input → empty `slots` tuple |
| | `test_slot_count_matches_input` | 24 entries → 24 slots |
| | `test_computed_at_is_set` | `computed_at == now` |
| | `test_sources_tuple` | Provenance fixed to `("nordpool", "tariff")` |
| Tariff math | `test_buy_price_formula` | Buy = `(spot + distribution + markup) * (1 + tax)` |
| | `test_sell_price_formula` | Sell = `(spot - dist - markup) * (1 - tax)` |
| | `test_spot_price_preserved` | Raw spot retained on slot |
| Sell sign | `test_negative_spot_produces_negative_sell` | Negative spot → `sell_eur_kwh < 0` |
| | `test_positive_spot_produces_positive_sell` | Positive spot → `sell_eur_kwh > 0` |
| | `test_sell_price_can_be_exactly_zero` | Fees that exactly cancel spot → `sell_eur_kwh == 0.0` |
| Resolution | `test_hourly_resolution_detected` | Hourly slot stride → `timedelta(hours=1)` |
| | `test_single_slot_defaults_to_hourly_resolution` | Single slot → defaults to 1h when no `resolution` arg |
| Helpers | `test_slot_at_returns_correct_slot` | `slot_at(t)` picks the slot containing `t` |
| | `test_slot_at_returns_none_outside_range` | Out-of-range time → `None` |
| | `test_window_returns_overlapping_slots` | `window(t1, t2)` returns slots overlapping the half-open interval |
| **72h assembly** | `test_72h_combines_yesterday_today_tomorrow` | 24 + 24 + 24 entries → 72 slots, ordered, boundaries correct |
| | `test_72h_uses_nordpool_resolution_not_derived` | Sparse input + 15-min `NordpoolData.resolution` → series resolution = 15-min (not rederived to 1h) |
| | `test_72h_empty_yesterday_returns_only_today_tomorrow` | Empty yesterday tuple → 48 slots starting at today |
| | `test_72h_applies_tariff_to_all_segments` | Tariff applied uniformly across yesterday and today slots |

### What is intentionally **not** covered here

- **End-to-end DAG wiring** (PricingNode within the engine) — exercised in `tests/test_coordinator.py` and indirectly through `tests/test_optimizer.py` / `tests/test_calculator.py`, which import `build_price_series` to construct fixtures.
- **HA-side Nordpool parsing** (15-min vs legacy attributes, zero-fill of tomorrow) — covered in `NordpoolTranslator` tests, not in the pricing module's tests.
- **Persistent yesterday store I/O** — coordinator's responsibility; covered in `tests/test_coordinator.py`.
