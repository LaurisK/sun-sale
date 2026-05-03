/* Sun Sale Dashboard Panel
 *
 * 3-day window: yesterday 00:00 → tomorrow 23:59 (local time)
 * Series:
 *   Left Y  — Nordpool spot price: past (amber step) / future (blue step, dashed)
 *   Right Y — Solar actual (green solid area) / Solar forecast (green dashed)
 * Vertical "Now" annotation splits past from future.
 *
 * Data sources:
 *   Past prices + solar actual  → HA history API
 *   Future prices               → sensor.nordpool_kwh_lt_eur_3_10_0 (raw_today / raw_tomorrow)
 *   Solar forecast (today past) → sensor.sun_sale_dashboard  (solar_frozen_forecast)
 *   Solar forecast (future)     → sensor.sun_sale_dashboard  (slots[].solar_forecast_w)
 */

(function () {
  'use strict';

  const NORDPOOL_ENTITY  = 'sensor.nordpool_kwh_lt_eur_3_10_0';
  const SOLAR_ENTITY     = 'sensor.namai_inv_total_pv_power_2';
  const DASHBOARD_ENTITY = 'sensor.sun_sale_dashboard';
  const APEXCHARTS_CDN   = 'https://cdn.jsdelivr.net/npm/apexcharts@3.54.0/dist/apexcharts.min.js';

  // ── Window helpers ─────────────────────────────────────────────────────────

  function localMidnight(offsetDays) {
    const d = new Date();
    d.setHours(0, 0, 0, 0);
    d.setDate(d.getDate() + offsetDays);
    return d.getTime();
  }

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

    // ── Boot ─────────────────────────────────────────────────────────────────

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
            margin: 0 0 16px;
            font-size: 1.25rem;
            color: var(--primary-text-color, #fff);
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

    // ── Data: history API (prices + solar actual) ─────────────────────────────

    async _fetchHistory(windowStartMs) {
      const start   = new Date(windowStartMs).toISOString();
      const end     = new Date().toISOString();
      const entities = [NORDPOOL_ENTITY, SOLAR_ENTITY].join(',');
      const url = `/api/history/period/${start}?end_time=${end}`
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
          nordpool: indexed[NORDPOOL_ENTITY] || [],
          solar:    indexed[SOLAR_ENTITY]    || [],
        };
      } catch (e) {
        console.warn('sunSale: history fetch failed', e);
        return { nordpool: [], solar: [] };
      }
    }

    // ── Data: future Nordpool prices from sensor attributes ───────────────────

    _readNordpoolFuture(after, before) {
      const state = this._hass.states[NORDPOOL_ENTITY];
      if (!state?.attributes) return [];
      const points = [];
      for (const attr of ['raw_today', 'raw_tomorrow']) {
        const raw = state.attributes[attr];
        if (!Array.isArray(raw)) continue;
        for (const entry of raw) {
          try {
            const t = new Date(entry.start).getTime();
            const v = parseFloat(entry.value);
            if (isFinite(v) && t > after && t <= before) points.push([t, v]);
          } catch { /* skip */ }
        }
      }
      return points.sort((a, b) => a[0] - b[0]);
    }

    // ── Data: solar forecast from dashboard sensor ────────────────────────────

    _readSolarForecast(now, windowStart, windowEnd) {
      const attrs = this._hass.states[DASHBOARD_ENTITY]?.attributes;
      if (!attrs) return [];

      // Frozen today forecast covers the past portion of today
      const frozen = (attrs.solar_frozen_forecast || [])
        .filter(f => f.t >= windowStart && f.t <= now)
        .map(f => [f.t, f.forecast_w / 1000]);

      // Future slots cover now → tomorrow 23:59
      const future = (attrs.slots || [])
        .filter(s => s.t > now && s.t <= windowEnd)
        .map(s => [s.t, s.solar_forecast_w / 1000]);

      return [...frozen, ...future].sort((a, b) => a[0] - b[0]);
    }

    // ── Chart render ──────────────────────────────────────────────────────────

    async _render() {
      const now         = Date.now();
      const windowStart = localMidnight(-1);         // yesterday 00:00 local
      const windowEnd   = localMidnight(2) - 60_000; // tomorrow 23:59 local

      const history      = await this._fetchHistory(windowStart);
      const pricePast    = history.nordpool.filter(([t]) => t <= now);
      const priceFuture  = this._readNordpoolFuture(now, windowEnd);
      const solarActual  = history.solar
        .filter(([t]) => t >= windowStart && t <= now)
        .map(([t, w]) => [t, w / 1000]);
      const solarForecast = this._readSolarForecast(now, windowStart, windowEnd);

      if (!pricePast.length && !priceFuture.length) {
        this._setStatus('No price data available.');
        return;
      }

      this._clearStatus();
      if (this._chart) { this._chart.destroy(); this._chart = null; }

      // Series order matters — must match yaxis array indices below
      const series = [
        { name: 'Price — past',      data: pricePast     }, // 0: left Y, amber
        { name: 'Price — future',    data: priceFuture   }, // 1: left Y, blue
        { name: 'Solar actual',      data: solarActual   }, // 2: right Y, green solid
        { name: 'Solar forecast',    data: solarForecast }, // 3: right Y, green dashed
      ];

      const options = {
        series,

        chart: {
          type: 'line',
          height: 460,
          background: 'transparent',
          toolbar: {
            show: true,
            tools: { zoom: true, zoomin: true, zoomout: true, pan: true, reset: true, download: false },
          },
          zoom: { enabled: true, type: 'x' },
          animations: { enabled: false },
          fontFamily: 'inherit',
          events: {
            beforeResetZoom: () => ({ xaxis: { min: windowStart, max: windowEnd } }),
          },
        },

        theme: { mode: 'dark' },

        stroke: {
          show: true,
          curve:     ['stepline', 'stepline', 'smooth', 'smooth'],
          width:     [2,          2,          2,        1.5],
          dashArray: [0,          5,          0,        5],
        },

        colors: ['#ffb300', '#42a5f5', '#4caf50', '#80cbc4'],

        fill: {
          type: ['solid', 'solid', 'gradient', 'solid'],
          gradient: {
            type: 'vertical',
            shadeIntensity: 0,
            opacityFrom: 0.4,
            opacityTo: 0.02,
            stops: [0, 100],
          },
        },

        xaxis: {
          type: 'datetime',
          min: windowStart,
          max: windowEnd,
          labels: {
            datetimeUTC: false,
            format: 'dd MMM HH:mm',
            style: { colors: '#aaa', fontSize: '10px' },
            rotate: -30,
          },
          axisBorder: { show: false },
          axisTicks: { show: false },
        },

        // One yaxis entry per series. Hidden entries must carry seriesName
        // of the axis they share so ApexCharts scales them together.
        yaxis: [
          {
            // 0 → 'Price — past' : price axis left
            seriesName: 'Price — past',
            title: { text: 'EUR / kWh', style: { color: '#ffb300', fontSize: '11px' } },
            min: 0,
            forceNiceScale: true,
            decimalsInFloat: 3,
            labels: {
              style: { colors: '#ffb300', fontSize: '10px' },
              formatter: v => (v != null ? v.toFixed(3) : ''),
            },
          },
          {
            // 1 → 'Price — future' : shares price axis
            seriesName: 'Price — past',
            show: false,
          },
          {
            // 2 → 'Solar actual' : solar kW axis right
            seriesName: 'Solar actual',
            opposite: true,
            title: { text: 'kW', style: { color: '#4caf50', fontSize: '11px' } },
            min: 0,
            forceNiceScale: true,
            decimalsInFloat: 1,
            labels: {
              style: { colors: '#4caf50', fontSize: '10px' },
              formatter: v => (v != null ? v.toFixed(1) : ''),
            },
          },
          {
            // 3 → 'Solar forecast' : shares solar axis
            seriesName: 'Solar actual',
            opposite: true,
            show: false,
          },
        ],

        annotations: {
          xaxis: [{
            x: now,
            borderColor: 'rgba(255,255,255,0.55)',
            strokeDashArray: 5,
            label: {
              text: 'Now',
              position: 'top',
              style: {
                color: '#fff',
                background: '#333',
                fontSize: '11px',
                padding: { top: 3, bottom: 3, left: 6, right: 6 },
              },
            },
          }],
        },

        tooltip: {
          shared: true,
          intersect: false,
          theme: 'dark',
          x: { format: 'dd MMM yyyy HH:mm' },
          y: {
            formatter: (val, { seriesIndex }) => {
              if (val == null) return null;
              return seriesIndex <= 1
                ? val.toFixed(4) + ' €/kWh'
                : val.toFixed(2) + ' kW';
            },
          },
        },

        legend: {
          show: true,
          labels: { colors: '#aaa' },
        },

        grid: {
          borderColor: 'rgba(255,255,255,0.07)',
          xaxis: { lines: { show: true } },
          yaxis: { lines: { show: true } },
        },

        markers: { size: 0 },
        dataLabels: { enabled: false },
      };

      const el = this.shadowRoot.querySelector('#chart');
      this._chart = new ApexCharts(el, options);
      this._chart.render();
    }
  }

  customElements.define('sun-sale-panel', SunSalePanel);
})();
