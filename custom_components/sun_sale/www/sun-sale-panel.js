/* Sun Sale Dashboard Panel — Nordpool price chart
 *
 * Single step-line chart showing electricity spot prices over a 72-hour window
 * (past 24 h in amber, future 48 h in blue) with a vertical "Now" marker.
 *
 * Past prices come from the HA history API.
 * Future prices come from sensor.nordpool_kwh_lt_eur_3_10_0 attributes
 * (raw_today / raw_tomorrow).
 */

(function () {
  'use strict';

  const NORDPOOL_ENTITY  = 'sensor.nordpool_kwh_lt_eur_3_10_0';
  const APEXCHARTS_CDN   = 'https://cdn.jsdelivr.net/npm/apexcharts@3.54.0/dist/apexcharts.min.js';
  const HOURS_PAST       = 24;
  const HOURS_FUTURE     = 48;

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
        this._setStatus('⚠ ' + e.message + ' — check your network or install ApexCharts via HACS.');
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
          <h2>☀ Sun Sale — Nordpool Prices</h2>
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

    // ── Data fetching ─────────────────────────────────────────────────────────

    async _fetchPastPrices(windowStartMs) {
      const start = new Date(windowStartMs).toISOString();
      const end   = new Date().toISOString();
      const url   = `/api/history/period/${start}?end_time=${end}`
        + `&filter_entity_id=${NORDPOOL_ENTITY}&minimal_response=true&no_attributes=true`;
      try {
        const resp = await this._hass.fetchWithAuth(url);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const raw = await resp.json();
        return (raw[0] || [])
          .filter(s => s.state && s.state !== 'unavailable' && s.state !== 'unknown')
          .map(s => [new Date(s.last_changed).getTime(), parseFloat(s.state)])
          .filter(([, v]) => isFinite(v));
      } catch (e) {
        console.warn('sunSale: history fetch failed', e);
        return [];
      }
    }

    _readFuturePrices() {
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
            if (isFinite(v)) points.push([t, v]);
          } catch { /* skip */ }
        }
      }
      return points.sort((a, b) => a[0] - b[0]);
    }

    // ── Chart render ──────────────────────────────────────────────────────────

    async _render() {
      const now          = Date.now();
      const windowStart  = now - HOURS_PAST   * 3600 * 1000;
      const windowEnd    = now + HOURS_FUTURE  * 3600 * 1000;

      const allPast   = await this._fetchPastPrices(windowStart);
      const allFuture = this._readFuturePrices();

      const pastData   = allPast.filter(([t])   => t <= now);
      const futureData = allFuture.filter(([t]) => t >  now && t <= windowEnd);

      if (pastData.length === 0 && futureData.length === 0) {
        this._setStatus('No Nordpool price data available.');
        return;
      }

      this._clearStatus();

      if (this._chart) { this._chart.destroy(); this._chart = null; }

      const options = {
        series: [
          { name: 'Price — past',   data: pastData   },
          { name: 'Price — future', data: futureData },
        ],

        chart: {
          type: 'line',
          height: 420,
          background: 'transparent',
          toolbar: {
            show: true,
            tools: {
              zoom: true, zoomin: true, zoomout: true,
              pan: true, reset: true, download: false,
            },
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
          curve: 'stepline',
          width: [2, 2],
          dashArray: [0, 0],
        },

        colors: ['#ffb300', '#42a5f5'],

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

        yaxis: {
          title: { text: 'EUR / kWh', style: { color: '#aaa', fontSize: '11px' } },
          min: 0,
          forceNiceScale: true,
          decimalsInFloat: 4,
          labels: {
            style: { colors: '#aaa', fontSize: '10px' },
            formatter: v => v != null ? v.toFixed(3) : '',
          },
        },

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
            formatter: val => val != null ? val.toFixed(4) + ' €/kWh' : null,
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
