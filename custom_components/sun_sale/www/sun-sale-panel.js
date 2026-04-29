/* Sun Sale Dashboard Panel
 *
 * Custom HA sidebar panel. Renders a 3-day ApexCharts overview:
 *   - Nordpool buy/sell prices (step-line, thick when active grid op)
 *   - Solar: forecast (dashed) vs actual (solid), green/red fill-between mismatch
 *   - Battery SoC (line, right axis)
 *   - Inverter mode band (colour-coded strip at bottom)
 *   - "Now" vertical annotation dividing past (actual) from future (planned)
 *
 * Past data comes from HA history API; future data from sensor.sun_sale_dashboard.
 */

(function () {
  'use strict';

  // ── Constants ──────────────────────────────────────────────────────────────

  const DASHBOARD_ENTITY = 'sensor.sun_sale_dashboard';
  const APEXCHARTS_CDN = 'https://cdn.jsdelivr.net/npm/apexcharts@3.54.0/dist/apexcharts.min.js';

  const HISTORY_ENTITIES = [
    'sensor.nordpool_kwh_lt_eur_3_10_0',  // 0 price
    'sensor.namai_inv_total_pv_power_2',  // 1 solar W
    'sensor.namai_inv_battery_soc_2',     // 2 SoC %
    'sensor.namai_inv_grid_power_net',    // 3 grid W
    'sensor.sunsale_inverter_mode',       // 4 mode string (our own sensor)
  ];
  const MODE_HISTORY_ENTITY = 'sensor.sunsale_inverter_mode';

  const SLOT_MS = 15 * 60 * 1000;

  // mode string → label + colour
  const MODE_META = {
    charge_from_grid: { label: 'Charge (grid)',    color: '#2196f3' },
    charge_solar:     { label: 'Charge (solar)',   color: '#00bcd4' },
    sell_discharge:   { label: 'Sell + discharge', color: '#ff9800' },
    self_use_sell:    { label: 'Self-use + sell',  color: '#8bc34a' },
    self_use:         { label: 'Self-use',         color: '#607d8b' },
    idle:             { label: 'Idle',             color: '#37474f' },
  };

  // ── ApexCharts loader ──────────────────────────────────────────────────────

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

  // ── Utility: resample sparse state-change history to fixed 15-min buckets ──

  function resample15min(points, startMs, endMs) {
    // points: [{t, v}] sorted ascending, v numeric
    // returns: [[t_ms, v], ...] one per 15-min slot, forward-filled
    const out = [];
    let cursor = 0;
    let last = null;
    for (let t = startMs; t <= endMs; t += SLOT_MS) {
      while (cursor < points.length && points[cursor].t <= t) {
        last = points[cursor].v;
        cursor++;
      }
      if (last !== null) out.push([t, last]);
    }
    return out;
  }

  // ── Utility: floor timestamp to nearest 15-min boundary ───────────────────

  function floor15(ms) {
    return ms - (ms % SLOT_MS);
  }

  // ── Utility: group consecutive same-value items into runs ─────────────────

  function groupRuns(items, valueOf, timestampOf) {
    const runs = [];
    let cur = null;
    for (const item of items) {
      const v = valueOf(item);
      const t = timestampOf(item);
      if (!cur || cur.value !== v) {
        if (cur) cur.end = t;
        cur = { start: t, end: t + SLOT_MS, value: v };
        runs.push(cur);
      } else {
        cur.end = t + SLOT_MS;
      }
    }
    return runs;
  }

  function gridPowerToMode(gridW) {
    if (gridW > 200)  return 'charge_from_grid';
    if (gridW < -200) return 'sell_discharge';
    return 'self_use';
  }

  // ── Utility: midnight UTC for a date offset ────────────────────────────────

  function dayBoundaryMs(offsetDays) {
    const d = new Date();
    d.setUTCHours(0, 0, 0, 0);
    d.setUTCDate(d.getUTCDate() + offsetDays);
    return d.getTime();
  }

  // ── Main panel element ─────────────────────────────────────────────────────

  class SunSalePanel extends HTMLElement {
    constructor() {
      super();
      this._hass = null;
      this._chart = null;
      this._dashboardAttrs = null;
      this._history = {};
      this._stateUnsub = null;
      this._initialized = false;
      this.attachShadow({ mode: 'open' });
    }

    // HA sets this every time any entity updates
    set hass(hass) {
      this._hass = hass;
      if (!this._initialized) {
        this._initialized = true;
        this._boot();
      }
    }

    disconnectedCallback() {
      if (this._stateUnsub) { this._stateUnsub(); this._stateUnsub = null; }
      if (this._chart) { this._chart.destroy(); this._chart = null; }
    }

    // ── Boot sequence ──────────────────────────────────────────────────────

    async _boot() {
      this._buildShell();
      try {
        await loadApexCharts();
      } catch (e) {
        this._setStatus('⚠ ' + e.message + ' — install ApexCharts card via HACS or check your network.');
        return;
      }
      await this._fetchHistory();
      this._subscribeSensor();
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
          #header {
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            flex-wrap: wrap;
            gap: 8px;
            margin-bottom: 16px;
          }
          h2 {
            margin: 0;
            font-size: 1.25rem;
            color: var(--primary-text-color, #fff);
          }
          #kpis {
            display: flex;
            gap: 20px;
            flex-wrap: wrap;
          }
          .kpi {
            text-align: right;
          }
          .kpi-label {
            font-size: 0.7rem;
            color: var(--secondary-text-color, #888);
            text-transform: uppercase;
            letter-spacing: 0.05em;
          }
          .kpi-value {
            font-size: 1rem;
            font-weight: 600;
            color: var(--primary-text-color, #fff);
          }
          #legend {
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
            margin-bottom: 12px;
          }
          .leg {
            display: flex;
            align-items: center;
            gap: 4px;
            font-size: 0.75rem;
            color: var(--secondary-text-color, #999);
          }
          .leg-dot {
            width: 10px; height: 10px;
            border-radius: 2px;
            flex-shrink: 0;
          }
          #status {
            padding: 40px;
            text-align: center;
            color: var(--secondary-text-color, #888);
          }
          #chart { width: 100%; }
        </style>
        <div id="card">
          <div id="header">
            <h2>☀ Sun Sale</h2>
            <div id="kpis"></div>
          </div>
          <div id="legend"></div>
          <div id="status">Loading…</div>
          <div id="chart"></div>
        </div>
      `;
    }

    _setStatus(msg) {
      this.shadowRoot.querySelector('#status').textContent = msg;
    }

    _clearStatus() {
      this.shadowRoot.querySelector('#status').textContent = '';
    }

    // ── HA history fetch ───────────────────────────────────────────────────

    async _fetchHistory() {
      const startMs = dayBoundaryMs(-1);           // yesterday 00:00 UTC
      const endMs   = Date.now();
      const start   = new Date(startMs).toISOString();
      const end     = new Date(endMs).toISOString();

      const url = `/api/history/period/${start}?end_time=${end}`
        + `&filter_entity_id=${HISTORY_ENTITIES.join(',')}`
        + `&minimal_response=true&no_attributes=true`;

      try {
        const resp = await this._hass.fetchWithAuth(url);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const raw = await resp.json();
        this._history = this._indexHistory(raw);
      } catch (e) {
        console.warn('sunSale: history fetch failed', e);
      }
    }

    _indexHistory(rawArrays) {
      const out = {};
      for (const entityHistory of rawArrays) {
        if (!entityHistory?.length) continue;
        const eid = entityHistory[0].entity_id;
        const filtered = entityHistory.filter(
          s => s.state !== 'unavailable' && s.state !== 'unknown' && s.state !== 'None'
        );
        if (eid === MODE_HISTORY_ENTITY) {
          // String states — keep as-is
          out[eid] = filtered.map(s => ({ t: new Date(s.last_changed).getTime(), v: s.state }));
        } else {
          out[eid] = filtered
            .map(s => ({ t: new Date(s.last_changed).getTime(), v: parseFloat(s.state) }))
            .filter(p => isFinite(p.v));
        }
      }
      return out;
    }

    // ── Sensor subscription ────────────────────────────────────────────────

    _subscribeSensor() {
      // Read current state immediately
      const state = this._hass.states[DASHBOARD_ENTITY];
      if (state?.attributes) {
        this._dashboardAttrs = state.attributes;
      }
      this._render();

      // Subscribe to future state_changed events
      this._hass.connection.subscribeEvents((ev) => {
        if (ev.data?.entity_id !== DASHBOARD_ENTITY) return;
        const attrs = ev.data?.new_state?.attributes;
        if (attrs) {
          this._dashboardAttrs = attrs;
          this._refreshChart();
        }
      }, 'state_changed').then(unsub => { this._stateUnsub = unsub; });
    }

    // ── Data assembly ──────────────────────────────────────────────────────

    _buildSeries() {
      const now = Date.now();
      const windowStart = dayBoundaryMs(-1);
      const windowEnd   = dayBoundaryMs(1) + 23 * 3600 * 1000 + 45 * 60 * 1000;

      const hist = this._history;
      const attrs = this._dashboardAttrs || {};
      const futureSlots = attrs.slots || [];
      const frozenForecast = attrs.solar_frozen_forecast || [];

      // ── PAST DATA (yesterday 00:00 → now) ─────────────────────────────

      // Price
      const nordpoolPts = hist[HISTORY_ENTITIES[0]] || [];
      const pricePast = resample15min(nordpoolPts, windowStart, now - SLOT_MS);

      // Determine slots where grid was actually active (|grid_power| > 50 W)
      const gridPts = hist[HISTORY_ENTITIES[3]] || [];
      const gridPast = resample15min(gridPts, windowStart, now - SLOT_MS);
      const gridActiveSet = new Set(
        gridPast.filter(([, v]) => Math.abs(v) > 50).map(([t]) => floor15(t))
      );

      const priceActivePast = pricePast
        .map(([t, v]) => [t, gridActiveSet.has(floor15(t)) ? v : null]);

      // Solar actual (W → kW)
      const pvPts = hist[HISTORY_ENTITIES[1]] || [];
      const pvActualPast = resample15min(pvPts, windowStart, now - SLOT_MS)
        .map(([t, w]) => [t, w / 1000]);

      // Solar frozen forecast for today (for mismatch colouring)
      const forecastBySlot = {};
      for (const { t, forecast_w } of frozenForecast) {
        forecastBySlot[floor15(t)] = forecast_w / 1000;
      }

      const pvAboveForecast = [];   // actual > forecast → green
      const pvBelowForecast = [];   // actual < forecast → red
      const pvForecastLine  = [];

      for (const [t, actualKw] of pvActualPast) {
        const slotKey = floor15(t);
        const fcKw = forecastBySlot[slotKey] ?? null;
        if (fcKw !== null) {
          pvForecastLine.push([t, fcKw]);
          if (actualKw >= fcKw) {
            pvAboveForecast.push([t, actualKw]);
            pvBelowForecast.push([t, null]);
          } else {
            pvAboveForecast.push([t, null]);
            pvBelowForecast.push([t, actualKw]);
          }
        } else {
          pvAboveForecast.push([t, actualKw]);
          pvBelowForecast.push([t, null]);
        }
      }

      // Battery SoC past
      const socPts = hist[HISTORY_ENTITIES[2]] || [];
      const socPast = resample15min(socPts, windowStart, now - SLOT_MS);

      // ── FUTURE DATA (now → end of tomorrow) ───────────────────────────

      const priceBuyFuture  = futureSlots.map(s => [s.t, s.buy_price]);
      const priceSellFuture = futureSlots.map(s => [s.t, s.sell_price]);
      const pvForecastFuture = futureSlots.map(s => [s.t, s.solar_forecast_w / 1000]);
      const socFuture = futureSlots.map(s => [s.t, s.battery_soc_pct]);

      // Active future slots (where grid operation is buy or sell)
      const priceActiveFuture = futureSlots
        .map(s => [s.t, s.grid_operation != null ? s.buy_price : null]);

      // Mode runs — past from dedicated mode sensor when available, else grid-power approximation
      const modePts = hist[MODE_HISTORY_ENTITY] || [];
      let pastModeRuns;
      if (modePts.length > 0) {
        // Forward-fill mode string across 15-min slots
        const modePast = resample15min(modePts, windowStart, now - SLOT_MS);
        pastModeRuns = groupRuns(modePast, ([, v]) => v, ([t]) => t);
      } else {
        pastModeRuns = groupRuns(gridPast, ([, v]) => gridPowerToMode(v), ([t]) => t);
      }
      const futureModeRuns = groupRuns(futureSlots, s => s.inverter_mode, s => s.t);
      this._modeRuns = [...pastModeRuns, ...futureModeRuns];
      this._windowStart = windowStart;
      this._windowEnd   = windowEnd;

      return {
        pricePast, priceActivePast,
        priceBuyFuture, priceSellFuture, priceActiveFuture,
        pvAboveForecast, pvBelowForecast, pvForecastLine,
        pvForecastFuture,
        socPast, socFuture,
        windowStart, windowEnd, now,
      };
    }

    // ── Chart render ───────────────────────────────────────────────────────

    _render() {
      if (!window.ApexCharts) return;
      this._clearStatus();

      if (!this._dashboardAttrs && Object.keys(this._history).length === 0) {
        this._setStatus('No data yet — waiting for coordinator update.');
        return;
      }

      this._updateLegend();
      this._updateKPIs();

      const s = this._buildSeries();
      const options = this._buildOptions(s);
      const el = this.shadowRoot.querySelector('#chart');

      if (this._chart) {
        this._chart.destroy();
        this._chart = null;
      }
      this._chart = new ApexCharts(el, options);
      this._chart.render();
    }

    _refreshChart() {
      if (!this._chart) { this._render(); return; }
      this._updateKPIs();
      const s = this._buildSeries();
      this._chart.updateSeries(this._seriesArray(s), false);
      this._chart.clearAnnotations();
      this._chart.addXaxisAnnotation({
        x: s.now,
        borderColor: 'rgba(255,255,255,0.4)',
        strokeDashArray: 4,
        label: { text: 'Now', style: { color: '#fff', background: '#444', fontSize: '11px' } },
      });
      for (const ann of this._modeAnnotations()) {
        this._chart.addXaxisAnnotation(ann);
      }
    }

    _modeAnnotations() {
      return (this._modeRuns || []).map(r => ({
        x: r.start,
        x2: r.end,
        fillColor: MODE_META[r.value]?.color || '#607d8b',
        opacity: 0.13,
        borderWidth: 0,
        label: { text: '' },
      }));
    }

    _seriesArray(s) {
      return [
        { name: 'Price past',         data: s.pricePast },
        { name: 'Active past',        data: s.priceActivePast },
        { name: 'Buy price (future)', data: s.priceBuyFuture },
        { name: 'Sell price (future)',data: s.priceSellFuture },
        { name: 'Active future',      data: s.priceActiveFuture },
        { name: 'Solar above FC',     data: s.pvAboveForecast },
        { name: 'Solar below FC',     data: s.pvBelowForecast },
        { name: 'Solar FC line',      data: s.pvForecastLine },
        { name: 'Solar FC future',    data: s.pvForecastFuture },
        { name: 'SoC past',           data: s.socPast },
        { name: 'SoC future',         data: s.socFuture },
      ];
    }

    _buildOptions(s) {
      const now = s.now;

      return {
        series: this._seriesArray(s),

        chart: {
          type: 'line',
          height: 460,
          background: 'transparent',
          toolbar: { show: true, tools: { zoom: true, zoomin: true, zoomout: true, pan: true, reset: true, download: false } },
          zoom: { enabled: true, type: 'x' },
          animations: { enabled: false },
          fontFamily: 'inherit',
          events: {
            beforeResetZoom: () => ({
              xaxis: { min: this._windowStart, max: this._windowEnd },
            }),
          },
        },

        theme: { mode: 'dark' },

        // Per-series overrides
        stroke: {
          show: true,
          curve: [
            'stepline',  // price past
            'stepline',  // active past
            'stepline',  // buy future
            'stepline',  // sell future
            'stepline',  // active future
            'smooth',    // solar above
            'smooth',    // solar below
            'smooth',    // solar FC line
            'smooth',    // solar FC future
            'smooth',    // SoC past
            'smooth',    // SoC future
          ],
          width: [1, 3, 1, 1, 3, 1.5, 1.5, 1, 1.5, 2, 2],
          dashArray: [0, 0, 4, 4, 0, 0, 0, 3, 3, 0, 4],
        },

        colors: [
          '#ffb300',   // price past
          '#ff5722',   // active past (thick)
          '#ffcc02',   // buy future
          '#69f0ae',   // sell future
          '#ff5722',   // active future (thick)
          '#4caf50',   // solar above forecast (green)
          '#f44336',   // solar below forecast (red)
          '#ffe082',   // solar FC reference line
          '#ffe08288', // solar FC future (translucent)
          '#42a5f5',   // SoC past
          '#42a5f5',   // SoC future
        ],

        fill: {
          type: ['solid', 'solid', 'solid', 'solid', 'solid', 'gradient', 'gradient', 'solid', 'solid', 'solid', 'solid'],
          gradient: {
            type: 'vertical',
            shadeIntensity: 0,
            opacityFrom: 0.45,
            opacityTo: 0.05,
            stops: [0, 100],
          },
        },

        xaxis: {
          type: 'datetime',
          min: s.windowStart,
          max: s.windowEnd,
          labels: {
            datetimeUTC: false,
            format: 'dd MMM HH:mm',
            style: { colors: '#aaa', fontSize: '10px' },
          },
          axisBorder: { show: false },
          axisTicks: { show: false },
        },

        yaxis: [
          // Y0: price (left)
          {
            seriesName: 'Price past',
            title: { text: 'EUR/kWh', style: { color: '#ffb300', fontSize: '11px' } },
            min: 0,
            forceNiceScale: true,
            decimalsInFloat: 3,
            labels: { style: { colors: '#ffb300', fontSize: '10px' } },
          },
          // Y1-4: price series share same axis (hidden)
          { show: false, seriesName: 'Price past' },
          { show: false, seriesName: 'Price past' },
          { show: false, seriesName: 'Price past' },
          { show: false, seriesName: 'Price past' },
          // Y5-8: solar (right)
          {
            seriesName: 'Solar above FC',
            opposite: true,
            title: { text: 'kW', style: { color: '#4caf50', fontSize: '11px' } },
            min: 0,
            forceNiceScale: true,
            decimalsInFloat: 1,
            labels: { style: { colors: '#4caf50', fontSize: '10px' } },
          },
          { show: false, seriesName: 'Solar above FC' },
          { show: false, seriesName: 'Solar above FC' },
          { show: false, seriesName: 'Solar above FC' },
          // Y9-10: SoC (right, offset below solar)
          {
            seriesName: 'SoC past',
            opposite: true,
            title: { text: 'SoC %', style: { color: '#42a5f5', fontSize: '11px' } },
            min: 0,
            max: 100,
            decimalsInFloat: 0,
            labels: { style: { colors: '#42a5f5', fontSize: '10px' } },
          },
          { show: false, seriesName: 'SoC past' },
        ],

        annotations: {
          xaxis: [
            {
              x: now,
              borderColor: 'rgba(255,255,255,0.35)',
              strokeDashArray: 5,
              label: {
                text: 'Now',
                position: 'top',
                style: { color: '#fff', background: '#333', fontSize: '11px', padding: { top: 2, bottom: 2, left: 6, right: 6 } },
              },
            },
            ...this._modeAnnotations(),
          ],
        },

        tooltip: {
          shared: true,
          intersect: false,
          theme: 'dark',
          x: { format: 'dd MMM yyyy HH:mm' },
          y: {
            formatter: (val, { seriesIndex }) => {
              if (val == null) return null;
              if (seriesIndex <= 4)  return val.toFixed(4) + ' €/kWh';
              if (seriesIndex <= 8)  return val.toFixed(2) + ' kW';
              return val.toFixed(1) + ' %';
            },
          },
        },

        legend: { show: false }, // we draw our own

        grid: {
          borderColor: 'rgba(255,255,255,0.06)',
          xaxis: { lines: { show: true } },
          yaxis: { lines: { show: true } },
        },

        markers: { size: 0 },
        dataLabels: { enabled: false },
      };
    }

    // ── KPI bar ────────────────────────────────────────────────────────────

    _updateKPIs() {
      const attrs = this._dashboardAttrs || {};
      const slots = attrs.slots || [];
      const cap = attrs.battery_capacity_kwh || '—';

      const now = Date.now();
      const curSlot = slots.find(s => s.t <= now && s.t + SLOT_MS > now) || slots[0] || null;
      const buyP  = curSlot ? curSlot.buy_price.toFixed(4) : '—';
      const sellP = curSlot ? curSlot.sell_price.toFixed(4) : '—';
      const socP  = curSlot ? curSlot.battery_soc_pct.toFixed(1) + ' %' : '—';
      const mode  = curSlot ? (MODE_META[curSlot.inverter_mode]?.label || curSlot.inverter_mode) : '—';

      this.shadowRoot.querySelector('#kpis').innerHTML = `
        <div class="kpi"><div class="kpi-label">Buy</div><div class="kpi-value">${buyP} €</div></div>
        <div class="kpi"><div class="kpi-label">Sell</div><div class="kpi-value">${sellP} €</div></div>
        <div class="kpi"><div class="kpi-label">SoC</div><div class="kpi-value">${socP}</div></div>
        <div class="kpi"><div class="kpi-label">Mode</div><div class="kpi-value">${mode}</div></div>
        <div class="kpi"><div class="kpi-label">Batt</div><div class="kpi-value">${cap} kWh</div></div>
      `;
    }

    // ── Legend ─────────────────────────────────────────────────────────────

    _updateLegend() {
      const lines = [
        { color: '#ffb300', label: 'Price (past)' },
        { color: '#ff5722', label: 'Price (active grid op)', thick: true },
        { color: '#ffcc02', label: 'Buy price (future)', dashed: true },
        { color: '#69f0ae', label: 'Sell price (future)', dashed: true },
        { color: '#4caf50', label: 'Solar actual > forecast' },
        { color: '#f44336', label: 'Solar actual < forecast' },
        { color: '#ffe082', label: 'Solar forecast', dashed: true },
        { color: '#42a5f5', label: 'Battery SoC' },
      ];
      const modes = Object.entries(MODE_META).map(([, m]) => ({ color: m.color, label: m.label, band: true }));
      const items = [...lines, ...modes];
      this.shadowRoot.querySelector('#legend').innerHTML = items.map(i => `
        <div class="leg">
          <div class="leg-dot" style="background:${i.color};opacity:${i.dashed ? 0.6 : 1};${i.band ? 'border-radius:2px;height:8px;' : ''}"></div>
          <span>${i.label}</span>
        </div>
      `).join('');
    }
  }

  customElements.define('sun-sale-panel', SunSalePanel);
})();
