/* Sun Sale Dashboard Panel
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
  const DASHBOARD_ENTITY  = 'sensor.sunsale_dashboard';
  const APEXCHARTS_CDN    = 'https://cdn.jsdelivr.net/npm/apexcharts@3.54.0/dist/apexcharts.min.js';

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
      this._initialized = false;
      this.attachShadow({ mode: 'open' });
    }

    set hass(hass) {
      this._hass = hass;
      if (!this._initialized) {
        this._initialized = true;
        this._boot();
      } else {
        this._renderBatteryRow(hass.states[DASHBOARD_ENTITY]?.attributes);
      }
    }

    disconnectedCallback() {
      if (this._chart) { this._chart.destroy(); this._chart = null; }
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
          #status {
            padding: 40px;
            text-align: center;
            color: var(--secondary-text-color, #888);
          }
          #chart { width: 100%; }
        </style>
        <div id="card">
          <h2>☀ Sun Sale</h2>
          <div id="subtitle">Buy &amp; Sell prices · Solar — 72 h window</div>
          <div id="battery"></div>
          <div id="profile"></div>
          <div id="status">Loading…</div>
          <div id="chart"></div>
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
        try {
          const t = new Date(slot.start).getTime();
          if (t > beforeMs) continue;
          if (typeof slot.buy_eur_kwh  === 'number') buy.push([t, slot.buy_eur_kwh]);
          if (typeof slot.sell_eur_kwh === 'number') sell.push([t, slot.sell_eur_kwh]);
        } catch { /* skip bad entries */ }
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
      for (const f of (dashAttrs.solar_frozen_forecast || [])) {
        if (f.t >= windowStart && f.t <= windowEnd)
          slots.set(f.t, f.forecast_kwh ?? f.forecast_w / 1000 * 0.25);
      }
      for (const s of (dashAttrs.slots || [])) {
        if (s.t >= windowStart && s.t <= windowEnd)
          slots.set(s.t, s.solar_forecast_kwh ?? s.solar_forecast_w / 1000 * 0.25);
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
        // -1 sentinel from the backend means observation history isn't yet
        // available — accuracy is pending, not zero. Skip painting these.
        if (err === -1 || obs === -1) continue;
        if (!isFinite(err) || Math.abs(err) < 1e-4) continue;
        out.push({
          x:            s.t,
          forecastKwh:  Number(s.forecast_kwh) || 0,
          errorKwh:     err,
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

      // Series: 0=buy(line) 1=sell(line) 2=solar forecast(bar).
      // All three use {x, y} object format. Mixing tuple-format line data
      // with object-format bar data on a datetime xaxis causes ApexCharts
      // to create the bar <g> group but emit zero <rect>s (bars invisible).
      const toXY = pts => pts.map(([x, y]) => ({ x, y }));
      const series = [
        { name: 'Buy price',      type: 'line', data: toXY(buyData)  },
        { name: 'Sell price',     type: 'line', data: toXY(sellData) },
        { name: 'Solar forecast', type: 'bar',  data: forecastBars   },
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
      const drawErrorOverlay = (chartContext) => {
        try {
          const w = chartContext?.w;
          if (!w || !errorSlots.length) return;
          const plotEl = chartContext.el?.querySelector('.apexcharts-graphical');
          if (!plotEl) return;
          // Wipe previous paint.
          plotEl.querySelectorAll('.' + OVL_GROUP).forEach(n => n.remove());

          // Solar-forecast bar is series index 2 → yaxis index 2 (single per-series).
          const yIdx  = 2;
          const xMin  = w.globals.minX;
          const xMax  = w.globals.maxX;
          const yMin  = w.globals.minYArr?.[yIdx] ?? w.globals.minY;
          const yMax  = w.globals.maxYArr?.[yIdx] ?? w.globals.maxY;
          const gw    = w.globals.gridWidth;
          const gh    = w.globals.gridHeight;
          if (!(xMax > xMin) || !(yMax > yMin) || !gw || !gh) return;
          const xPx   = t => ((t - xMin) / (xMax - xMin)) * gw;
          const yPx   = v => gh - ((v - yMin) / (yMax - yMin)) * gh;
          // Match the forecast bar width: columnWidth% of one slot.
          const slotW = (SLOT_MS / (xMax - xMin)) * gw;
          const colW  = slotW * 1.0; // mirrors plotOptions.bar.columnWidth: '100%'

          const g = document.createElementNS(SVG_NS, 'g');
          g.setAttribute('class', OVL_GROUP);
          for (const e of errorSlots) {
            const cx     = xPx(e.x + SLOT_MS / 2);  // bars are centred on slot midpoint
            const yTop   = yPx(Math.max(e.forecastKwh, e.forecastKwh + e.errorKwh));
            const yBot   = yPx(Math.min(e.forecastKwh, e.forecastKwh + e.errorKwh));
            const rect   = document.createElementNS(SVG_NS, 'rect');
            rect.setAttribute('x',      String(cx - colW / 2));
            rect.setAttribute('y',      String(yTop));
            rect.setAttribute('width',  String(colW));
            rect.setAttribute('height', String(Math.max(1, yBot - yTop)));
            rect.setAttribute('fill',   e.errorKwh > 0 ? ERR_POS : ERR_NEG);
            rect.setAttribute('fill-opacity', '0.85');
            g.appendChild(rect);
          }
          plotEl.appendChild(g);
        } catch (err) {
          console.warn('sunSale: error overlay paint failed', err);
        }
      };

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
          curve:     ['stepline', 'stepline', 'smooth'],
          width:     [2,          2,          0       ],
          dashArray: [0,          0,          0       ],
        },

        // 0:amber buy 1:coral sell 2:forecast (per-point fillColor; this is the fallback)
        colors: ['#ffb300', '#ff7043', GREY_BAR],

        fill: {
          type:    ['solid', 'solid', 'solid'],
          opacity: [1,       1,       1      ],
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
            forceNiceScale:  true,
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
            forceNiceScale:  true,
            decimalsInFloat: 3,
            labels: {
              style: { colors: '#cfcfcf', fontSize: '10px' },
              formatter: v => (v != null ? v.toFixed(3) : ''),
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
          ],
        },

        tooltip: {
          shared:    true,
          intersect: false,
          theme:     'dark',
          x: { format: 'dd MMM yyyy HH:mm' },
          y: {
            formatter: (val, { seriesIndex, dataPointIndex, w }) => {
              if (val == null) return null;
              if (seriesIndex < 2) return val.toFixed(4) + ' €/kWh';

              // Forecast bar — annotate with charging-profile disposition.
              const pt    = w.config.series[seriesIndex].data[dataPointIndex];
              const slotT = pt?.x;
              const prof  = slotT != null ? profileSlots.get(slotT) : null;
              let line = val.toFixed(3) + ' kWh forecast';
              if (prof) {
                if (prof.mode === 'solar_charge')      line += ' → charge battery';
                else if (prof.mode === 'sell')         line += ` → sell @ ${prof.sell_eur_kwh.toFixed(3)} €/kWh`;
                else if (prof.mode === 'no_export')    line += ' → curtail (sell ≤ 0)';
              }
              return line;
            },
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
    }
  }

  customElements.define('sun-sale-panel', SunSalePanel);
})();
