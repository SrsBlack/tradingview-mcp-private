"""
Full ICT Analysis Pipeline — bridges TradingView chart data to trading-ai-v2 ICT scorer.

For each symbol:
  1. Collect OHLCV across H4 (bias), H1 (intermediate), M15 (trigger)
  2. Run structure, FVG, OB, liquidity, session, SMT analysis
  3. Score with score_ict_setup() → ICTScoreBreakdown (0-100, Grade A/B/C/D)

Usage:
    from bridge.ict_pipeline import ICTPipeline
    pipeline = ICTPipeline()
    results = pipeline.analyze_watchlist()  # → list[SymbolAnalysis]
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from bridge.config import get_bridge_config, ensure_trading_ai_path, BridgeConfig
from bridge.tv_client import TVClient, TVClientError
from bridge.tv_data_adapter import bars_to_dataframe, validate_dataframe

# Ensure trading-ai-v2 is importable
ensure_trading_ai_path()

from analysis.structure import detect_swings, classify_structure, get_current_bias, SwingPoint, StructureEvent
from analysis.fvg import detect_fvgs, get_active_fvgs, price_in_fvg, FVGZone
from analysis.order_blocks import detect_order_blocks, get_active_obs, OrderBlock
from analysis.liquidity import scan_sweeps, get_draw_on_liquidity, swing_to_liquidity, LiquidityLevel, LiquiditySweep
from analysis.sessions import get_session_info, SessionInfo
from analysis.smt import detect_smt_divergence, SMT_PAIRS
from analysis.ict.scorer import score_ict_setup, ICTScoreBreakdown
from core.types import Direction, SignalGrade


# ---------------------------------------------------------------------------
# Result data classes
# ---------------------------------------------------------------------------

@dataclass
class TimeframeAnalysis:
    """Analysis results for a single timeframe."""
    timeframe: str  # e.g., "H4", "H1", "M15"
    bar_count: int = 0
    swing_count: int = 0
    structure_events: int = 0
    bias: str = "NEUTRAL"
    fvg_count: int = 0
    ob_count: int = 0
    sweep_count: int = 0


@dataclass
class SymbolAnalysis:
    """Complete ICT analysis for a single symbol."""
    symbol: str
    timestamp: str = ""
    current_price: float = 0.0

    # ICT Score
    total_score: float = 0.0
    grade: str = "INVALID"
    direction: str = "NEUTRAL"
    confidence: float = 0.0
    confluence_factors: list[str] = field(default_factory=list)

    # Score breakdown
    structure_score: float = 0.0
    liquidity_score: float = 0.0
    ob_score: float = 0.0
    fvg_score: float = 0.0
    session_score: float = 0.0
    ote_score: float = 0.0
    smt_score: float = 0.0

    # Session context
    session_type: str = ""
    is_kill_zone: bool = False
    is_silver_bullet: bool = False

    # Per-timeframe detail
    htf_analysis: TimeframeAnalysis | None = None
    itf_analysis: TimeframeAnalysis | None = None
    ltf_analysis: TimeframeAnalysis | None = None

    # SMT
    has_smt: bool = False
    smt_pair: str = ""

    # Liquidity sweep
    sweep_detected: bool = False

    # Risk level suggestion
    risk_level: str = "SKIP"

    # Error info
    error: str | None = None

    def to_dict(self) -> dict:
        """Convert to serializable dict."""
        d = {
            "symbol": self.symbol,
            "timestamp": self.timestamp,
            "current_price": self.current_price,
            "total_score": round(self.total_score, 1),
            "grade": self.grade,
            "direction": self.direction,
            "confidence": round(self.confidence, 2),
            "confluence_factors": self.confluence_factors,
            "breakdown": {
                "structure": round(self.structure_score, 1),
                "liquidity": round(self.liquidity_score, 1),
                "order_block": round(self.ob_score, 1),
                "fvg": round(self.fvg_score, 1),
                "session": round(self.session_score, 1),
                "ote": round(self.ote_score, 1),
                "smt": round(self.smt_score, 1),
            },
            "session": {
                "type": self.session_type,
                "is_kill_zone": self.is_kill_zone,
                "is_silver_bullet": self.is_silver_bullet,
            },
            "has_smt": self.has_smt,
            "sweep_detected": self.sweep_detected,
            "risk_level": self.risk_level,
        }
        if self.error:
            d["error"] = self.error
        return d


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

class ICTPipeline:
    """
    Full ICT analysis pipeline from TradingView chart data.

    Connects to TradingView via CDP, fetches multi-timeframe OHLCV,
    and runs the complete trading-ai-v2 ICT scoring chain.
    """

    def __init__(self, config: BridgeConfig | None = None, client: TVClient | None = None):
        self.config = config or get_bridge_config()
        self.client = client or TVClient()

        # Cache for H4 data (changes infrequently)
        self._h4_cache: dict[str, tuple[pd.DataFrame, float]] = {}
        self._H4_CACHE_TTL = 3600  # 1 hour

    # ------------------------------------------------------------------
    # Single symbol analysis
    # ------------------------------------------------------------------

    def analyze_symbol(self, symbol: str) -> SymbolAnalysis:
        """
        Run full ICT analysis on a single symbol.

        Steps:
            1. Collect OHLCV for H4, H1, M15
            2. Detect structure, FVGs, OBs, liquidity sweeps
            3. Check session context and SMT divergence
            4. Score with score_ict_setup()

        Args:
            symbol: TradingView symbol name (e.g., "BTCUSD", "EURUSD")

        Returns:
            SymbolAnalysis with full ICT score and breakdown.
        """
        result = SymbolAnalysis(
            symbol=symbol,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

        try:
            # -- Step 1: Collect multi-TF OHLCV --
            dfs = self._collect_data(symbol)

            df_htf = dfs.get("H4")
            df_itf = dfs.get("H1")
            df_ltf = dfs.get("M15")

            # Use the best available timeframe for analysis
            # Prefer M15 (trigger), fallback to H1, then H4
            df_primary = None
            for candidate in [df_ltf, df_itf, df_htf]:
                if candidate is not None and not candidate.empty:
                    df_primary = candidate
                    break
            if df_primary is None:
                result.error = "DATA_UNAVAILABLE"
                return result

            result.current_price = float(df_primary["close"].iloc[-1])

            # -- Step 2: Structure analysis (on H4 for bias) --
            df_structure = df_htf if (df_htf is not None and len(df_htf) >= 20) else df_primary

            swings = detect_swings(df_structure, lookback=5)
            labeled_swings, structure_events = classify_structure(swings, df=df_structure)
            htf_bias = get_current_bias(structure_events)

            if df_htf is not None:
                result.htf_analysis = TimeframeAnalysis(
                    timeframe="H4",
                    bar_count=len(df_htf),
                    swing_count=len(swings),
                    structure_events=len(structure_events),
                    bias=htf_bias.name,
                )

            # Use HTF bias as the directional context
            direction = htf_bias if htf_bias != Direction.NEUTRAL else Direction.BULLISH

            # -- Step 3: FVG detection (on M15 for precision) --
            fvgs: list[FVGZone] = []
            df_fvg = df_ltf if (df_ltf is not None and len(df_ltf) >= 10) else df_primary
            if df_fvg is not None and len(df_fvg) >= 10:
                fvgs = detect_fvgs(df_fvg, max_age_bars=50)

            # -- Step 4: Order Block detection --
            obs: list[OrderBlock] = []
            df_ob = df_ltf if (df_ltf is not None and len(df_ltf) >= 20) else df_primary
            if df_ob is not None and len(df_ob) >= 20:
                ltf_swings = detect_swings(df_ob, lookback=5)
                # detect_order_blocks needs fvgs and swings
                obs = detect_order_blocks(
                    df_ob, fvgs=fvgs, swings=ltf_swings,
                    lookback=20, require_fvg=True, require_bos=True,
                )

            # -- Step 5: Liquidity detection --
            sweeps: list[LiquiditySweep] = []
            dol: LiquidityLevel | None = None
            df_liq = df_ltf if (df_ltf is not None and len(df_ltf) >= 20) else df_primary
            if df_liq is not None and len(df_liq) >= 20:
                liq_swings = detect_swings(df_liq, lookback=5)
                # Build liquidity levels from swing points
                levels = swing_to_liquidity(liq_swings)
                # Scan recent bars for sweeps
                sweeps = scan_sweeps(levels, df_liq, lookback_bars=10)
                result.sweep_detected = len(sweeps) > 0
                # Find the draw on liquidity
                dol = get_draw_on_liquidity(levels, result.current_price, bias=direction)

            # -- Step 6: Session context --
            session_info = get_session_info(datetime.now(timezone.utc))
            result.session_type = session_info.session.name if session_info.session else "UNKNOWN"
            result.is_kill_zone = session_info.is_kill_zone
            result.is_silver_bullet = session_info.is_silver_bullet

            # -- Step 7: SMT divergence --
            has_smt = False
            smt_pair = self.config.smt_pairs.get(self.config.internal_symbol(symbol))
            if smt_pair and (df_primary is not None):
                try:
                    smt_df = self._get_smt_data(smt_pair)
                    if smt_df is not None and len(smt_df) >= 20:
                        smt_result = detect_smt_divergence(
                            self.config.internal_symbol(symbol), df_primary
                        )
                        # detect_smt_divergence returns bool or SMTDivergence
                        has_smt = bool(smt_result)
                        result.smt_pair = smt_pair
                except Exception:
                    pass  # SMT is optional; don't fail the whole analysis

            result.has_smt = has_smt

            # -- Step 8: Determine range for OTE/premium-discount --
            range_high = float(df_primary["high"].max())
            range_low = float(df_primary["low"].min())

            # -- Step 9: Score! --
            score = score_ict_setup(
                current_price=result.current_price,
                direction=direction,
                session_info=session_info,
                structure_events=structure_events,
                swings=swings,
                fvgs=fvgs,
                obs=obs,
                liquidity_sweeps=sweeps,
                draw_on_liquidity=dol,
                has_smt_divergence=has_smt,
                range_high=range_high,
                range_low=range_low,
            )

            # -- Step 10: Populate result --
            result.total_score = score.total
            result.grade = score.grade.name
            result.direction = score.direction.name
            result.confluence_factors = score.confluence_factors

            result.structure_score = score.structure
            result.liquidity_score = score.liquidity
            result.ob_score = score.order_block
            result.fvg_score = score.fvg
            result.session_score = score.session
            result.ote_score = score.ote
            result.smt_score = score.smt

            # Confidence = normalized score (0-1)
            result.confidence = min(score.total / 100.0, 1.0)

            # Risk level based on grade
            if score.grade in (SignalGrade.A,):
                result.risk_level = "1% per trade (FTMO)"
            elif score.grade == SignalGrade.B:
                result.risk_level = "0.5% per trade (reduced)"
            elif score.grade == SignalGrade.C:
                result.risk_level = "0.25% per trade (pullback only)"
            else:
                result.risk_level = "SKIP"

            # LTF analysis summary
            if df_ltf is not None:
                ltf_swings = detect_swings(df_ltf, lookback=5)
                result.ltf_analysis = TimeframeAnalysis(
                    timeframe="M15",
                    bar_count=len(df_ltf),
                    swing_count=len(ltf_swings),
                    structure_events=len(structure_events),
                    bias=htf_bias.name,
                    fvg_count=len(fvgs),
                    ob_count=len(obs),
                    sweep_count=len(sweeps),
                )

        except Exception as e:
            result.error = f"{type(e).__name__}: {e}"

        return result

    # ------------------------------------------------------------------
    # Watchlist scan
    # ------------------------------------------------------------------

    def analyze_watchlist(self, symbols: list[str] | None = None) -> list[SymbolAnalysis]:
        """
        Analyze all symbols in the watchlist.

        Args:
            symbols: Override list. Defaults to config.watchlist.

        Returns:
            List of SymbolAnalysis results.
        """
        symbols = symbols or self.config.watchlist
        results: list[SymbolAnalysis] = []

        for symbol in symbols:
            print(f"[ICT] Analyzing {symbol}...", flush=True)
            t0 = time.time()
            analysis = self.analyze_symbol(symbol)
            elapsed = time.time() - t0
            print(
                f"[ICT] {symbol}: Grade {analysis.grade} "
                f"({analysis.total_score:.0f}/100) "
                f"{analysis.direction} "
                f"[{elapsed:.1f}s]",
                flush=True,
            )
            results.append(analysis)

        return results

    # ------------------------------------------------------------------
    # Data collection helpers
    # ------------------------------------------------------------------

    def _collect_data(self, symbol: str) -> dict[str, pd.DataFrame | None]:
        """
        Collect OHLCV data across H4, H1, M15 timeframes.

        Uses H4 cache to avoid unnecessary chart switches.

        Returns:
            {"H4": df_or_None, "H1": df_or_None, "M15": df_or_None}
        """
        dfs: dict[str, pd.DataFrame | None] = {"H4": None, "H1": None, "M15": None}
        cfg = self.config

        # Switch to the correct symbol and poll until the chart confirms it loaded.
        # TradingView Desktop has one chart — we must wait for the symbol to actually change.
        target_sym = symbol.split(":")[-1]
        try:
            self.client.set_symbol(symbol)
            # Poll quote until chart symbol matches, up to 15s
            # Compare only the ticker part (strip exchange prefix) since TV may
            # return a different exchange prefix (e.g. CBOT_MINI_DL vs CBOT)
            for _ in range(15):
                time.sleep(1.0)
                try:
                    quote = self.client.get_quote()
                    chart_sym = quote.get("symbol", "").split(":")[-1]
                    if chart_sym == target_sym:
                        time.sleep(0.5)
                        break
                except Exception:
                    pass
            else:
                print(f"[WARN] Chart did not switch to {symbol} in time — skipping", flush=True)
                return dfs
        except TVClientError:
            pass

        # At this point the chart is confirmed on the correct symbol.
        # All subsequent OHLCV fetches switch timeframe only (switch=False for symbol).
        # This prevents re-triggering a symbol switch that could race with stale data.

        def _fetch_tf(timeframe: str, count: int) -> pd.DataFrame | None:
            """Switch to timeframe (symbol already correct) and fetch OHLCV."""
            try:
                self.client.set_timeframe(timeframe)
                time.sleep(1.5)  # wait for bars to reload on new timeframe
                raw = self.client.get_ohlcv(switch=False, count=count)
                # Verify the returned data isn't from the wrong symbol by checking
                # that the current quote symbol still matches our target
                try:
                    q = self.client.get_quote()
                    live_sym = q.get("symbol", "").split(":")[-1]
                    if live_sym and live_sym != target_sym:
                        print(f"[WARN] Symbol drift on {timeframe}: expected {target_sym}, got {live_sym} — discarding", flush=True)
                        return None
                except Exception:
                    pass
                df = bars_to_dataframe(raw)
                valid, _ = validate_dataframe(df)
                return df if valid else None
            except TVClientError:
                return None

        # Check H4 cache
        cached = self._h4_cache.get(symbol)
        if cached and (time.time() - cached[1]) < self._H4_CACHE_TTL:
            dfs["H4"] = cached[0]
        else:
            df = _fetch_tf(cfg.htf, cfg.bar_counts.get(cfg.htf, 200))
            if df is not None:
                dfs["H4"] = df
                self._h4_cache[symbol] = (df, time.time())

        # Fetch H1
        df = _fetch_tf(cfg.itf, cfg.bar_counts.get(cfg.itf, 200))
        if df is not None:
            dfs["H1"] = df

        # Fetch M15
        df = _fetch_tf(cfg.ltf, cfg.bar_counts.get(cfg.ltf, 200))
        if df is not None:
            dfs["M15"] = df

        return dfs

    def _get_smt_data(self, smt_symbol: str) -> pd.DataFrame | None:
        """Fetch OHLCV for a correlated symbol (for SMT divergence check)."""
        try:
            tv_symbol = self.config.tv_symbol(smt_symbol)
            raw = self.client.get_ohlcv(
                symbol=tv_symbol,
                timeframe=self.config.ltf,
                count=50,
            )
            df = bars_to_dataframe(raw)
            valid, _ = validate_dataframe(df, min_bars=10)
            return df if valid else None
        except TVClientError:
            return None


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    """Run ICT pipeline on watchlist and print results."""
    import json

    pipeline = ICTPipeline()
    results = pipeline.analyze_watchlist()

    # Print summary table
    print("\n" + "=" * 70)
    print(f"{'Symbol':<10} {'Grade':<6} {'Score':<8} {'Direction':<10} {'Confluence'}")
    print("-" * 70)
    for r in results:
        factors = ", ".join(r.confluence_factors[:4]) if r.confluence_factors else "None"
        print(f"{r.symbol:<10} {r.grade:<6} {r.total_score:>5.1f}/100 {r.direction:<10} {factors}")
    print("=" * 70)

    # Print full JSON
    print("\nFull analysis:")
    print(json.dumps([r.to_dict() for r in results], indent=2))


if __name__ == "__main__":
    main()
