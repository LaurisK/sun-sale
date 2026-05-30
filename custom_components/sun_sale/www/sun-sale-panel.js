/* Sun Sale Dashboard Panel
 *
 * Daily generation row (above main chart):
 *   Compact text pills — 8 days: yesterday → +6 days.
 *   Yesterday/Today show predicted / actual (today also shows remaining-forecast).
 *   Future days show predicted only. Data: forecast_daily_kwh,
 *   actual_yesterday_kwh, actual_today_kwh, charging_profile_summary.
 *
 * 72-hour window: yesterday 00:00 → tomorrow 23:59 (local)
 *
 * Left Y axis — EUR/kWh:
 *   Buy price   amber  solid stepline  — past history + future from pricing sensor (one continuous line)
 *   Sell price  coral  solid stepline  — past history + future from pricing sensor (one continuous line)
 *
 * Right Y axis — kWh/slot (15-min):
 *   Solar forecast — vertical bar per 15-min slot (scalar y = forecast_kwh).
 *                    Past + non-today-future slots: grey 25 % opacity.
 *                    Today's remaining slots: per-bar colour from
 *                    charging_profile_slots:
 *                      blue   SOLAR_CHARGE  — going to battery
 *                      amber  SELL          — exporting for €
 *                      red    NO_EXPORT     — curtailed (sell ≤ 0)
 *
 *   Forecast-vs-observed error overlay — green/red rect per slot, anchored
 *   at the top of the forecast bar (y = forecast_kwh):
 *     +error (observed > forecast)  → green segment grows UP
 *     -error (observed < forecast)  → red segment grows DOWN
 *   ApexCharts 3.x has no native expression for this in a mixed line+bar
 *   chart (stacked/rangeBar/range-y all fail), so we paint the rects as raw
 *   SVG into `.apexcharts-graphical` from chart events.
 *
 * Overlays:
 *   Translucent bands  ChargingProfile mode windows (today's remaining slots):
 *                        blue   SOLAR_CHARGE
 *                        amber  SELL
 *                        red    NO_EXPORT
 *                      Bands carry no inline text — the colour-coded pills
 *                      in the profile row above the chart act as the legend.
 *   Dashed white line "Now"
 *   Dashed grey line  y = 0 price reference
 */

