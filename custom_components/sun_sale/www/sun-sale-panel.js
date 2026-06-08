/* Sun Sale Dashboard Panel
 *
 * Daily generation row (above main chart):
 *   Compact text pills — 8 days: yesterday → +6 days.
 *   Yesterday/Today show predicted / actual (today also shows remaining-forecast).
 *   Future days show predicted only. Data: forecast_daily_kwh,
 *   actual_yesterday_kwh, actual_today_kwh.
 *
 * 72-hour window: yesterday 00:00 → tomorrow 23:59 (local)
 *
 * Left Y axis — EUR/kWh:
 *   Buy price   amber  solid stepline  — past history + future from pricing sensor (one continuous line)
 *   Sell price  coral  solid stepline  — past history + future from pricing sensor (one continuous line)
 *
 * Right Y axis — kWh/slot (15-min):
 *   Solar forecast — vertical bar per 15-min slot (scalar y = forecast_kwh), grey 25 % opacity.
 *
 *   Forecast-vs-observed error overlay — green/red rect per slot, anchored
 *   at the top of the forecast bar (y = forecast_kwh):
 *     +error (observed > forecast)  → green segment grows UP
 *     -error (observed < forecast)  → red segment grows DOWN
 *   Rendered as an ECharts custom series so it survives zoom/pan natively.
 *
 * Overlays:
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
  const ECHARTS_CDN           = 'https://cdn.jsdelivr.net/npm/echarts@5.5.1/dist/echarts.min.js';

  // ── Helpers ────────────────────────────────────────────────────────────────

  function localMidnight(offsetDays) {
    const d = new Date();
    d.setHours(0, 0, 0, 0);
    d.setDate(d.getDate() + offsetDays);
    return d.getTime();
  }

  function loadECharts() {
    if (window.echarts) return Promise.resolve();
    return new Promise((resolve, reject) => {
      const s = document.createElement('script');
      s.src = ECHARTS_CDN;
      s.onload = resolve;
      s.onerror = () => reject(new Error('Could not load ECharts from CDN'));
      document.head.appendChild(s);
    });
  }

  // ── Panel element ──────────────────────────────────────────────────────────

  class SunSalePanel extends HTMLElement {
    constructor() {
      super();
      this._hass         = null;
      this._chart        = null;
      this._g1Chart      = null;
      this._g2Chart      = null;
      this._g3Chart      = null;
      this._resizeObs    = null;
      this._initialized  = false;
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
        this._renderGenerationRow(dashAttrs);
        const panel = this.shadowRoot?.querySelector('#schedule-panel');
        if (panel?.classList.contains('open')) this._syncScheduleDrawer();
      }
    }

    disconnectedCallback() {
      if (this._resizeObs) { this._resizeObs.disconnect(); this._resizeObs = null; }
      if (this._chart)   { this._chart.dispose();   this._chart   = null; }
      if (this._g1Chart) { this._g1Chart.dispose(); this._g1Chart = null; }
      if (this._g2Chart) { this._g2Chart.dispose(); this._g2Chart = null; }
      if (this._g3Chart) { this._g3Chart.dispose(); this._g3Chart = null; }
    }

    // ── Boot ──────────────────────────────────────────────────────────────────

    async _boot() {
      this._buildShell();
      try {
        await loadECharts();
      } catch (e) {
        this._setStatus('⚠ ' + e.message + ' — check network or install ECharts via HACS.');
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
          .header-row {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
          }
          #schedule-toggle {
            background: rgba(255, 255, 255, 0.06);
            color: var(--primary-text-color, #fff);
            border: 1px solid rgba(255, 255, 255, 0.12);
            border-radius: 6px;
            padding: 6px 12px;
            font-size: 0.8rem;
            cursor: pointer;
            font-family: inherit;
            display: inline-flex;
            align-items: center;
            gap: 6px;
          }
          #schedule-toggle:hover { background: rgba(255, 255, 255, 0.10); }
          #schedule-toggle.open { background: rgba(66, 165, 245, 0.18); border-color: rgba(66, 165, 245, 0.50); }
          #schedule-panel {
            display: none;
            margin: 0 0 16px;
            padding: 14px 16px;
            background: rgba(255, 255, 255, 0.03);
            border: 1px solid rgba(255, 255, 255, 0.08);
            border-radius: 8px;
          }
          #schedule-panel.open { display: block; }
          .sched-section-title {
            font-size: 0.75rem;
            font-weight: 600;
            color: var(--secondary-text-color, #888);
            text-transform: uppercase;
            letter-spacing: 0.05em;
            margin: 0 0 8px;
          }
          .sched-section + .sched-section { margin-top: 14px; }
          .sched-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
            gap: 8px 16px;
          }
          .sched-row {
            display: flex;
            align-items: center;
            justify-content: flex-start;
            gap: 10px;
            padding: 6px 0;
            font-size: 0.85rem;
            color: var(--primary-text-color, #fff);
          }
          .sched-row .sched-label {
            color: var(--secondary-text-color, #bbb);
            flex: 1 1 auto;
            min-width: 0;
          }
          .sched-row .sched-missing {
            color: #ef5350;
            font-size: 0.75rem;
          }
          .sched-toggle {
            position: relative;
            display: inline-block;
            width: 36px;
            height: 20px;
            flex: 0 0 auto;
          }
          .sched-toggle input { opacity: 0; width: 0; height: 0; }
          .sched-toggle .slider {
            position: absolute;
            cursor: pointer;
            inset: 0;
            background-color: #555;
            border-radius: 20px;
            transition: background-color 0.15s;
          }
          .sched-toggle .slider:before {
            position: absolute;
            content: "";
            height: 14px;
            width: 14px;
            left: 3px;
            top: 3px;
            background-color: #fff;
            border-radius: 50%;
            transition: transform 0.15s;
          }
          .sched-toggle input:checked + .slider { background-color: #42a5f5; }
          .sched-toggle input:checked + .slider:before { transform: translateX(16px); }
          .sched-toggle input:disabled + .slider { opacity: 0.4; cursor: not-allowed; }
          .sched-num {
            display: flex;
            align-items: center;
            gap: 6px;
            flex: 0 0 auto;
          }
          .sched-num input {
            width: 78px;
            padding: 4px 6px;
            background: rgba(255, 255, 255, 0.06);
            border: 1px solid rgba(255, 255, 255, 0.12);
            border-radius: 4px;
            color: var(--primary-text-color, #fff);
            font-family: inherit;
            font-size: 0.85rem;
            text-align: right;
          }
          .sched-num input:focus { outline: 1px solid #42a5f5; outline-offset: -1px; }
          .sched-num input:disabled { opacity: 0.4; cursor: not-allowed; }
          .sched-num .sched-unit {
            color: var(--secondary-text-color, #888);
            font-size: 0.75rem;
          }
        </style>
        <div id="card">
          <div class="header-row">
            <div>
              <h2>☀ Sun Sale</h2>
              <div id="subtitle">Buy &amp; Sell prices · Solar — 72 h window</div>
            </div>
            <button id="schedule-toggle" type="button" title="Edit schedule parameters">
              <span>⚙</span><span>Schedule</span>
            </button>
          </div>
          <div id="schedule-panel"></div>
          <div id="battery"></div>
          <div id="generation"></div>
          <div id="status">Loading…</div>
          <div id="chart"></div>
          <div id="bill"></div>
          <div id="accuracy"></div>
        </div>
      `;

      const toggleBtn = this.shadowRoot.querySelector('#schedule-toggle');
      const panel     = this.shadowRoot.querySelector('#schedule-panel');
      toggleBtn.addEventListener('click', () => {
        const open = panel.classList.toggle('open');
        toggleBtn.classList.toggle('open', open);
        if (open) this._renderScheduleDrawer();
      });
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

    _renderGenerationRow(dashAttrs) {
      const el = this.shadowRoot.querySelector('#generation');
      if (!el) return;
      if (!dashAttrs) { el.innerHTML = ''; return; }

      const daily = dashAttrs.forecast_daily_kwh;
      if (!daily) { el.innerHTML = ''; return; }

      const fmt = (v) => (typeof v === 'number' ? v.toFixed(2) : '—');

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
          inner += `<span class="pred">${predTxt}</span>`
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

    // ── Schedule parameters drawer ────────────────────────────────────────────
    // Inline editor for sunSale's schedule policy switches + numeric knobs.
    // Entities are resolved by suffix-match against the `sunsale_` prefix so
    // the panel works even if HA renames the entity_id slug (e.g. `α` in the
    // Profitability-Tilt name is unidecoded inconsistently across HA versions).

    _SCHEDULE_SWITCHES = [
      { key: 'automation',             label: 'Automation enabled',     match: ['automation'] },
      { key: 'use_standby',            label: 'Use standby (night)',    match: ['use_standby'] },
      { key: 'allow_grid_charging',    label: 'Allow grid charging',    match: ['allow_grid_charging'] },
      { key: 'allow_feed_in',          label: 'Allow feed-in',          match: ['allow_feed_in'] },
      { key: 'allow_discharge_to_grid',label: 'Allow discharge to grid',match: ['allow_discharge_to_grid'] },
    ];

    _SCHEDULE_NUMBERS = [
      { key: 'mode_change_penalty',    label: 'Mode-change penalty',    match: ['mode_change_penalty'], unit: 'EUR/kWh' },
      { key: 'profitability_tilt',     label: 'Profitability tilt α',   match: ['profitability_tilt'],  unit: '' },
      { key: 'terminal_value_discount',label: 'Terminal-value discount',match: ['terminal_value_discount'], unit: '' },
      { key: 'max_discharge_to_grid',  label: 'Max discharge to grid',  match: ['max_discharge_to_grid'], unit: 'kW' },
    ];

    _findEntityId(domain, matchAll) {
      if (!this._hass?.states) return null;
      const prefix = `${domain}.sunsale_`;
      for (const eid of Object.keys(this._hass.states)) {
        if (!eid.startsWith(prefix)) continue;
        if (matchAll.every(m => eid.includes(m))) return eid;
      }
      return null;
    }

    _renderScheduleDrawer() {
      const panel = this.shadowRoot.querySelector('#schedule-panel');
      if (!panel) return;

      const switchRow = (spec) => {
        const eid = this._findEntityId('switch', spec.match);
        if (!eid) {
          return `<div class="sched-row" data-spec="${spec.key}">
            <span class="sched-missing">entity not found</span>
            <span class="sched-label">${spec.label}</span>
          </div>`;
        }
        const st = this._hass.states[eid];
        const on = st?.state === 'on';
        const unavailable = !st || st.state === 'unavailable';
        return `<div class="sched-row" data-spec="${spec.key}">
          <label class="sched-toggle">
            <input type="checkbox" data-eid="${eid}" data-kind="switch"
                   ${on ? 'checked' : ''} ${unavailable ? 'disabled' : ''}>
            <span class="slider"></span>
          </label>
          <span class="sched-label" title="${eid}">${spec.label}</span>
        </div>`;
      };

      const numberRow = (spec) => {
        const eid = this._findEntityId('number', spec.match);
        if (!eid) {
          return `<div class="sched-row" data-spec="${spec.key}">
            <span class="sched-missing">entity not found</span>
            <span class="sched-label">${spec.label}</span>
          </div>`;
        }
        const st = this._hass.states[eid];
        const unavailable = !st || st.state === 'unavailable' || st.state === 'unknown';
        const value = unavailable ? '' : st.state;
        const attrs = st?.attributes || {};
        const min   = attrs.min  != null ? attrs.min  : 0;
        const max   = attrs.max  != null ? attrs.max  : 1;
        const step  = attrs.step != null ? attrs.step : 0.01;
        const unit  = spec.unit || attrs.unit_of_measurement || '';
        return `<div class="sched-row" data-spec="${spec.key}">
          <span class="sched-num">
            <input type="number" data-eid="${eid}" data-kind="number"
                   min="${min}" max="${max}" step="${step}"
                   value="${value}" ${unavailable ? 'disabled' : ''}>
            ${unit ? `<span class="sched-unit">${unit}</span>` : ''}
          </span>
          <span class="sched-label" title="${eid}">${spec.label}</span>
        </div>`;
      };

      panel.innerHTML = `
        <div class="sched-section">
          <div class="sched-section-title">Policy switches</div>
          <div class="sched-grid">
            ${this._SCHEDULE_SWITCHES.map(switchRow).join('')}
          </div>
        </div>
        <div class="sched-section">
          <div class="sched-section-title">Tuning knobs</div>
          <div class="sched-grid">
            ${this._SCHEDULE_NUMBERS.map(numberRow).join('')}
          </div>
        </div>
      `;

      panel.querySelectorAll('input[data-kind="switch"]').forEach((el) => {
        el.addEventListener('change', () => this._onSwitchChange(el));
      });
      panel.querySelectorAll('input[data-kind="number"]').forEach((el) => {
        el.addEventListener('change', () => this._onNumberChange(el));
      });
    }

    _syncScheduleDrawer() {
      // In-place state refresh that preserves input focus and partial typing.
      // Falls back to a full re-render when the entity wiring changes (entity
      // appears/disappears) so the "entity not found" rows update too.
      const panel = this.shadowRoot.querySelector('#schedule-panel');
      if (!panel) return;
      const expected =
        this._SCHEDULE_SWITCHES.length + this._SCHEDULE_NUMBERS.length;
      const rows = panel.querySelectorAll('.sched-row');
      if (rows.length !== expected) { this._renderScheduleDrawer(); return; }

      const active = this.shadowRoot.activeElement;
      panel.querySelectorAll('input[data-kind="switch"]').forEach((el) => {
        if (el === active) return;
        const st = this._hass.states[el.dataset.eid];
        if (!st) return;
        el.checked  = st.state === 'on';
        el.disabled = st.state === 'unavailable';
      });
      panel.querySelectorAll('input[data-kind="number"]').forEach((el) => {
        if (el === active) return;
        const st = this._hass.states[el.dataset.eid];
        if (!st) return;
        const unavailable = st.state === 'unavailable' || st.state === 'unknown';
        el.disabled = unavailable;
        if (!unavailable) el.value = st.state;
      });
    }

    async _onSwitchChange(el) {
      const eid = el.dataset.eid;
      const service = el.checked ? 'turn_on' : 'turn_off';
      try {
        await this._hass.callService('switch', service, { entity_id: eid });
      } catch (e) {
        console.warn('sunSale: switch service call failed', eid, e);
        // Revert visual state — HA push will re-sync on next update.
        el.checked = !el.checked;
      }
    }

    async _onNumberChange(el) {
      const eid = el.dataset.eid;
      const raw = parseFloat(el.value);
      if (!isFinite(raw)) { el.value = ''; return; }
      const min = parseFloat(el.min);
      const max = parseFloat(el.max);
      let value = raw;
      if (isFinite(min) && value < min) value = min;
      if (isFinite(max) && value > max) value = max;
      el.value = value;
      try {
        await this._hass.callService('number', 'set_value', { entity_id: eid, value });
      } catch (e) {
        console.warn('sunSale: number service call failed', eid, e);
      }
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

      const GREY_BAR = 'rgba(158,158,158,0.25)';
      const forecastBars = [];
      let t = Math.floor(windowStart / SLOT_MS) * SLOT_MS;
      while (t <= windowEnd) {
        const fKwh = forecastSlots.get(t);
        if (fKwh != null && fKwh > 0.001) {
          forecastBars.push({ x: t, y: fKwh, color: GREY_BAR });
        }
        t += SLOT_MS;
      }

      this._clearStatus();
      if (this._chart) { this._chart.dispose(); this._chart = null; }

      // ─── Inverter StorageMode bands (history yesterday→now + plan today→tomorrow).
      // History entries from inverter_mode_history are {t, mode} change events;
      // turn them into bands [event.t, next_event.t || now]. Plan slots from
      // inverter_mode_plan are {t, end_t, mode} and become bands directly.
      // StorageMode → strip color. Keys match StorageMode.value
      // (Python contract/models.py). Used by renderModeStripBlock and the
      // tooltip dot, and as the recognised-mode filter when stitching bands.
      const STORAGE_MODE_STRIP = {
        feed_in:     '#ff9800',
        self_use:    '#2196f3',
        no_export:   '#03a9f4',
        discharge:   '#f44336',
        grid_charge: '#4caf50',
        stand_by:    '#7890a0',
        auto:        '#bdbdbd',
        track:       '#9c27b0',
        unknown:     '#606060',
      };
      const modeBands = [];
      {
        const nowMs = Number(dashAttrs?.now_ts) || Date.now();
        const histRaw = Array.isArray(dashAttrs?.inverter_mode_history)
          ? [...dashAttrs.inverter_mode_history].sort((a, b) => a.t - b.t)
          : [];
        for (let i = 0; i < histRaw.length; i++) {
          const ev = histRaw[i];
          if (!STORAGE_MODE_STRIP[ev.mode]) continue;
          const nextStart = i + 1 < histRaw.length ? histRaw[i + 1].t : nowMs;
          if (nextStart <= ev.t) continue;
          modeBands.push({ mode: ev.mode, x: ev.t, x2: nextStart });
        }
        const planRaw = Array.isArray(dashAttrs?.inverter_mode_plan)
          ? dashAttrs.inverter_mode_plan
          : [];
        for (const s of planRaw) {
          if (!STORAGE_MODE_STRIP[s.mode]) continue;
          if (!(s.end_t > s.t)) continue;
          modeBands.push({ mode: s.mode, x: s.t, x2: s.end_t });
        }
      }

      const STORAGE_MODE_LABEL = {
        feed_in: 'Feed-in', self_use: 'Self-use', no_export: 'No export',
        discharge: 'Discharge', grid_charge: 'Grid charge', stand_by: 'Standby',
        auto: 'Auto', track: 'Track', unknown: 'Unknown',
      };
      const STORAGE_MODE_DOT = STORAGE_MODE_STRIP;

      const billingData = this._buildBillingSeries(windowStart, now);
      const { imported: importData, exported: exportData } =
        this._buildImportExportSeries(windowStart, now);

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

      // O(1) slot lookups for the tooltip formatter (avoids per-mousemove scans).
      const forecastBarBySlot = new Map(forecastBars.map(b => [b.x, b]));
      const errorBySlot       = new Map(errorSlots.map(e => [e.x, e]));
      const billingBySlot     = new Map(billingData.map(p => [p.x, p]));
      const importBySlot      = new Map(importData.map(p => [p.x, p]));
      const exportBySlot      = new Map(exportData.map(p => [p.x, p]));

      // Stepline binary search: prices are sparse, so we interpolate by holding
      // the last value whose timestamp ≤ x (stepline 'end' semantics).
      const findSteplineAt = (data, x) => {
        if (!data.length || x < data[0][0]) return null;
        let lo = 0, hi = data.length - 1;
        while (lo < hi) {
          const mid = (lo + hi + 1) >>> 1;
          if (data[mid][0] <= x) lo = mid;
          else hi = mid - 1;
        }
        return data[lo];
      };

      // Forecast bars rendered as a custom series. Native `type: 'bar'` on a
      // time axis auto-sizes bar width from data density, leaving gaps when
      // the visible range zooms in; rendering rects directly via api.coord()
      // guarantees each rect spans exactly its 15-min slot regardless of zoom.
      const renderForecastBar = (params, api) => {
        const bar = forecastBars[params.dataIndex];
        if (!bar) return;
        const xLeft  = api.coord([bar.x,           0])[0];
        const xRight = api.coord([bar.x + SLOT_MS, 0])[0];
        const yTop   = api.coord([bar.x, bar.y])[1];
        const yBot   = api.coord([bar.x, 0])[1];
        return {
          type: 'rect',
          shape: {
            x:      xLeft,
            y:      yTop,
            width:  Math.max(1, xRight - xLeft),
            height: Math.max(1, yBot - yTop),
          },
          style: { fill: bar.color },
        };
      };

      // Forecast-error overlay as a second custom series. Each rect is anchored
      // at the top of its forecast bar (y = forecast_kwh) and extends UP by
      // +error (green: under-forecast) or DOWN by |error| (red: over-forecast).
      // Pending observations (-1 sentinel) render as a thin grey strip above
      // the bar top. Width matches the forecast bar exactly (same xLeft/xRight
      // derivation), so the two custom series stay aligned at every zoom level.
      const PEND_PX = 4;
      const renderErrorRect = (params, api) => {
        const slot = errorSlots[params.dataIndex];
        if (!slot) return;
        const xLeft  = api.coord([slot.x,           0])[0];
        const xRight = api.coord([slot.x + SLOT_MS, 0])[0];
        const width  = Math.max(1, xRight - xLeft);
        if (slot.pending) {
          const yTop = api.coord([slot.x, slot.forecastKwh])[1];
          return {
            type: 'rect',
            shape: { x: xLeft, y: yTop - PEND_PX, width, height: PEND_PX },
            style: { fill: '#9e9e9e', opacity: 0.7 },
          };
        }
        const yUpper = api.coord([slot.x, Math.max(slot.forecastKwh, slot.forecastKwh + slot.errorKwh)])[1];
        const yLower = api.coord([slot.x, Math.min(slot.forecastKwh, slot.forecastKwh + slot.errorKwh)])[1];
        return {
          type: 'rect',
          shape: { x: xLeft, y: yUpper, width, height: Math.max(1, yLower - yUpper) },
          style: { fill: slot.errorKwh > 0 ? '#66bb6a' : '#ef5350', opacity: 0.85 },
        };
      };

      // Inverter-mode strip: a thin lane above the plot area that paints each
      // (history + plan) band as a solid block, so mode is always visible even
      // for slots whose forecast is zero (e.g. night, no expected generation).
      // y/height are pixel-absolute against the grid origin so the strip stays
      // fixed at the top regardless of y-axis zoom.
      const STRIP_HEIGHT_PX = 10;
      const STRIP_GAP_PX    = 6;
      const renderModeStripBlock = (params, api) => {
        const band = modeBands[params.dataIndex];
        if (!band) return;
        const xLeft  = api.coord([band.x,  0])[0];
        const xRight = api.coord([band.x2, 0])[0];
        const gridTop = params.coordSys.y;
        return {
          type: 'rect',
          shape: {
            x:      xLeft,
            y:      gridTop - STRIP_HEIGHT_PX - STRIP_GAP_PX,
            width:  Math.max(1, xRight - xLeft),
            height: STRIP_HEIGHT_PX,
          },
          style: {
            fill:   STORAGE_MODE_STRIP[band.mode] || '#606060',
            stroke: 'rgba(0,0,0,0.35)',
            lineWidth: 0.5,
          },
        };
      };

      const dot = c => `<span style="display:inline-block;width:9px;color:${c}">●</span>`;
      const sq  = c => `<span style="display:inline-block;width:9px;color:${c}">▮</span>`;

      const series = [
        {
          name:       'Solar forecast',
          type:       'custom',
          yAxisIndex: 1,
          renderItem: renderForecastBar,
          data:       forecastBars.map(b => [b.x + SLOT_MS / 2, b.y]),
          itemStyle:  { color: GREY_BAR },   // legend swatch fallback
          z:          2,
          markLine:   {
            symbol: 'none',
            silent: true,
            data: [{
              xAxis: now,
              lineStyle: { color: 'rgba(255,255,255,0.55)', type: 'dashed', width: 1 },
              label: {
                show: true,
                position: 'insideEndTop',
                formatter: 'Now',
                color: '#fff',
                backgroundColor: '#333',
                padding: [3, 6, 3, 6],
                fontSize: 11,
              },
            }],
          },
        },
        {
          name:       'Buy price',
          type:       'line',
          yAxisIndex: 0,
          step:       'end',
          showSymbol: false,
          lineStyle:  { color: '#ffb300', width: 2 },
          itemStyle:  { color: '#ffb300' },
          data:       buyData,
          z:          4,
        },
        {
          name:       'Sell price',
          type:       'line',
          yAxisIndex: 0,
          step:       'end',
          showSymbol: false,
          lineStyle:  { color: '#ff7043', width: 2 },
          itemStyle:  { color: '#ff7043' },
          data:       sellData,
          z:          4,
        },
        {
          name:       'Net billing',
          type:       'line',
          yAxisIndex: 2,
          smooth:     true,
          showSymbol: false,
          lineStyle:  { color: '#66bb6a', width: 2, type: 'dashed' },
          itemStyle:  { color: '#66bb6a' },
          data:       billingData.map(p => [p.x, p.y]),
          z:          3,
        },
        {
          name:       'Grid import',
          type:       'line',
          yAxisIndex: 3,
          smooth:     true,
          showSymbol: false,
          lineStyle:  { color: '#ef5350', width: 2, type: 'dashed' },
          itemStyle:  { color: '#ef5350' },
          data:       importData.map(p => [p.x, p.y]),
          z:          3,
        },
        {
          name:       'Grid export',
          type:       'line',
          yAxisIndex: 3,
          smooth:     true,
          showSymbol: false,
          lineStyle:  { color: '#42a5f5', width: 2, type: 'dashed' },
          itemStyle:  { color: '#42a5f5' },
          data:       exportData.map(p => [p.x, p.y]),
          z:          3,
        },
        {
          name:       'Forecast error',
          type:       'custom',
          yAxisIndex: 1,
          renderItem: renderErrorRect,
          data:       errorSlots.map(e => [e.x, e.forecastKwh]),
          itemStyle:  { color: '#66bb6a' },
          tooltip:    { show: false },
          silent:     true,
          z:          5,
        },
        {
          name:       'Inverter mode',
          type:       'custom',
          yAxisIndex: 1,
          renderItem: renderModeStripBlock,
          // One datapoint per band; the x value picks the slot for ECharts'
          // tooltip slot-bucketing, while the renderItem reads the band from
          // the modeBands closure for the full (x, x2, mode) tuple.
          data:       modeBands.map(b => [b.x, 0]),
          tooltip:    { show: false },
          silent:     true,
          z:          6,
        },
      ];

      const option = {
        backgroundColor: 'transparent',
        animation:       false,
        textStyle:       { fontFamily: 'inherit', color: '#aaa' },
        grid: {
          left:   60,
          right:  60,
          // top includes ~16 px reserved for the inverter-mode strip lane
          // (see renderModeStripBlock — STRIP_HEIGHT_PX + STRIP_GAP_PX).
          top:    56,
          bottom: 70,
        },
        legend: {
          show:        true,
          textStyle:   { color: '#aaa' },
          data:        ['Solar forecast', 'Buy price', 'Sell price', 'Net billing', 'Grid import', 'Grid export'],
          top:         5,
        },
        xAxis: {
          type: 'time',
          min:  windowStart,
          max:  windowEnd,
          axisLine: { show: false },
          axisTick: { show: false },
          splitLine: { show: true, lineStyle: { color: 'rgba(255,255,255,0.07)' } },
          axisLabel: {
            color:    '#aaa',
            fontSize: 10,
            rotate:   -30,
            formatter: (val) => {
              const d = new Date(val);
              const dd  = String(d.getDate()).padStart(2, '0');
              const mon = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][d.getMonth()];
              const hh  = String(d.getHours()).padStart(2, '0');
              const mm  = String(d.getMinutes()).padStart(2, '0');
              return `${dd} ${mon} ${hh}:${mm}`;
            },
          },
        },
        // Four y-axes: 0=price (left), 1=forecast kWh (right), 2=billing EUR (hidden),
        // 3=import/export kWh (hidden). Hidden axes auto-scale per series so cumulative
        // billing/import/export lines stay readable without sharing the price scale.
        yAxis: [
          {
            type:     'value',
            name:     'EUR / kWh',
            nameTextStyle: { color: '#ffb300', fontSize: 11 },
            position: 'left',
            min:      priceYMin,
            max:      priceYMax,
            axisLine: { show: false },
            axisTick: { show: false },
            splitLine: { lineStyle: { color: 'rgba(255,255,255,0.07)' } },
            axisLabel: {
              color:    '#ffb300',
              fontSize: 10,
              formatter: v => (v != null ? v.toFixed(3) : ''),
            },
          },
          {
            type:     'value',
            name:     'kWh / slot',
            nameTextStyle: { color: '#cfcfcf', fontSize: 11 },
            position: 'right',
            min:      0,
            max:      genYMaxKwh,
            axisLine: { show: false },
            axisTick: { show: false },
            splitLine: { show: false },
            axisLabel: {
              color:    '#cfcfcf',
              fontSize: 10,
              formatter: v => (v != null ? v.toFixed(3) : ''),
            },
          },
          { type: 'value', position: 'right', show: false },
          { type: 'value', position: 'right', show: false },
        ],
        dataZoom: [
          { type: 'inside', xAxisIndex: 0, filterMode: 'none' },
          {
            type:       'slider',
            xAxisIndex: 0,
            filterMode: 'none',
            bottom:     8,
            height:     20,
            startValue: windowStart,
            endValue:   windowEnd,
            backgroundColor:      'rgba(255,255,255,0.04)',
            fillerColor:          'rgba(255,255,255,0.10)',
            borderColor:          'transparent',
            handleStyle:          { color: '#888' },
            moveHandleStyle:      { color: '#888' },
            textStyle:            { color: '#888' },
            dataBackground:       { lineStyle: { opacity: 0 }, areaStyle: { opacity: 0 } },
            selectedDataBackground: { lineStyle: { opacity: 0 }, areaStyle: { opacity: 0 } },
          },
        ],
        toolbox: {
          right: 20,
          top:   5,
          iconStyle: { borderColor: '#aaa' },
          feature: {
            dataZoom: { yAxisIndex: 'none' },
            restore:  {},
          },
        },
        tooltip: {
          trigger:        'axis',
          axisPointer:    { type: 'line', lineStyle: { color: 'rgba(255,255,255,0.35)' } },
          backgroundColor: '#1f1f1f',
          borderColor:    '#555',
          textStyle:      { color: '#e0e0e0', fontSize: 12 },
          padding:        [8, 12],
          extraCssText:   'min-width:240px; line-height:1.55; box-shadow:0 4px 12px rgba(0,0,0,0.4);',
          formatter: (params) => {
            const hoveredX = Array.isArray(params)
              ? (params[0]?.axisValue)
              : params.axisValue;
            if (hoveredX == null) return '';
            const xMs   = typeof hoveredX === 'number' ? hoveredX : new Date(hoveredX).getTime();
            const slotT = Math.floor(xMs / SLOT_MS) * SLOT_MS;
            const timeStr = new Date(xMs).toLocaleString([], {
              day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit',
              hour12: false,
            });
            const lines = [];

            const buyPt = findSteplineAt(buyData, xMs);
            if (buyPt) lines.push(`<div>${dot('#ffb300')} Buy price: <strong>${buyPt[1].toFixed(4)}</strong> €/kWh</div>`);

            const sellPt = findSteplineAt(sellData, xMs);
            if (sellPt) lines.push(`<div>${dot('#ff7043')} Sell price: <strong>${sellPt[1].toFixed(4)}</strong> €/kWh</div>`);

            const fBar = forecastBarBySlot.get(slotT);
            if (fBar) {
              const line = `<div>${dot(fBar.color)} Solar forecast: <strong>${fBar.y.toFixed(3)}</strong> kWh</div>`;
              lines.push(line);
            }

            const errSlot = errorBySlot.get(slotT);
            if (errSlot) {
              if (errSlot.pending) {
                lines.push(`<div style="padding-left:14px;font-size:11px;color:#9e9e9e">↳ Forecast error: pending observation</div>`);
              } else {
                const sign  = errSlot.errorKwh >= 0 ? '+' : '';
                const color = errSlot.errorKwh > 0 ? '#66bb6a' : '#ef5350';
                lines.push(`<div style="padding-left:14px;font-size:11px;color:${color}">↳ Forecast error: ${sign}${errSlot.errorKwh.toFixed(3)} kWh</div>`);
              }
            }

            const billPt = billingBySlot.get(slotT);
            if (billPt) {
              const sign = billPt.y >= 0 ? '+' : '';
              lines.push(`<div>${dot('#66bb6a')} Net total: <strong>${sign}${billPt.y.toFixed(2)}</strong> €</div>`);
            }

            const impPt = importBySlot.get(slotT);
            if (impPt) lines.push(`<div>${dot('#ef5350')} Import total: <strong>${impPt.y.toFixed(2)}</strong> kWh</div>`);

            const expPt = exportBySlot.get(slotT);
            if (expPt) lines.push(`<div>${dot('#42a5f5')} Export total: <strong>${expPt.y.toFixed(2)}</strong> kWh</div>`);

            const modeAt = modeBands.find(b => xMs >= b.x && xMs < b.x2);
            if (modeAt) {
              const dotColor = STORAGE_MODE_DOT[modeAt.mode] || '#9e9e9e';
              const label    = STORAGE_MODE_LABEL[modeAt.mode] || modeAt.mode;
              lines.push(`<div>${sq(dotColor)} Inverter mode: <strong>${label}</strong></div>`);
            }

            if (!lines.length) return '';
            return `<div style="font-size:11px;color:#aaa;margin-bottom:6px;padding-bottom:4px;border-bottom:1px solid #444;">${timeStr}</div>${lines.join('')}`;
          },
        },
        series,
      };

      const el = this.shadowRoot.querySelector('#chart');
      el.style.height = '500px';
      this._chart = window.echarts.init(el, null, { renderer: 'canvas' });
      this._chart.setOption(option);

      // Resize on container size changes (HA sidebar toggle, window resize).
      if (this._resizeObs) this._resizeObs.disconnect();
      this._resizeObs = new ResizeObserver(() => { this._chart?.resize(); });
      this._resizeObs.observe(el);

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

    // Build running-total {x, y} points for gross import + export kWh from
    // monthly_bill slots. Same shape as _buildBillingSeries — the slots run
    // from yesterday-or-month-start through now, so the cumulative line
    // resets to 0 at the start of that window each cycle.
    _buildImportExportSeries(windowStart, nowMs) {
      const billAttrs = this._hass.states[MONTHLY_BILL_ENTITY]?.attributes;
      if (!billAttrs || !Array.isArray(billAttrs.slots) || !billAttrs.slots.length) {
        return { imported: [], exported: [] };
      }

      const sorted = [...billAttrs.slots].sort((a, b) =>
        new Date(a.start).getTime() - new Date(b.start).getTime()
      );

      let runImp = 0, runExp = 0;
      const imported = [], exported = [];
      for (const s of sorted) {
        const t = new Date(s.start).getTime();
        runImp += s.imported_kwh ?? 0;
        runExp += s.exported_kwh ?? 0;
        if (t >= windowStart && t <= nowMs) {
          imported.push({ x: t, y: runImp });
          exported.push({ x: t, y: runExp });
        }
      }
      return { imported, exported };
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

    _buildQualityChartOption(title, xLabels, metricsArr) {
      // metricsArr: [{n, bias_wh, mae_wh, rmse_wh, mape_pct, r2}, ...] aligned with xLabels.
      const get = (key) => metricsArr.map(m => (m && m[key] != null) ? m[key] : null);

      const maeSeries  = get('mae_wh');
      const rmseSeries = get('rmse_wh');
      const biasSeries = get('bias_wh');
      const mapeSeries = get('mape_pct');
      const r2Series   = get('r2').map(v => v != null ? +(v * 100).toFixed(2) : null);

      const UNIT = ['Wh', 'Wh', 'Wh', '%', '%'];

      return {
        backgroundColor: 'transparent',
        animation:       false,
        textStyle:       { fontFamily: 'inherit', color: '#aaa' },
        title: {
          text:      title,
          textStyle: { color: '#aaa', fontSize: 12, fontWeight: 600 },
          top:       0,
          left:      'center',
        },
        grid: { left: 60, right: 60, top: 40, bottom: 40 },
        legend: {
          show:      true,
          textStyle: { color: '#aaa' },
          bottom:    0,
        },
        tooltip: {
          trigger:         'axis',
          axisPointer:     { type: 'shadow' },
          backgroundColor: '#1f1f1f',
          borderColor:     '#555',
          textStyle:       { color: '#e0e0e0', fontSize: 12 },
          padding:         [8, 12],
          formatter: (params) => {
            const head = `<div style="font-size:11px;color:#aaa;margin-bottom:4px;padding-bottom:4px;border-bottom:1px solid #444;">${params[0]?.axisValueLabel ?? ''}</div>`;
            const rows = params.map(p => {
              if (p.value == null) return '';
              const swatch = `<span style="display:inline-block;width:9px;color:${p.color}">●</span>`;
              return `<div>${swatch} ${p.seriesName}: <strong>${(+p.value).toFixed(1)}</strong> ${UNIT[p.seriesIndex] || ''}</div>`;
            }).filter(Boolean).join('');
            return head + rows;
          },
        },
        xAxis: {
          type:      'category',
          data:      xLabels,
          axisLine:  { show: false },
          axisTick:  { show: false },
          axisLabel: { color: '#aaa', fontSize: 10, rotate: -30 },
        },
        yAxis: [
          {
            type:          'value',
            name:          'Wh',
            nameTextStyle: { color: '#ffb300', fontSize: 10 },
            position:      'left',
            axisLine:      { show: false },
            axisTick:      { show: false },
            splitLine:     { lineStyle: { color: 'rgba(255,255,255,0.07)' } },
            axisLabel: {
              color:    '#ffb300',
              fontSize: 10,
              formatter: v => (v != null ? v.toFixed(1) : ''),
            },
          },
          {
            type:          'value',
            name:          '%',
            nameTextStyle: { color: '#66bb6a', fontSize: 10 },
            position:      'right',
            axisLine:      { show: false },
            axisTick:      { show: false },
            splitLine:     { show: false },
            axisLabel: {
              color:    '#66bb6a',
              fontSize: 10,
              formatter: v => (v != null ? v.toFixed(1) : ''),
            },
          },
        ],
        series: [
          {
            name:       'MAE (Wh)',
            type:       'bar',
            yAxisIndex: 0,
            data:       maeSeries,
            itemStyle:  { color: '#ffb300', opacity: 0.8 },
            barWidth:   '60%',
          },
          {
            name:       'RMSE (Wh)',
            type:       'line',
            yAxisIndex: 0,
            smooth:     true,
            showSymbol: false,
            data:       rmseSeries,
            lineStyle:  { color: '#ff7043', width: 2, type: 'dashed' },
            itemStyle:  { color: '#ff7043' },
          },
          {
            name:       'Bias (Wh)',
            type:       'line',
            yAxisIndex: 0,
            smooth:     true,
            showSymbol: false,
            data:       biasSeries,
            lineStyle:  { color: '#42a5f5', width: 2, type: 'dotted' },
            itemStyle:  { color: '#42a5f5' },
          },
          {
            name:       'MAPE (%)',
            type:       'line',
            yAxisIndex: 1,
            smooth:     true,
            showSymbol: false,
            data:       mapeSeries,
            lineStyle:  { color: '#66bb6a', width: 2 },
            itemStyle:  { color: '#66bb6a' },
          },
          {
            name:       'R²×100 (%)',
            type:       'line',
            yAxisIndex: 1,
            smooth:     true,
            showSymbol: false,
            data:       r2Series,
            lineStyle:  { color: '#ab47bc', width: 2, type: 'dashed' },
            itemStyle:  { color: '#ab47bc' },
          },
        ],
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

      // Dispose previous charts.
      if (this._g1Chart) { this._g1Chart.dispose(); this._g1Chart = null; }
      if (this._g2Chart) { this._g2Chart.dispose(); this._g2Chart = null; }
      if (this._g3Chart) { this._g3Chart.dispose(); this._g3Chart = null; }

      const sunriseStr = quality.sunrise_utc
        ? new Date(quality.sunrise_utc).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit', hour12: false})
        : '—';
      const sunsetStr = quality.sunset_utc
        ? new Date(quality.sunset_utc).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit', hour12: false})
        : '—';

      container.innerHTML = `
        <div class="accuracy-title">Forecast Quality</div>
        <div class="accuracy-subtitle">Sunrise ${sunriseStr} · Sunset ${sunsetStr} (local)</div>
        <div class="accuracy-subtitle">EMA α=0.1 running accuracy per bucket</div>
        <div id="g1-chart" class="accuracy-chart" style="height:300px"></div>
        <div id="g2-chart" class="accuracy-chart" style="height:300px"></div>
        <div id="g3-chart" class="accuracy-chart" style="height:300px"></div>
      `;

      // Group 1: intensity bins — sorted numerically by Wh.
      {
        const g1 = quality.group1 || {};
        const keys = Object.keys(g1).map(Number).sort((a, b) => a - b);
        const labels = keys.map(k => k + ' Wh');
        const metrics = keys.map(k => g1[String(k)]);
        const el = container.querySelector('#g1-chart');
        this._g1Chart = window.echarts.init(el, null, { renderer: 'canvas' });
        this._g1Chart.setOption(this._buildQualityChartOption(
          'Group 1 — Accuracy by Predicted Intensity (per forecast-kWh bin)',
          labels, metrics,
        ));
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
        this._g2Chart = window.echarts.init(el, null, { renderer: 'canvas' });
        this._g2Chart.setOption(this._buildQualityChartOption(
          'Group 2 — Accuracy by Solar-Day Position (Dawn → Dusk)',
          labels, metrics,
        ));
      }

      // Group 3: horizon buckets d0–d6.
      {
        const g3 = quality.group3 || {};
        const keys = Object.keys(g3).map(Number).sort((a, b) => a - b);
        const labels = keys.map(k => `d${k}`);
        const metrics = keys.map(k => g3[String(k)]);
        const el = container.querySelector('#g3-chart');
        this._g3Chart = window.echarts.init(el, null, { renderer: 'canvas' });
        this._g3Chart.setOption(this._buildQualityChartOption(
          'Group 3 — Accuracy by Forecast Horizon (d0 = same day, d6 = 6 days ahead)',
          labels, metrics,
        ));
      }
    }
  }

  customElements.define('sun-sale-panel', SunSalePanel);
})();
