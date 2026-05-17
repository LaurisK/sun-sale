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
 *   Forecast-vs-observed error overlay is intentionally not rendered here:
 *   ApexCharts 3.x in a mixed line+bar chart does not support `chart.stacked`
 *   (lines disappear) nor `rangeBar` overlap (silently dropped), and plain
 *   bar+range-y renders side-by-side rather than overlaid. The backend keeps
 *   exposing `forecast_error_slots` so the overlay can be added once an
 *   overlay-capable rendering approach is in place.
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

      // Series: 0=buy(line) 1=sell(line) 2=solar forecast(bar)
      const series = [
        { name: 'Buy price',      type: 'line', data: buyData      },
        { name: 'Sell price',     type: 'line', data: sellData     },
        { name: 'Solar forecast', type: 'bar',  data: forecastBars },
      ];

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
            columnWidth: '90%',
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

        // yaxis[n] matches series[n]; shared axes use seriesName + show: false.
        // Series 1 (Sell price) shares the price axis (series 0).
        yaxis: [
          {
            seriesName: 'Buy price',
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
          { seriesName: 'Buy price', show: false },
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

      console.groupCollapsed('sunSale: render inputs');
      console.log('buyData.length =',  buyData.length,  buyData[0],  buyData[buyData.length - 1]);
      console.log('sellData.length =', sellData.length, sellData[0], sellData[sellData.length - 1]);
      console.log('forecastBars.length =', forecastBars.length, forecastBars[0], forecastBars[forecastBars.length - 1]);
      console.log('profileAnnotations.length =', profileAnnotations.length, profileAnnotations[0]);
      console.log('window =', new Date(windowStart).toISOString(), '→', new Date(windowEnd).toISOString());
      console.log('options =', options);
      console.groupEnd();

      try {
        this._chart = new ApexCharts(el, options);
      } catch (e) {
        console.error('sunSale: ApexCharts constructor threw', e);
        this._setStatus('⚠ chart constructor failed: ' + e.message);
        return;
      }
      try {
        await this._chart.render();
        console.log('sunSale: ApexCharts.render() resolved');
      } catch (e) {
        console.error('sunSale: ApexCharts.render() threw', e);
        this._setStatus('⚠ chart render failed: ' + e.message);
      }
    }
  }

  if (!customElements.get('sun-sale-panel')) {
    customElements.define('sun-sale-panel', SunSalePanel);
  } else {
    console.warn('sunSale: <sun-sale-panel> already registered — this script load is a duplicate, the older copy is what actually runs. Clear the Service Worker cache to pick up the new file.');
  }
})();
