/* Sun Sale Dashboard Panel
 *
 * 72-hour window: yesterday 00:00 → tomorrow 23:59 (local)
 *
 * Left Y axis — EUR/kWh:
 *   Buy price   amber  solid stepline  — past history + future from pricing sensor (one continuous line)
 *   Sell price  coral  solid stepline  — past history + future from pricing sensor (one continuous line)
 *
 * Right Y axis — kW:
 *   Solar actual      green  solid area  (inverter history)
 *   Solar forecast    teal   dashed line (sensor.sun_sale_dashboard frozen + slots)
 *
 * Overlays:
 *   Red shaded bands  negative-sell lockout windows (sensor.sun_sale_calculation)
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
  const CALC_ENTITY       = 'sensor.sunsale_calculation';
  const SOLAR_ENTITY      = 'sensor.namai_inv_total_pv_power_2';
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
          <div id="status">Loading…</div>
          <div id="chart"></div>
        </div>
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
      const entities = [BUY_PRICE_ENTITY, SELL_PRICE_ENTITY, SOLAR_ENTITY].join(',');
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
          solar:     indexed[SOLAR_ENTITY]       || [],
        };
      } catch (e) {
        console.warn('sunSale: history fetch failed', e);
        return { buyPrice: [], sellPrice: [], solar: [] };
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

    // ── Data: negative-sell lockout windows from calculation sensor ───────────

    _readLockoutWindows() {
      const attrs = this._hass.states[CALC_ENTITY]?.attributes;
      if (!Array.isArray(attrs?.feed_in_lockout_windows)) return [];
      return attrs.feed_in_lockout_windows
        .map(w => {
          const x  = new Date(w.start).getTime();
          const x2 = new Date(w.end).getTime();
          return isFinite(x) && isFinite(x2) ? { x, x2 } : null;
        })
        .filter(Boolean);
    }

    // ── Data: solar forecast from dashboard sensor ────────────────────────────

    _readSolarForecast(now, windowStart, windowEnd) {
      const attrs = this._hass.states[DASHBOARD_ENTITY]?.attributes;
      if (!attrs) return [];

      const frozen = (attrs.solar_frozen_forecast || [])
        .filter(f => f.t >= windowStart && f.t <= now)
        .map(f => [f.t, f.forecast_w / 1000]);

      const future = (attrs.slots || [])
        .filter(s => s.t > now && s.t <= windowEnd)
        .map(s => [s.t, s.solar_forecast_w / 1000]);

      return [...frozen, ...future].sort((a, b) => a[0] - b[0]);
    }

    // ── Render ─────────────────────────────────────────────────────────────────

    async _render() {
      const now         = Date.now();
      const windowStart = localMidnight(-1);         // yesterday 00:00 local
      const windowEnd   = localMidnight(2) - 60_000; // tomorrow 23:59 local

      const history       = await this._fetchHistory(windowStart);
      const pricingSlots  = this._readPricingSlots(windowEnd);
      const lockouts      = this._readLockoutWindows();
      const solarForecast = this._readSolarForecast(now, windowStart, windowEnd);

      // Merge past (history) + future/present (pricing sensor) into one series each.
      // History covers yesterday→now; pricing sensor covers its known window (typically 24–36 h ahead).
      // We use a Set to avoid duplicating timestamps when the ranges overlap.
      const _merge = (histPoints, pricingPoints, windowStartMs) => {
        const histFiltered = histPoints.filter(([t]) => t >= windowStartMs);
        const pricingTimes = new Set(pricingPoints.map(([t]) => t));
        // Drop history points that land within 30 min of a pricing point (pricing is authoritative)
        const histDeduped  = histFiltered.filter(([t]) =>
          !pricingPoints.some(([pt]) => Math.abs(pt - t) < 30 * 60 * 1000)
        );
        return [...histDeduped, ...pricingPoints].sort((a, b) => a[0] - b[0]);
      };

      const buyData  = _merge(history.buyPrice,  pricingSlots.buy,  windowStart);
      const sellData = _merge(history.sellPrice, pricingSlots.sell, windowStart);
      const solarPast = history.solar
        .filter(([t]) => t >= windowStart && t <= now)
        .map(([t, w]) => [t, w / 1000]);

      if (!buyData.length && !sellData.length) {
        this._setStatus('No price data — waiting for sunSale coordinator to run.');
        return;
      }

      this._clearStatus();
      if (this._chart) { this._chart.destroy(); this._chart = null; }

      // Red shaded bands for negative-sell windows
      const lockoutAnnotations = lockouts.map(w => ({
        x:           w.x,
        x2:          w.x2,
        fillColor:   'rgba(229, 57, 53, 0.14)',
        borderColor: 'rgba(229, 57, 53, 0.40)',
        label: {
          text: '⛔ sell off',
          position: 'top',
          offsetY: 6,
          style: {
            color:      '#ef9a9a',
            background: 'transparent',
            fontSize:   '10px',
            padding: { top: 2, bottom: 2, left: 4, right: 4 },
          },
        },
      }));

      // Series order must match yaxis[] and stroke/fill/colors arrays below
      const series = [
        { name: 'Buy price',      data: buyData       }, // 0 price axis amber
        { name: 'Sell price',     data: sellData      }, // 1 price axis coral
        { name: 'Solar actual',   data: solarPast     }, // 2 solar axis green
        { name: 'Solar forecast', data: solarForecast }, // 3 solar axis teal
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

        stroke: {
          show:      true,
          curve:     ['stepline', 'stepline', 'smooth', 'smooth'],
          width:     [2,          2,          2,        1.5],
          dashArray: [0,          0,          0,        5],
        },

        // 0:amber  1:coral  2:green  3:teal
        colors: ['#ffb300', '#ff7043', '#4caf50', '#80cbc4'],

        fill: {
          type: ['solid', 'solid', 'gradient', 'solid'],
          gradient: {
            // only series 2 (Solar actual) uses gradient fill
            type:           'vertical',
            shadeIntensity: 0,
            opacityFrom:    0.35,
            opacityTo:      0.02,
            stops:          [0, 100],
          },
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

        // Four yaxis entries — one per series (ApexCharts requirement).
        // Series 1 (Sell price) shares the price axis (series 0).
        // Series 3 (Solar forecast) shares the solar axis (series 2).
        yaxis: [
          {
            // 0 — Buy price → price axis (left)
            seriesName: 'Buy price',
            title: {
              text:  'EUR / kWh',
              style: { color: '#ffb300', fontSize: '11px' },
            },
            forceNiceScale: true,
            decimalsInFloat: 3,
            labels: {
              style: { colors: '#ffb300', fontSize: '10px' },
              formatter: v => (v != null ? v.toFixed(3) : ''),
            },
          },
          { seriesName: 'Buy price', show: false }, // 1 sell price — shares price axis
          {
            // 2 — Solar actual → solar axis (right)
            seriesName: 'Solar actual',
            opposite:   true,
            title: {
              text:  'kW',
              style: { color: '#4caf50', fontSize: '11px' },
            },
            min: 0,
            forceNiceScale:  true,
            decimalsInFloat: 1,
            labels: {
              style: { colors: '#4caf50', fontSize: '10px' },
              formatter: v => (v != null ? v.toFixed(1) : ''),
            },
          },
          { seriesName: 'Solar actual', opposite: true, show: false }, // 3 solar forecast — shares solar axis
        ],

        annotations: {
          // Horizontal reference at zero so negative sell prices are obvious
          yaxis: [{
            y:               0,
            borderColor:     'rgba(255,255,255,0.20)',
            strokeDashArray: 3,
          }],
          xaxis: [
            // "Now" divider
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
            // Negative-sell lockout bands
            ...lockoutAnnotations,
          ],
        },

        tooltip: {
          shared:    true,
          intersect: false,
          theme:     'dark',
          x: { format: 'dd MMM yyyy HH:mm' },
          y: {
            formatter: (val, { seriesIndex }) => {
              if (val == null) return null;
              return seriesIndex < 2
                ? val.toFixed(4) + ' €/kWh'
                : val.toFixed(2) + ' kW';
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
      this._chart.render();
    }
  }

  customElements.define('sun-sale-panel', SunSalePanel);
})();
