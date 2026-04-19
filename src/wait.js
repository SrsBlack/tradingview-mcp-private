import { evaluate } from './connection.js';

const DEFAULT_TIMEOUT = 15000;
const POLL_INTERVAL = 300;
const CHART_API = 'window.TradingViewApi._activeChartWidgetWV.value()';

export async function waitForChartReady(expectedSymbol = null, expectedTf = null, timeout = DEFAULT_TIMEOUT) {
  const start = Date.now();
  let lastBarCount = -1;
  let stableCount = 0;

  // Extract the base symbol for matching (e.g., "BITSTAMP:BTCUSD" → "BTCUSD")
  const expectedBase = expectedSymbol ? expectedSymbol.split(':').pop().toUpperCase() : null;

  while (Date.now() - start < timeout) {
    const state = await evaluate(`
      (function() {
        // Check for loading spinner
        var spinner = document.querySelector('[class*="loader"]')
          || document.querySelector('[class*="loading"]')
          || document.querySelector('[data-name="loading"]');
        var isLoading = spinner && spinner.offsetParent !== null;

        // Get bar count from chart bars
        var barCount = -1;
        try {
          var bars = document.querySelectorAll('[class*="bar"]');
          barCount = bars.length;
        } catch {}

        // Get current symbol from chart API (more reliable than DOM)
        var apiSymbol = '';
        try {
          apiSymbol = ${CHART_API}.symbol();
        } catch {}

        // Fallback: DOM symbol from legend
        var domSymbol = '';
        try {
          var symbolEl = document.querySelector('[data-name="legend-source-title"]')
            || document.querySelector('[class*="title"] [class*="apply-common-tooltip"]');
          domSymbol = symbolEl ? symbolEl.textContent.trim() : '';
        } catch {}

        return { isLoading: !!isLoading, barCount: barCount, apiSymbol: apiSymbol, domSymbol: domSymbol };
      })()
    `);

    if (!state) {
      await new Promise(r => setTimeout(r, POLL_INTERVAL));
      continue;
    }

    // Not ready if still loading
    if (state.isLoading) {
      stableCount = 0;
      await new Promise(r => setTimeout(r, POLL_INTERVAL));
      continue;
    }

    // Check symbol match via API (primary) or DOM (fallback)
    if (expectedBase) {
      const apiBase = (state.apiSymbol || '').split(':').pop().toUpperCase();
      const domBase = (state.domSymbol || '').toUpperCase();
      if (apiBase !== expectedBase && !domBase.includes(expectedBase)) {
        stableCount = 0;
        await new Promise(r => setTimeout(r, POLL_INTERVAL));
        continue;
      }
    }

    // Check bar count stability
    if (state.barCount === lastBarCount && state.barCount > 0) {
      stableCount++;
    } else {
      stableCount = 0;
    }
    lastBarCount = state.barCount;

    if (stableCount >= 3) {
      return true;
    }

    await new Promise(r => setTimeout(r, POLL_INTERVAL));
  }

  // Timeout — return false
  return false;
}