(function () {
  'use strict';

  // ── Entity IDs ─────────────────────────────────────────────────────────────
  // Update these to match your Home Assistant instance.
  const BUY_PRICE_ENTITY  = 'sensor.sunsale_current_buy_price';
  const SELL_PRICE_ENTITY = 'sensor.sunsale_current_sell_price';
  const PRICING_ENTITY    = 'sensor.sunsale_pricing';
  const DASHBOARD_ENTITY      = 'sensor.sunsale_dashboard';
  const MONTHLY_BILL_ENTITY   = 'sensor.sunsale_monthly_bill';
  const APEXCHARTS_CDN        = 'https://cdn.jsdelivr.net/npm/apexcharts@3.54.0/dist/apexcharts.min.js';

  // ── Helpers ────────────────────────────────────────────────────────────────

  function localMidnight(offsetDays) {
    const d = new Date();
    d.setHours(0, 0, 0, 0);
    d.setDate(d.getDate() + offsetDays);
    return d.getTime();
  }

  function loadApexCharts() {
    if (window.ApexCharts) return Promise.resolve();
    return new Promise((resolve, reject) => {
      const s = document.createElement('script');
      s.src = APEXCHARTS_CDN;
      s.onload = resolve;
      s.onerror = () => reject(new Error('Could not load ApexCharts from CDN'));
      document.head.appendChild(s);
    });
  }

  // ── Panel element ──────────────────────────────────────────────────────────

  class SunSalePanel extends HTMLElement {
    constructor() {
      super();
      this._hass        = null;
      this._chart       = null;
      this._g1Chart     = null;
      this._g2Chart     = null;
      this._g3Chart     = null;
      this._initialized = false;
      this.attachShadow({ mode: 'open' });
    }

    set hass(hass) {
      this._hass = hass;
      if (!this._initialized) {
        this._initialized = true;
        this._boot();
      } else {
        const dashAttrs = hass.states[DASHBOARD_ENTITY]?.attributes;
        this._renderBatteryRow(dashAttrs);
        this._renderProfileRow(dashAttrs);
        this._renderGenerationRow(dashAttrs);
      }
    }

    disconnectedCallback() {
      if (this._chart)     { this._chart.destroy();     this._chart     = null; }
      if (this._g1Chart)   { this._g1Chart.destroy();    this._g1Chart   = null; }
      if (this._g2Chart)   { this._g2Chart.destroy();    this._g2Chart   = null; }
      if (this._g3Chart)   { this._g3Chart.destroy();    this._g3Chart   = null; }
    }

    // ── Boot ──────────────────────────────────────────────────────────────────

    async _boot() {
      this._buildShell();
      try {
        await loadApexCharts();
      } catch (e) {
        this._setStatus('⚠ ' + e.message + ' — check network or install ApexCharts via HACS.');
        return;
      }
      await this._render();
    }

    _buildShell() {
      this.shadowRoot.innerHTML = `
        <style>
          :host {
            display: block;
            padding: 16px;
            box-sizing: border-box;
            background: var(--primary-background-color, #111);
            min-height: 100vh;
          }
          #card {
            background: var(--card-background-color, #1c1c1c);
            border-radius: 12px;
            padding: 20px;
          }
          h2 {
            margin: 0 0 2px;
            font-size: 1.25rem;
            color: var(--primary-text-color, #fff);
          }
          #subtitle {
            margin: 0 0 16px;
            font-size: 0.75rem;
            color: var(--secondary-text-color, #888);
          }
          #battery {
            display: flex;
            align-items: baseline;
            gap: 8px;
            margin: 0 0 12px;
            font-size: 0.95rem;
            color: var(--primary-text-color, #fff);
          }
          #battery .label { color: var(--secondary-text-color, #888); }
          #battery .state { font-weight: 600; }
          #battery .state.charging    { color: #4caf50; }
          #battery .state.discharging { color: #ff7043; }
          #battery .state.idle        { color: #9e9e9e; }
          #battery .soc { font-weight: 600; }
          #battery .capacity {
            font-size: 0.75rem;
            color: var(--secondary-text-color, #888);
          }
          #profile {
            display: flex;
            flex-wrap: wrap;
            align-items: center;
            gap: 8px;
            margin: 0 0 12px;
            font-size: 0.85rem;
            color: var(--primary-text-color, #fff);
          }
          #profile .label { color: var(--secondary-text-color, #888); }
          #profile .value { font-weight: 600; }
          #profile .sep { color: var(--secondary-text-color, #444); }
          #profile .pill {
            display: inline-flex;
            align-items: baseline;
            gap: 6px;
            padding: 3px 8px;
            border-radius: 4px;
            border-left: 3px solid;
            line-height: 1.3;
          }
          #profile .pill .value { font-weight: 600; }
          #profile .pill.charge  { background: rgba(66, 165, 245, 0.16); border-left-color: #42a5f5; }
          #profile .pill.sell    { background: rgba(255, 179, 0, 0.16);  border-left-color: #ffb300; }
          #profile .pill.curtail { background: rgba(229, 57, 53, 0.16);  border-left-color: #e53935; }
          #generation {
            display: flex;
            flex-wrap: wrap;
            align-items: center;
            gap: 6px 10px;
            margin: 0 0 12px;
            font-size: 0.85rem;
            color: var(--primary-text-color, #fff);
          }
          #generation .day {
            display: inline-flex;
            align-items: baseline;
            gap: 4px;
            padding: 3px 8px;
            border-radius: 4px;
            background: rgba(255, 255, 255, 0.04);
            border-left: 3px solid rgba(66, 165, 245, 0.40);
            line-height: 1.3;
          }
          #generation .day.today { border-left-color: #42a5f5; background: rgba(66, 165, 245, 0.10); }
          #generation .day.past  { border-left-color: rgba(158, 158, 158, 0.60); }
          #generation .day .name { color: var(--secondary-text-color, #888); font-weight: 600; }
          #generation .day .pred { font-weight: 600; }
          #generation .day .actual { color: #66bb6a; font-weight: 600; }
          #generation .day .remain { color: #ffb300; font-size: 0.75rem; }
          #generation .day .sep  { color: var(--secondary-text-color, #444); }
          #generation .day .unit { color: var(--secondary-text-color, #888); font-size: 0.75rem; }
          #status {
            padding: 40px;
            text-align: center;
            color: var(--secondary-text-color, #888);
          }
          #chart { width: 100%; }
          #accuracy { margin-top: 24px; }
          .accuracy-title {
            font-size: 0.85rem;
            font-weight: 600;
            color: var(--secondary-text-color, #888);
            text-transform: uppercase;
            letter-spacing: 0.05em;
            margin: 16px 0 4px;
          }
          .accuracy-subtitle {
            font-size: 0.75rem;
            color: var(--secondary-text-color, #666);
            margin: 0 0 4px;
          }
          .accuracy-chart { width: 100%; }
          .bill-title {
            font-size: 0.85rem;
            font-weight: 600;
            color: var(--secondary-text-color, #888);
            text-transform: uppercase;
            letter-spacing: 0.05em;
            margin: 16px 0 4px;
          }
          .bill-summary {
            display: flex;
            flex-wrap: wrap;
            align-items: baseline;
            gap: 6px 8px;
            margin: 0 0 8px;
            font-size: 0.85rem;
            color: var(--primary-text-color, #fff);
          }
          .bill-summary .bill-label { color: var(--secondary-text-color, #888); }
          .bill-summary .bill-value { font-weight: 600; }
          .bill-summary .bill-value.revenue { color: #66bb6a; }
          .bill-summary .bill-sep { color: var(--secondary-text-color, #444); }
        </style>
        <div id="card">
          <h2>☀ Sun Sale</h2>
          <div id="subtitle">Buy &amp; Sell prices · Solar — 72 h window</div>
          <div id="battery"></div>
          <div id="profile"></div>
          <div id="generation"></div>
          <div id="status">Loading…</div>
          <div id="chart"></div>
          <div id="bill"></div>
          <div id="accuracy"></div>
        </div>
      `;
    }

    _renderBatteryRow(dashAttrs) {
      const el = this.shadowRoot.querySelector('#battery');
      if (!el) return;
      if (!dashAttrs) { el.innerHTML = ''; return; }

      const state    = dashAttrs.battery_state || 'idle';
      const soc      = dashAttrs.battery_soc_pct;
      const charged  = dashAttrs.battery_remaining_kwh;
      const capacity = dashAttrs.battery_capacity_kwh;

      const stateLabel = state.charAt(0).toUpperCase() + state.slice(1);
      const socTxt = (typeof soc === 'number') ? soc.toFixed(1) + '%' : '—';
      const capTxt = (typeof charged === 'number' && typeof capacity === 'number')
        ? `${charged.toFixed(2)} / ${capacity.toFixed(2)} kWh`
        : '';

      el.innerHTML = `
        <span class="label">Battery:</span>
        <span class="state ${state}">${stateLabel}</span>
        <span class="soc">${socTxt}</span>
        ${capTxt ? `<span class="capacity">${capTxt}</span>` : ''}
      `;
    }

    _renderProfileRow(dashAttrs) {
      const el = this.shadowRoot.querySelector('#profile');
      if (!el) return;
      const sum = dashAttrs?.charging_profile_summary;
      if (!sum) { el.innerHTML = ''; return; }

      const fmt = (v) => (typeof v === 'number' ? v.toFixed(2) : '—');
      const remaining = fmt(sum.today_remaining_generation_kwh);
      const free      = fmt(sum.free_capacity_kwh);
      const charge    = fmt(sum.allocated_solar_kwh);
      const sell      = fmt(sum.total_sell_kwh);
      const curtail   = fmt(sum.total_no_export_kwh);

      el.innerHTML = `
        <span class="label">Today remaining:</span>
        <span class="value">${remaining} kWh</span>
        <span class="sep">·</span>
        <span class="label">Free capacity:</span>
        <span class="value">${free} kWh</span>
        <span class="sep">→</span>
        <span class="pill charge">Battery <span class="value">${charge}</span> kWh</span>
        <span class="pill sell">Sell <span class="value">${sell}</span> kWh</span>
        <span class="pill curtail">Curtail <span class="value">${curtail}</span> kWh</span>
      `;
    }

    _renderGenerationRow(dashAttrs) {
      const el = this.shadowRoot.querySelector('#generation');
      if (!el) return;
      if (!dashAttrs) { el.innerHTML = ''; return; }

      const daily = dashAttrs.forecast_daily_kwh;
      if (!daily) { el.innerHTML = ''; return; }

      const fmt = (v) => (typeof v === 'number' ? v.toFixed(2) : '—');
      const remainingToday = dashAttrs.charging_profile_summary?.today_remaining_generation_kwh;

      const DAY_NAMES = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
      const OFFSETS   = [-1, 0, 1, 2, 3, 4, 5, 6];
      const KEYS      = ['yesterday', 'today', 'tomorrow', 'd2', 'd3', 'd4', 'd5', 'd6'];

      const dayLabel = (off) => {
        if (off === -1) return 'Yday';
        if (off === 0)  return 'Today';
        if (off === 1)  return 'Tmrw';
        const d = new Date();
        d.setDate(d.getDate() + off);
        return DAY_NAMES[d.getDay()];
      };

      const parts = OFFSETS.map((off, i) => {
        const pred = daily[KEYS[i]];
        const predTxt = fmt(pred);
        const cls = off < 0 ? 'past' : (off === 0 ? 'today' : '');

        let inner = `<span class="name">${dayLabel(off)}</span>`;
        if (off === -1) {
          const actual = dashAttrs.actual_yesterday_kwh;
          inner += `<span class="pred">${predTxt}</span>`
                +  `<span class="sep">/</span>`
                +  `<span class="actual">${fmt(actual)}</span>`
                +  `<span class="unit">kWh</span>`;
        } else if (off === 0) {
          const actual = dashAttrs.actual_today_kwh;
          let predCell = `<span class="pred">${predTxt}</span>`;
          if (typeof remainingToday === 'number') {
            predCell += `<span class="remain">(${fmt(remainingToday)} rem)</span>`;
          }
          inner += predCell
                +  `<span class="sep">/</span>`
                +  `<span class="actual">${fmt(actual)}</span>`
                +  `<span class="unit">kWh</span>`;
        } else {
          inner += `<span class="pred">${predTxt}</span>`
                +  `<span class="unit">kWh</span>`;
        }
        return `<span class="day ${cls}">${inner}</span>`;
      });

      el.innerHTML = parts.join('');
    }

    _setStatus(msg) {
      const el = this.shadowRoot.querySelector('#status');
      if (el) el.textContent = msg;
    }

    _clearStatus() {
      const el = this.shadowRoot.querySelector('#status');
      if (el) el.textContent = '';
    }

    // ── Data: history API — past buy/sell prices + solar actual ───────────────

    async _fetchHistory(windowStartMs) {
      const start    = new Date(windowStartMs).toISOString();
      const end      = new Date().toISOString();
      const entities = [BUY_PRICE_ENTITY, SELL_PRICE_ENTITY].join(',');
      const url      = `/api/history/period/${start}?end_time=${end}`
        + `&filter_entity_id=${entities}&minimal_response=true&no_attributes=true`;
      try {
        const resp = await this._hass.fetchWithAuth(url);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const raw = await resp.json();

        const indexed = {};
        for (const entityHistory of raw) {
          if (!entityHistory?.length) continue;
          const eid = entityHistory[0].entity_id;
          indexed[eid] = entityHistory
            .filter(s => s.state && !['unavailable', 'unknown', 'None'].includes(s.state))
            .map(s => [new Date(s.last_changed).getTime(), parseFloat(s.state)])
            .filter(([, v]) => isFinite(v));
        }
        return {
          buyPrice:  indexed[BUY_PRICE_ENTITY]  || [],
          sellPrice: indexed[SELL_PRICE_ENTITY] || [],
        };
      } catch (e) {
        console.warn('sunSale: history fetch failed', e);
        return { buyPrice: [], sellPrice: [] };
      }
    }

    // ── Data: buy/sell prices from pricing pipeline sensor (all slots) ────────

    _readPricingSlots(beforeMs) {
      const attrs = this._hass.states[PRICING_ENTITY]?.attributes;
      if (!Array.isArray(attrs?.slots)) return { buy: [], sell: [] };

      const buy  = [];
      const sell = [];
      for (const slot of attrs.slots) {
        const t = new Date(slot.start).getTime();
        if (!isFinite(t) || t > beforeMs) continue;
        if (typeof slot.buy_eur_kwh  === 'number') buy.push([t, slot.buy_eur_kwh]);
        if (typeof slot.sell_eur_kwh === 'number') sell.push([t, slot.sell_eur_kwh]);
      }
      return {
        buy:  buy.sort((a, b)  => a[0] - b[0]),
        sell: sell.sort((a, b) => a[0] - b[0]),
      };
    }

    // ── Data: 15-min forecast kWh slots from dashboard sensor ─────────────────

    _buildForecastSlots(dashAttrs, windowStart, windowEnd) {
      const slots = new Map();
      if (!dashAttrs) return slots;
      for (const f of (dashAttrs.forecast_slots || [])) {
        if (f.t >= windowStart && f.t <= windowEnd)
          slots.set(f.t, f.forecast_kwh ?? f.forecast_w / 1000 * 0.25);
      }
      return slots;
    }

    // ── Data: per-slot charging-profile disposition from dashboard sensor ─────
    // Returns Map<slotStartMs, { mode, expected_kwh, sell_eur_kwh }>.
    // Only today's remaining slots are present (backend convention).

    _readChargingProfile(dashAttrs) {
      const slots = new Map();
      if (!Array.isArray(dashAttrs?.charging_profile_slots)) return slots;
      for (const s of dashAttrs.charging_profile_slots) {
        if (typeof s?.t !== 'number' || typeof s.mode !== 'string') continue;
        slots.set(s.t, {
          mode:          s.mode,
          expected_kwh:  Number(s.expected_kwh)  || 0,
          sell_eur_kwh:  Number(s.sell_eur_kwh)  || 0,
        });
      }
      return slots;
    }

    // ── Data: per-slot forecast accuracy from dashboard sensor ────────────────
    // Returns [{x, forecastKwh, errorKwh}] for slots inside the window with a
    // non-zero error. error_kwh = observed_kwh - forecast_kwh.

    _readForecastErrors(dashAttrs, windowStart, windowEnd) {
      if (!Array.isArray(dashAttrs?.forecast_error_slots)) return [];
      const out = [];
      for (const s of dashAttrs.forecast_error_slots) {
        if (typeof s?.t !== 'number') continue;
        if (s.t < windowStart || s.t > windowEnd) continue;
        const err = Number(s.error_kwh);
        const obs = Number(s.observed_kwh);
        // -1 sentinel from the backend = observation not yet recovered for
        // this slot. Surface as a "pending" marker; the overlay drawer
        // renders these as a thin grey strip instead of a green/red rect.
        if (err === -1 || obs === -1) {
          out.push({
            x:            s.t,
            forecastKwh:  Number(s.forecast_kwh) || 0,
            errorKwh:     0,
            pending:      true,
          });
          continue;
        }
        if (!isFinite(err) || Math.abs(err) < 1e-4) continue;
        out.push({
          x:            s.t,
          forecastKwh:  Number(s.forecast_kwh) || 0,
          errorKwh:     err,
          pending:      false,
        });
      }
      return out;
    }

    // ── Render ─────────────────────────────────────────────────────────────────

    async _render() {
      const now         = Date.now();
      const windowStart = localMidnight(-1);         // yesterday 00:00 local
      const windowEnd   = localMidnight(2) - 60_000; // tomorrow 23:59 local
      const SLOT_MS     = 15 * 60 * 1000;

      const history       = await this._fetchHistory(windowStart);
      const pricingSlots  = this._readPricingSlots(windowEnd);
      const dashAttrs     = this._hass.states[DASHBOARD_ENTITY]?.attributes;
      const forecastSlots = this._buildForecastSlots(dashAttrs, windowStart, windowEnd);
      const profileSlots  = this._readChargingProfile(dashAttrs);
      const errorSlots    = this._readForecastErrors(dashAttrs, windowStart, windowEnd);

      this._renderBatteryRow(dashAttrs);
      this._renderProfileRow(dashAttrs);
      this._renderGenerationRow(dashAttrs);

      // Merge past history + future pricing sensor into continuous price lines.
      const _merge = (histPoints, pricingPoints, windowStartMs) => {
        const histFiltered = histPoints.filter(([t]) => t >= windowStartMs);
        const histDeduped  = histFiltered.filter(([t]) =>
          !pricingPoints.some(([pt]) => Math.abs(pt - t) < 30 * 60 * 1000)
        );
        return [...histDeduped, ...pricingPoints].sort((a, b) => a[0] - b[0]);
      };

      const buyData  = _merge(history.buyPrice,  pricingSlots.buy,  windowStart);
      const sellData = _merge(history.sellPrice, pricingSlots.sell, windowStart);

      if (!buyData.length && !sellData.length) {
        this._setStatus('No price data — waiting for sunSale coordinator to run.');
        return;
      }

      // Single forecast bar per slot. Per-point colour by charging-profile mode
      // for today's remaining slots; grey 25 % opacity everywhere else.
      const GREY_BAR = 'rgba(158,158,158,0.25)';
      const MODE_COLORS = {
        solar_charge: '#42a5f5',
        sell:         '#ffb300',
        no_export:    '#e53935',
      };

      const forecastBars = [];
      let t = Math.floor(windowStart / SLOT_MS) * SLOT_MS;
      while (t <= windowEnd) {
        const fKwh = forecastSlots.get(t);
        if (fKwh != null && fKwh > 0.001) {
          const prof  = profileSlots.get(t);
          const color = (prof && MODE_COLORS[prof.mode]) || GREY_BAR;
          forecastBars.push({ x: t, y: fKwh, fillColor: color, strokeColor: color });
        }
        t += SLOT_MS;
      }

      this._clearStatus();
      if (this._chart) { this._chart.destroy(); this._chart = null; }

      // Translucent bands grouping contiguous ChargingProfile-mode slots.
      // Legend lives in the pill row above the chart — no inline label here.
      const MODE_BAND_FILL = {
        solar_charge: 'rgba(66, 165, 245, 0.12)',
        sell:         'rgba(255, 179, 0, 0.10)',
        no_export:    'rgba(229, 57, 53, 0.14)',
      };
      const profileBands = [];
      {
        const sorted = [...profileSlots.entries()]
          .filter(([, p]) => MODE_BAND_FILL[p.mode])
          .sort((a, b) => a[0] - b[0]);
        let run = null;
        for (const [slotT, prof] of sorted) {
          if (run && prof.mode === run.mode && slotT === run.x2) {
            run.x2 = slotT + SLOT_MS;
            continue;
          }
          if (run) profileBands.push(run);
          run = { mode: prof.mode, x: slotT, x2: slotT + SLOT_MS };
        }
        if (run) profileBands.push(run);
      }
      const profileAnnotations = profileBands.map(b => ({
        x:           b.x,
        x2:          b.x2,
        fillColor:   MODE_BAND_FILL[b.mode],
        borderColor: 'transparent',
        opacity:     1,
      }));

      // ─── Inverter StorageMode bands (history yesterday→now + plan today→tomorrow).
      // History entries from inverter_mode_history are {t, mode} change events;
      // turn them into bands [event.t, next_event.t || now]. Plan slots from
      // inverter_mode_plan are {t, end_t, mode} and become bands directly.
      const STORAGE_MODE_FILL = {
        sell:    'rgba(255, 152, 0, 0.18)',
        store:   'rgba(33, 150, 243, 0.16)',
        hoard:   'rgba(3, 169, 244, 0.20)',
        dump:    'rgba(244, 67, 54, 0.20)',
        gulp:    'rgba(76, 175, 80, 0.20)',
        stby:    'rgba(120, 144, 156, 0.16)',
        auto:    'rgba(189, 189, 189, 0.10)',
        track:   'rgba(156, 39, 176, 0.18)',
        unknown: 'rgba(96, 96, 96, 0.18)',
      };
      const modeBands = [];
      {
        const nowMs = Number(dashAttrs?.now_ts) || Date.now();
        const histRaw = Array.isArray(dashAttrs?.inverter_mode_history)
          ? [...dashAttrs.inverter_mode_history].sort((a, b) => a.t - b.t)
          : [];
        for (let i = 0; i < histRaw.length; i++) {
          const ev = histRaw[i];
          if (!STORAGE_MODE_FILL[ev.mode]) continue;
          const nextStart = i + 1 < histRaw.length ? histRaw[i + 1].t : nowMs;
          if (nextStart <= ev.t) continue;
          modeBands.push({ mode: ev.mode, x: ev.t, x2: nextStart });
        }
        const planRaw = Array.isArray(dashAttrs?.inverter_mode_plan)
          ? dashAttrs.inverter_mode_plan
          : [];
        for (const s of planRaw) {
          if (!STORAGE_MODE_FILL[s.mode]) continue;
          if (!(s.end_t > s.t)) continue;
          modeBands.push({ mode: s.mode, x: s.t, x2: s.end_t });
        }
      }
      const modeAnnotations = modeBands.map(b => ({
        x:           b.x,
        x2:          b.x2,
        fillColor:   STORAGE_MODE_FILL[b.mode],
        borderColor: 'transparent',
        opacity:     1,
        // y-range omitted — ApexCharts defaults to spanning the full plot area,
        // which is exactly what we want for piecewise-constant mode bands.
      }));

      // Series: 0=solar forecast(bar) 1=buy(line) 2=sell(line) 3=net billing(line).
      // Bar is first so price lines render on top of generation bars.
      // All series use {x, y} object format. Mixing tuple-format line data
      // with object-format bar data on a datetime xaxis causes ApexCharts
      // to create the bar <g> group but emit zero <rect>s (bars invisible).
      const toXY = pts => pts.map(([x, y]) => ({ x, y }));
      const billingData = this._buildBillingSeries(windowStart, now);
      const series = [
        { name: 'Solar forecast', type: 'bar',  data: forecastBars   },
        { name: 'Buy price',      type: 'line', data: toXY(buyData)  },
        { name: 'Sell price',     type: 'line', data: toXY(sellData) },
        { name: 'Net billing',    type: 'line', data: billingData     },
      ];

      // Forecast-accuracy overlay drawn straight into the SVG plot area.
      // For each slot with a non-zero error: a rect anchored at the top of
      // the forecast bar (y = forecast_kwh), extending UP by +error (green
      // for under-forecast) or DOWN by |error| (red for over-forecast).
      // ApexCharts can't express this via stacked / rangeBar / range-y in
      // a mixed line+bar chart, so we paint custom <rect>s once after
      // render() resolves. Wiring this into chart.events.mounted/updated
      // collapses the bar series (cause unknown — likely re-entrant render).
      const SVG_NS    = 'http://www.w3.org/2000/svg';
      const OVL_GROUP = 'sunsale-error-overlay';
      const ERR_POS   = '#66bb6a';   // observed > forecast
      const ERR_NEG   = '#ef5350';   // observed < forecast
      const ERR_PEND  = '#9e9e9e';   // observation pending (-1 sentinel)
      const PEND_PX   = 4;           // pending-marker strip height
      // Helpers used by the unified tooltip below.
      const findSteplineAt = (data, x) => {
        // Binary-search a sorted [[t, v], ...] series and return the last
        // [t, v] whose t <= x (stepline semantics). Null if x precedes data.
        if (!data.length || x < data[0][0]) return null;
        let lo = 0, hi = data.length - 1;
        while (lo < hi) {
          const mid = (lo + hi + 1) >>> 1;
          if (data[mid][0] <= x) lo = mid;
          else hi = mid - 1;
        }
        return data[lo];
      };
      const STORAGE_MODE_LABEL = {
        sell: 'Sell', store: 'Store', hoard: 'Hoard', dump: 'Dump',
        gulp: 'Gulp', stby: 'Standby', auto: 'Auto', track: 'Track', unknown: 'Unknown',
      };
      const STORAGE_MODE_DOT = {
        sell: '#ff9800', store: '#2196f3', hoard: '#03a9f4', dump: '#f44336',
        gulp: '#4caf50', stby: '#7890a0', auto: '#bdbdbd', track: '#9c27b0', unknown: '#606060',
      };
      const PROFILE_MODE_LABEL = {
        solar_charge: 'battery',
        sell:         'sell',
        no_export:    'curtail',
      };

      const drawErrorOverlay = (chartContext) => {
        try {
          const w = chartContext?.w;
          if (!w || !errorSlots.length) return;
          const plotEl = chartContext.el?.querySelector('.apexcharts-graphical');
          if (!plotEl) return;
          // Wipe previous paint.
          plotEl.querySelectorAll('.' + OVL_GROUP).forEach(n => n.remove());

          // Solar-forecast bar is series index 0 → yaxis index 0 (single per-series).
          const yIdx  = 0;
          const xMin  = w.globals.minX;
          const xMax  = w.globals.maxX;
          const yMin  = w.globals.minYArr?.[yIdx] ?? w.globals.minY;
          const yMax  = w.globals.maxYArr?.[yIdx] ?? w.globals.maxY;
          const gw    = w.globals.gridWidth;
          const gh    = w.globals.gridHeight;
          if (!(xMax > xMin) || !(yMax > yMin) || !gw || !gh) return;
          const xPx   = t => ((t - xMin) / (xMax - xMin)) * gw;
          const yPx   = v => gh - ((v - yMin) / (yMax - yMin)) * gh;
          // Match the forecast bar width: plotOptions.bar.columnWidth = '100%'.
          const colW = (SLOT_MS / (xMax - xMin)) * gw;

          const g = document.createElementNS(SVG_NS, 'g');
          g.setAttribute('class', OVL_GROUP);
          for (const e of errorSlots) {
            const cx   = xPx(e.x + SLOT_MS / 2);  // bars are centred on slot midpoint
            const rect = document.createElementNS(SVG_NS, 'rect');
            rect.setAttribute('x',     String(cx - colW / 2));
            rect.setAttribute('width', String(colW));
            if (e.pending) {
              // Thin grey strip sitting just above the forecast bar top —
              // visible regardless of forecast magnitude (zero-forecast slots
              // get the strip pinned to the x-axis).
              const yTop = yPx(e.forecastKwh);
              rect.setAttribute('y',      String(yTop - PEND_PX));
              rect.setAttribute('height', String(PEND_PX));
              rect.setAttribute('fill',   ERR_PEND);
              rect.setAttribute('fill-opacity', '0.7');
            } else {
              const yTop = yPx(Math.max(e.forecastKwh, e.forecastKwh + e.errorKwh));
              const yBot = yPx(Math.min(e.forecastKwh, e.forecastKwh + e.errorKwh));
              rect.setAttribute('y',      String(yTop));
              rect.setAttribute('height', String(Math.max(1, yBot - yTop)));
              rect.setAttribute('fill',   e.errorKwh > 0 ? ERR_POS : ERR_NEG);
              rect.setAttribute('fill-opacity', '0.85');
            }
            g.appendChild(rect);
          }
          plotEl.appendChild(g);
        } catch (err) {
          console.warn('sunSale: error overlay paint failed', err);
        }
      };

      // Price y-axis: actual prices fill the top half of the chart.
      // Extending the axis min down by one full natural range achieves this:
      // the tight lower bound (pMin-pPad) maps to the 50% mark, so prices
      // sit above the generation bars which are pinned to the bottom half.
      const allPriceVals = [...buyData, ...sellData].map(([, v]) => v).filter(isFinite);
      const pMin = allPriceVals.length ? Math.min(...allPriceVals) : 0;
      const pMax = allPriceVals.length ? Math.max(...allPriceVals) : 0.2;
      const pPad = Math.max((pMax - pMin) * 0.05, 0.005);
      const priceYMax = pMax + pPad;
      const priceYMinTight = pMin - pPad;
      const priceYMin = priceYMinTight - (priceYMax - priceYMinTight);

      // Generation y-axis: bars fill the bottom half of the chart.
      // Doubling the axis max means the actual peak maps to the 50% mark.
      const maxForecastKwh = forecastBars.length
        ? Math.max(...forecastBars.map(b => b.y))
        : 1.0;
      const genYMaxKwh = Math.ceil(maxForecastKwh * 1000 * 1.1 / 100) * 100 / 1000 * 2;

      const options = {
        series,

        chart: {
          type:       'line',
          height:     500,
          background: 'transparent',
          toolbar: {
            show:  true,
            tools: { zoom: true, zoomin: true, zoomout: true, pan: true, reset: true, download: false },
          },
          zoom:       { enabled: true, type: 'x' },
          animations: { enabled: false },
          fontFamily: 'inherit',
          events: {
            beforeResetZoom: () => ({ xaxis: { min: windowStart, max: windowEnd } }),
            // Re-paint the forecast-error overlay after ApexCharts rebuilds
            // the SVG plot area (zoom, pan, reset). Deferred via rAF so the
            // chart's own layout has settled before we read w.globals.
            // (Wiring this into `mounted`/`updated` collapses the bar series —
            // see drawErrorOverlay docstring above.)
            zoomed:   function (ctx) { requestAnimationFrame(() => drawErrorOverlay(ctx)); },
            scrolled: function (ctx) { requestAnimationFrame(() => drawErrorOverlay(ctx)); },
          },
        },

        theme: { mode: 'dark' },

        plotOptions: {
          bar: {
            horizontal:  false,
            columnWidth: '100%',
          },
        },

        stroke: {
          show:      true,
          curve:     ['smooth',   'stepline', 'stepline', 'smooth'],
          width:     [0,          2,          2,          2       ],
          dashArray: [0,          0,          0,          4       ],
        },

        // 0:forecast (per-point fillColor; fallback) 1:amber buy 2:coral sell 3:green billing
        colors: [GREY_BAR, '#ffb300', '#ff7043', '#66bb6a'],

        fill: {
          type:    ['solid', 'solid', 'solid', 'solid'],
          opacity: [1,       1,       1,       1      ],
        },

        xaxis: {
          type: 'datetime',
          min:  windowStart,
          max:  windowEnd,
          labels: {
            datetimeUTC: false,
            format:      'dd MMM HH:mm',
            style: { colors: '#aaa', fontSize: '10px' },
            rotate: -30,
          },
          axisBorder: { show: false },
          axisTicks:  { show: false },
        },

        // Two y-axes: left = EUR/kWh shared by both price lines; right = kWh/slot
        // for the solar-forecast bar. seriesName-as-array binds multiple series
        // to one axis. The earlier "duplicate seriesName + show:false" pattern
        // left 'Sell price' with no axis match — that, combined with mixed
        // tuple/object series data, was enough to make ApexCharts emit zero
        // <rect>s for the bar series.
        yaxis: [
          {
            seriesName: ['Buy price', 'Sell price'],
            title: {
              text:  'EUR / kWh',
              style: { color: '#ffb300', fontSize: '11px' },
            },
            min:             priceYMin,
            max:             priceYMax,
            decimalsInFloat: 3,
            labels: {
              style: { colors: '#ffb300', fontSize: '10px' },
              formatter: v => (v != null ? v.toFixed(3) : ''),
            },
          },
          {
            seriesName: 'Solar forecast',
            opposite:   true,
            title: {
              text:  'kWh / slot',
              style: { color: '#cfcfcf', fontSize: '11px' },
            },
            min:             0,
            max:             genYMaxKwh,
            decimalsInFloat: 3,
            labels: {
              style: { colors: '#cfcfcf', fontSize: '10px' },
              formatter: v => (v != null ? v.toFixed(3) : ''),
            },
          },
          {
            seriesName: 'Net billing',
            opposite:   true,
            show:       false,
            decimalsInFloat: 2,
            labels: {
              formatter: v => (v != null ? v.toFixed(2) : ''),
            },
          },
        ],

        annotations: {
          yaxis: [{
            y:               0,
            borderColor:     'rgba(255,255,255,0.20)',
            strokeDashArray: 3,
          }],
          xaxis: [
            {
              x:               now,
              borderColor:     'rgba(255,255,255,0.55)',
              strokeDashArray: 5,
              label: {
                text:     'Now',
                position: 'top',
                style: {
                  color:      '#fff',
                  background: '#333',
                  fontSize:   '11px',
                  padding: { top: 3, bottom: 3, left: 6, right: 6 },
                },
              },
            },
            ...profileAnnotations,
            ...modeAnnotations,
          ],
        },

        // Custom tooltip — ApexCharts' built-in shared tooltip drops series
        // whose x-array doesn't align with the hovered series, which is the
        // norm here (sparse stepline prices vs dense 15-min bars). This
        // walks every series and looks up the value at the hovered timestamp,
        // so all data + status overlays at that time are surfaced together.
        tooltip: {
          shared:    true,
          intersect: false,
          theme:     'dark',
          custom: ({ seriesIndex, dataPointIndex, w }) => {
            // Resolve the hovered timestamp from whatever series ApexCharts
            // reported as the trigger. seriesX is per-series; fall back to
            // the configured data point's .x when the series has none.
            let hoveredX = null;
            const sx = w.globals.seriesX?.[seriesIndex];
            if (sx && sx[dataPointIndex] != null) {
              hoveredX = sx[dataPointIndex];
            } else {
              const pt = w.config.series[seriesIndex]?.data?.[dataPointIndex];
              if (pt && pt.x != null) hoveredX = pt.x;
            }
            if (hoveredX == null) return '';

            const slotT = Math.floor(hoveredX / SLOT_MS) * SLOT_MS;
            const date  = new Date(hoveredX);
            const timeStr = date.toLocaleString([], {
              day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit',
            });

            const lines = [];
            const dot = c => `<span style="display:inline-block;width:9px;color:${c}">●</span>`;
            const sq  = c => `<span style="display:inline-block;width:9px;color:${c}">▮</span>`;

            // Buy price (stepline — value persists between updates).
            const buyPt = findSteplineAt(buyData, hoveredX);
            if (buyPt) {
              lines.push(`<div>${dot('#ffb300')} Buy price: <strong>${buyPt[1].toFixed(4)}</strong> €/kWh</div>`);
            }

            // Sell price (stepline).
            const sellPt = findSteplineAt(sellData, hoveredX);
            if (sellPt) {
              lines.push(`<div>${dot('#ff7043')} Sell price: <strong>${sellPt[1].toFixed(4)}</strong> €/kWh</div>`);
            }

            // Solar forecast bar at this 15-min slot + charging-profile disposition.
            const fBar = forecastBars.find(b => b.x === slotT);
            if (fBar) {
              const prof = profileSlots.get(slotT);
              let line = `<div>${dot(fBar.fillColor)} Solar forecast: <strong>${fBar.y.toFixed(3)}</strong> kWh`;
              if (prof) {
                if (prof.mode === 'sell') {
                  line += ` → sell @ ${prof.sell_eur_kwh.toFixed(3)} €/kWh`;
                } else if (PROFILE_MODE_LABEL[prof.mode]) {
                  line += ` → ${PROFILE_MODE_LABEL[prof.mode]}`;
                }
              }
              line += '</div>';
              lines.push(line);
            }

            // Forecast-vs-observed error (drawn as overlay rects on chart).
            const errSlot = errorSlots.find(e => e.x === slotT);
            if (errSlot) {
              if (errSlot.pending) {
                lines.push(`<div style="padding-left:14px;font-size:11px;color:#9e9e9e">↳ Forecast error: pending observation</div>`);
              } else {
                const sign  = errSlot.errorKwh >= 0 ? '+' : '';
                const color = errSlot.errorKwh > 0 ? '#66bb6a' : '#ef5350';
                lines.push(`<div style="padding-left:14px;font-size:11px;color:${color}">↳ Forecast error: ${sign}${errSlot.errorKwh.toFixed(3)} kWh</div>`);
              }
            }

            // Net billing running total at this slot (past only).
            const billPt = billingData.find(p => p.x === slotT);
            if (billPt) {
              const sign = billPt.y >= 0 ? '+' : '';
              lines.push(`<div>${dot('#66bb6a')} Net total: <strong>${sign}${billPt.y.toFixed(2)}</strong> €</div>`);
            }

            // Inverter storage-mode band covering this instant (history or plan).
            const modeAt = modeBands.find(b => hoveredX >= b.x && hoveredX < b.x2);
            if (modeAt) {
              const dotColor = STORAGE_MODE_DOT[modeAt.mode] || '#9e9e9e';
              const label    = STORAGE_MODE_LABEL[modeAt.mode] || modeAt.mode;
              lines.push(`<div>${sq(dotColor)} Inverter mode: <strong>${label}</strong></div>`);
            }

            if (!lines.length) return '';

            return `
              <div style="
                padding: 8px 12px;
                background: #1f1f1f;
                border: 1px solid #555;
                border-radius: 4px;
                font-size: 12px;
                color: #e0e0e0;
                line-height: 1.55;
                min-width: 240px;
                box-shadow: 0 4px 12px rgba(0,0,0,0.4);
              ">
                <div style="font-size:11px;color:#aaa;margin-bottom:6px;padding-bottom:4px;border-bottom:1px solid #444;">
                  ${timeStr}
                </div>
                ${lines.join('')}
              </div>
            `;
          },
        },

        legend: {
          show:   true,
          labels: { colors: '#aaa' },
        },

        grid: {
          borderColor: 'rgba(255,255,255,0.07)',
          xaxis: { lines: { show: true } },
          yaxis: { lines: { show: true } },
        },

        markers:    { size: 0 },
        dataLabels: { enabled: false },
      };

      const el = this.shadowRoot.querySelector('#chart');
      this._chart = new ApexCharts(el, options);
      this._chart.render().then(() => drawErrorOverlay(this._chart));

      this._renderBillSummary();

      const dashAttrsForQuality = this._hass.states[DASHBOARD_ENTITY]?.attributes;
      this._renderAccuracySection(dashAttrsForQuality?.forecast_quality ?? null);
    }

    // ── Net Billing ────────────────────────────────────────────────────────────

    // Build running-total {x, y} points for the main chart from windowStart to nowMs.
    _buildBillingSeries(windowStart, nowMs) {
      const billAttrs = this._hass.states[MONTHLY_BILL_ENTITY]?.attributes;
      if (!billAttrs || !Array.isArray(billAttrs.slots) || !billAttrs.slots.length) return [];

      const { carry_eur = 0, slots } = billAttrs;
      const sorted = [...slots].sort((a, b) =>
        new Date(a.start).getTime() - new Date(b.start).getTime()
      );

      let running = carry_eur;
      const data = [];
      for (const s of sorted) {
        const t = new Date(s.start).getTime();
        running += s.net_cost_eur ?? 0;
        if (t >= windowStart && t <= nowMs) {
          data.push({ x: t, y: running });
        }
      }
      return data;
    }

    _renderBillSummary() {
      const container = this.shadowRoot.querySelector('#bill');
      if (!container) return;

      const billAttrs = this._hass.states[MONTHLY_BILL_ENTITY]?.attributes;
      if (!billAttrs || !Array.isArray(billAttrs.slots) || !billAttrs.slots.length) {
        container.innerHTML = '<div class="bill-title">Net Billing</div>'
          + '<div class="bill-summary" style="color:#666">No billing data yet.</div>';
        return;
      }

      const {
        carry_eur, yday_to_now_eur, total_month_eur, month_str,
        previous_month_str, previous_month_eur,
      } = billAttrs;

      const fmt2 = v => (v >= 0 ? '+' : '') + v.toFixed(2) + ' €';
      const prevHtml = (previous_month_str)
        ? `<span class="bill-sep">·</span>`
          + `<span class="bill-label">Prev ${previous_month_str}:</span>`
          + `<span class="bill-value">${fmt2(previous_month_eur)}</span>`
        : '';

      container.innerHTML = `
        <div class="bill-title">Net Billing — ${month_str}</div>
        <div class="bill-summary">
          <span class="bill-label">Carry:</span>
          <span class="bill-value">${fmt2(carry_eur)}</span>
          <span class="bill-sep">·</span>
          <span class="bill-label">Live:</span>
          <span class="bill-value">${fmt2(yday_to_now_eur)}</span>
          <span class="bill-sep">·</span>
          <span class="bill-label">Total:</span>
          <span class="bill-value ${total_month_eur < 0 ? 'revenue' : ''}">${fmt2(total_month_eur)}</span>
          ${prevHtml}
        </div>
      `;
    }

    // ── Forecast Quality Charts ────────────────────────────────────────────────

    _buildQualityChartOptions(title, xLabels, metricsArr) {
      // metricsArr: [{n, bias_wh, mae_wh, rmse_wh, mape_pct, r2}, ...] aligned with xLabels.
      const get = (key) => metricsArr.map(m => (m && m[key] != null) ? m[key] : null);

      const maeSeries   = get('mae_wh');
      const rmseSeries  = get('rmse_wh');
      const biasSeries  = get('bias_wh');
      const mapeSeries  = get('mape_pct');
      const r2Series    = get('r2').map(v => v != null ? +(v * 100).toFixed(2) : null);
      const nSeries     = metricsArr.map(m => m ? m.n : 0);

      return {
        series: [
          { name: 'MAE (Wh)',   type: 'bar',  data: maeSeries  },
          { name: 'RMSE (Wh)',  type: 'line', data: rmseSeries },
          { name: 'Bias (Wh)',  type: 'line', data: biasSeries },
          { name: 'MAPE (%)',   type: 'line', data: mapeSeries },
          { name: 'R²×100 (%)', type: 'line', data: r2Series   },
        ],
        chart: {
          type:       'line',
          height:     300,
          background: 'transparent',
          toolbar:    { show: false },
          animations: { enabled: false },
          fontFamily: 'inherit',
        },
        theme: { mode: 'dark' },
        plotOptions: {
          bar: { horizontal: false, columnWidth: '60%' },
        },
        stroke: {
          show:      true,
          curve:     ['smooth', 'smooth', 'smooth', 'smooth', 'smooth'],
          width:     [0,        2,        2,        2,        2       ],
          dashArray: [0,        5,        3,        0,        8       ],
        },
        colors: ['#ffb300', '#ff7043', '#42a5f5', '#66bb6a', '#ab47bc'],
        fill: {
          type:    ['solid', 'solid', 'solid', 'solid', 'solid'],
          opacity: [0.8,     1,       1,       1,       1      ],
        },
        xaxis: {
          categories: xLabels,
          labels: {
            style:  { colors: '#aaa', fontSize: '10px' },
            rotate: -30,
          },
          axisBorder: { show: false },
          axisTicks:  { show: false },
        },
        yaxis: [
          {
            seriesName: ['MAE (Wh)', 'RMSE (Wh)', 'Bias (Wh)'],
            title: {
              text:  'Wh',
              style: { color: '#ffb300', fontSize: '10px' },
            },
            forceNiceScale:  true,
            decimalsInFloat: 1,
            labels: {
              style:     { colors: '#ffb300', fontSize: '10px' },
              formatter: v => (v != null ? v.toFixed(1) : ''),
            },
          },
          {
            seriesName: ['MAPE (%)', 'R²×100 (%)'],
            opposite:   true,
            title: {
              text:  '%',
              style: { color: '#66bb6a', fontSize: '10px' },
            },
            forceNiceScale:  true,
            decimalsInFloat: 1,
            labels: {
              style:     { colors: '#66bb6a', fontSize: '10px' },
              formatter: v => (v != null ? v.toFixed(1) : ''),
            },
          },
        ],
        tooltip: {
          shared:    true,
          intersect: false,
          theme:     'dark',
          y: {
            formatter: (val, { seriesIndex }) => {
              if (val == null) return '—';
              const units = ['Wh', 'Wh', 'Wh', '%', '%'];
              const label = ['MAE', 'RMSE', 'Bias', 'MAPE', 'R²×100'][seriesIndex] || '';
              const n = nSeries[/* dataPointIndex not avail here */0] || '';
              return `${val.toFixed(1)} ${units[seriesIndex]}`;
            },
          },
        },
        legend: {
          show:   true,
          labels: { colors: '#aaa' },
        },
        grid: {
          borderColor: 'rgba(255,255,255,0.07)',
          xaxis: { lines: { show: false } },
          yaxis: { lines: { show: true  } },
        },
        markers:    { size: 0 },
        dataLabels: { enabled: false },
        title: {
          text:  title,
          style: { color: '#aaa', fontSize: '12px', fontWeight: '600' },
          margin: 4,
        },
      };
    }

    _renderAccuracySection(quality) {
      const container = this.shadowRoot.querySelector('#accuracy');
      if (!container) return;

      if (!quality || (!Object.keys(quality.group1 || {}).length &&
                       !Object.keys(quality.group2 || {}).length &&
                       !Object.keys(quality.group3 || {}).length)) {
        container.innerHTML = '<div class="accuracy-title">Forecast Quality</div>'
          + '<div class="accuracy-subtitle" style="color:#666">No quality data yet — accumulates over time.</div>';
        return;
      }

      // Destroy previous charts.
      if (this._g1Chart) { this._g1Chart.destroy(); this._g1Chart = null; }
      if (this._g2Chart) { this._g2Chart.destroy(); this._g2Chart = null; }
      if (this._g3Chart) { this._g3Chart.destroy(); this._g3Chart = null; }

      const sunriseStr = quality.sunrise_utc
        ? new Date(quality.sunrise_utc).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'})
        : '—';
      const sunsetStr = quality.sunset_utc
        ? new Date(quality.sunset_utc).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'})
        : '—';

      container.innerHTML = `
        <div class="accuracy-title">Forecast Quality</div>
        <div class="accuracy-subtitle">Sunrise ${sunriseStr} · Sunset ${sunsetStr} (local)</div>
        <div class="accuracy-subtitle">EMA α=0.1 running accuracy per bucket</div>
        <div id="g1-chart" class="accuracy-chart"></div>
        <div id="g2-chart" class="accuracy-chart"></div>
        <div id="g3-chart" class="accuracy-chart"></div>
      `;

      // Group 1: intensity bins — sorted numerically by Wh.
      {
        const g1 = quality.group1 || {};
        const keys = Object.keys(g1).map(Number).sort((a, b) => a - b);
        const labels = keys.map(k => k + ' Wh');
        const metrics = keys.map(k => g1[String(k)]);
        const el = container.querySelector('#g1-chart');
        const opts = this._buildQualityChartOptions(
          'Group 1 — Accuracy by Predicted Intensity (per forecast-kWh bin)',
          labels, metrics,
        );
        this._g1Chart = new ApexCharts(el, opts);
        this._g1Chart.render();
      }

      // Group 2: solar-day positional buckets — sorted 1..N.
      {
        const g2 = quality.group2 || {};
        const keys = Object.keys(g2).map(Number).sort((a, b) => a - b);
        const n = keys.length;
        const half = Math.ceil(n / 2);
        const labels = keys.map(k => {
          if (k <= half) return `Dawn +${k - 1}`;
          return `Dusk -${n - k}`;
        });
        const metrics = keys.map(k => g2[String(k)]);
        const el = container.querySelector('#g2-chart');
        const opts = this._buildQualityChartOptions(
          'Group 2 — Accuracy by Solar-Day Position (Dawn → Dusk)',
          labels, metrics,
        );
        this._g2Chart = new ApexCharts(el, opts);
        this._g2Chart.render();
      }

      // Group 3: horizon buckets d0–d6.
      {
        const g3 = quality.group3 || {};
        const keys = Object.keys(g3).map(Number).sort((a, b) => a - b);
        const labels = keys.map(k => `d${k}`);
        const metrics = keys.map(k => g3[String(k)]);
        const el = container.querySelector('#g3-chart');
        const opts = this._buildQualityChartOptions(
          'Group 3 — Accuracy by Forecast Horizon (d0 = same day, d6 = 6 days ahead)',
          labels, metrics,
        );
        this._g3Chart = new ApexCharts(el, opts);
        this._g3Chart.render();
      }
    }
  }

  customElements.define('sun-sale-panel', SunSalePanel);
})();
