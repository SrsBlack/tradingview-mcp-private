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

from bridge.config import get_bridge_config, ensure_trading_ai_path, BridgeConfig, price_in_range, PRICE_RANGES
from bridge.price_verify import PriceVerifier
from bridge.tv_client import TVClient, TVClientError
from bridge.tv_data_adapter import bars_to_dataframe, validate_dataframe

# Ensure trading-ai-v2 is importable
ensure_trading_ai_path()

from analysis.structure import detect_swings, classify_structure, get_current_bias, SwingPoint, StructureEvent
from analysis.fvg import detect_fvgs, get_active_fvgs, price_in_fvg, FVGZone
from analysis.order_blocks import detect_order_blocks, get_active_obs, OrderBlock
from analysis.liquidity import scan_sweeps, get_draw_on_liquidity, swing_to_liquidity, build_liquidity_map, LiquidityLevel, LiquiditySweep
from analysis.sessions import get_session_info, SessionInfo
from analysis.smt import detect_smt_divergence, SMT_PAIRS
from analysis.ict.scorer import score_ict_setup, ICTScoreBreakdown, get_pd_zone, pd_aligned_with_bias
from analysis.ict.core import detect_cisd, get_latest_cisd, detect_po3_phase, PO3Phase, detect_judas_swing, JudasSwing
from analysis.ict.advanced import run_advanced_analysis, AdvancedAnalysis
from analysis.sessions import get_asian_range, get_ndog, get_cbdr
from core.types import Direction, SignalGrade, FVGQuality


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

    # Liquidity sweep + displacement
    sweep_detected: bool = False
    displacement_confirmed: bool = False  # FVG created after sweep = displacement proof

    # Premium/Discount zone
    pd_zone: str = ""  # "premium", "discount", "equilibrium"
    pd_aligned: bool = False  # True if zone is correct for direction

    # Risk level suggestion
    risk_level: str = "SKIP"

    # Volatility (ATR-14 on M15)
    atr_m15: float = 0.0

    # Advanced ICT concepts
    has_cisd: bool = False
    po3_phase: str = ""  # accumulation, manipulation, distribution
    has_judas_swing: bool = False
    judas_direction: str = ""  # BULLISH / BEARISH
    is_macro_time: bool = False
    asian_range: tuple[float, float] | None = None
    ndog_count: int = 0  # New Day Opening Gaps detected
    cbdr_range: tuple[float, float] | None = None
    advanced_score: float = 0.0
    advanced_factors: list[str] = field(default_factory=list)

    # Fibonacci extension TP levels
    fib_tp_levels: list[float] = field(default_factory=list)

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
            "displacement_confirmed": self.displacement_confirmed,
            "pd_zone": self.pd_zone,
            "pd_aligned": self.pd_aligned,
            "risk_level": self.risk_level,
            "atr_m15": self.atr_m15,
            "advanced": {
                "has_cisd": self.has_cisd,
                "po3_phase": self.po3_phase,
                "has_judas_swing": self.has_judas_swing,
                "judas_direction": self.judas_direction,
                "is_macro_time": self.is_macro_time,
                "asian_range": list(self.asian_range) if self.asian_range else None,
                "ndog_count": self.ndog_count,
                "cbdr_range": list(self.cbdr_range) if self.cbdr_range else None,
                "advanced_score": round(self.advanced_score, 1),
                "advanced_factors": self.advanced_factors,
                "fib_tp_levels": [round(l, 5) for l in self.fib_tp_levels],
            },
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
        self.price_verifier = PriceVerifier()

        # Cache for H4 data (changes infrequently)
        self._h4_cache: dict[str, tuple[pd.DataFrame, float]] = {}
        self._H4_CACHE_TTL = 3600  # 1 hour
        self._last_verified_price: float = 0.0
        self._last_collected_dfs: dict[str, pd.DataFrame] = {}

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
            # When NEUTRAL, we'll score both directions and pick the stronger one
            direction = htf_bias
            score_both_directions = (htf_bias == Direction.NEUTRAL)

            # -- Step 3: FVG detection (on M15 for precision) --
            fvgs: list[FVGZone] = []
            df_fvg = df_ltf if (df_ltf is not None and len(df_ltf) >= 10) else df_primary
            if df_fvg is not None and len(df_fvg) >= 10:
                fvgs = detect_fvgs(df_fvg, max_age_bars=50, min_quality=FVGQuality.VERY_AGGRESSIVE)

            # -- Step 4: Order Block detection --
            obs: list[OrderBlock] = []
            df_ob = df_ltf if (df_ltf is not None and len(df_ltf) >= 20) else df_primary
            if df_ob is not None and len(df_ob) >= 20:
                ltf_swings = detect_swings(df_ob, lookback=5)
                # detect_order_blocks needs fvgs and swings
                obs = detect_order_blocks(
                    df_ob, fvgs=fvgs, swings=ltf_swings,
                    lookback=20, require_fvg=True, require_bos=False,
                )

            # -- Step 5: Liquidity detection --
            sweeps: list[LiquiditySweep] = []
            dol: LiquidityLevel | None = None
            levels: list = []
            df_liq = df_ltf if (df_ltf is not None and len(df_ltf) >= 20) else df_primary
            if df_liq is not None and len(df_liq) >= 20:
                liq_swings = detect_swings(df_liq, lookback=5)
                # Build comprehensive liquidity map: swings + PDH/PDL + PWH/PWL + equal levels
                levels = build_liquidity_map(df_liq, liq_swings)
                # Scan recent bars for sweeps (64 bars = 16 hours on M15 — covers London→NY)
                sweeps = scan_sweeps(levels, df_liq, lookback_bars=64)
                result.sweep_detected = len(sweeps) > 0

                # Displacement confirmation: sweep + FVG in opposite direction = proven manipulation
                # An SSL sweep (BEARISH) followed by bullish FVGs = bullish displacement confirmed
                # A BSL sweep (BULLISH) followed by bearish FVGs = bearish displacement confirmed
                if sweeps and fvgs:
                    for sweep in sweeps:
                        # FVG must exist after the sweep bar and in the reversal direction
                        reversal_dir = Direction.BULLISH if sweep.sweep_direction == Direction.BEARISH else Direction.BEARISH
                        displacement_fvgs = [
                            f for f in fvgs
                            if f.direction == reversal_dir and f.bar_index > sweep.bar_index
                        ]
                        if displacement_fvgs:
                            result.displacement_confirmed = True
                            break

                # Find the draw on liquidity (need direction; defer if scoring both)
                if not score_both_directions:
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
                        smt_result = detect_smt_divergence(df_primary, smt_df)
                        has_smt = bool(smt_result)
                        result.smt_pair = smt_pair
                except Exception:
                    pass  # SMT is optional; don't fail the whole analysis

            result.has_smt = has_smt

            # -- Step 8: Determine dealing range for OTE/premium-discount --
            # Use M15 (trigger TF) swings for the dealing range — tighter and more
            # precise than H4 swings, which produce OTE zones too wide to score.
            ltf_swing_list = detect_swings(df_primary, lookback=5) if df_primary is not None else []
            ltf_highs = [s for s in ltf_swing_list if s.swing_type == "swing_high"]
            ltf_lows = [s for s in ltf_swing_list if s.swing_type == "swing_low"]
            if ltf_highs and ltf_lows:
                # Use the most recent swing high/low pair as the dealing range
                range_high = float(max(s.price for s in ltf_highs[-3:]))
                range_low = float(min(s.price for s in ltf_lows[-3:]))
            else:
                # Fallback: use last 50 bars (12.5 hours on M15) instead of full dataset
                recent = df_primary.iloc[-50:] if len(df_primary) >= 50 else df_primary
                range_high = float(recent["high"].max())
                range_low = float(recent["low"].min())

            # -- Step 8b: Premium/Discount zone --
            if range_high > range_low:
                result.pd_zone = get_pd_zone(result.current_price, range_high, range_low)
                result.pd_aligned = pd_aligned_with_bias(
                    result.current_price, range_high, range_low, direction
                )

            # -- Step 8c: Macro time check --
            result.is_macro_time = getattr(session_info, 'is_macro_time', False)

            # -- Step 8d: CISD detection (on M15 — earliest reversal signal) --
            cisd_signals = detect_cisd(df_primary) if df_primary is not None and len(df_primary) >= 10 else []
            latest_cisd = get_latest_cisd(cisd_signals, direction=direction, max_age_bars=20)
            result.has_cisd = latest_cisd is not None

            # -- Step 8e: PO3 phase detection --
            # Use the last 20 M15 bars as "session" slice (~5 hours of current session)
            po3_result = None
            if df_primary is not None and len(df_primary) >= 6:
                session_slice = df_primary.iloc[-20:] if len(df_primary) >= 20 else df_primary
                po3_result = detect_po3_phase(session_slice, daily_bias=direction)
                result.po3_phase = po3_result.phase.value
                # PO3 factor appended in Step 10b (after score sets confluence_factors)

            # -- Step 8e2: Asian Range detection --
            asian_rng = None
            if df_primary is not None and len(df_primary) >= 20:
                try:
                    asian_rng = get_asian_range(df_primary)
                    if asian_rng:
                        result.asian_range = asian_rng
                except Exception:
                    pass

            # -- Step 8e3: Judas Swing detection --
            if df_primary is not None and len(df_primary) >= 6 and direction != Direction.NEUTRAL:
                session_slice = df_primary.iloc[-20:] if len(df_primary) >= 20 else df_primary
                judas = detect_judas_swing(session_slice, daily_bias=direction, asian_range=asian_rng)
                if judas:
                    result.has_judas_swing = True
                    result.judas_direction = judas.direction.name

            # -- Step 8e4: NDOG (New Day Opening Gaps) --
            if df_primary is not None and len(df_primary) >= 30:
                try:
                    ndogs = get_ndog(df_primary)
                    result.ndog_count = len(ndogs)
                except Exception:
                    pass

            # -- Step 8e5: CBDR (Central Bank Dealers Range) --
            cbdr_data = None
            if df_primary is not None and len(df_primary) >= 20:
                try:
                    cbdr_data = get_cbdr(df_primary)
                    if cbdr_data:
                        result.cbdr_range = cbdr_data
                except Exception:
                    pass

            # -- Step 8f: Advanced ICT analysis (CRT, Turtle Soup, Unicorn, IPDA, etc.) --
            adv: AdvancedAnalysis | None = None
            try:
                if df_primary is not None and len(df_primary) >= 20:
                    # Compute CBDR range in pips for advanced scoring
                    cbdr_pips = 0.0
                    if cbdr_data:
                        cbdr_pips = abs(cbdr_data[0] - cbdr_data[1])
                        # Rough conversion: forex pairs have 5-digit prices
                        base_sym = self.config.internal_symbol(symbol)
                        if base_sym in ("EURUSD", "GBPUSD", "AUDUSD", "NZDUSD"):
                            cbdr_pips *= 10000  # to pips
                        elif base_sym == "USDJPY":
                            cbdr_pips *= 100
                    session_slice_adv = df_primary.iloc[-20:] if len(df_primary) >= 20 else df_primary
                    adv = run_advanced_analysis(
                        df=df_primary,
                        symbol=self.config.internal_symbol(symbol),
                        obs=obs,
                        fvgs=fvgs,
                        direction=direction,
                        has_cisd=result.has_cisd,
                        in_kill_zone=result.is_kill_zone,
                        daily_bias=direction,
                        session_df=session_slice_adv,
                        cbdr=cbdr_data,
                        cbdr_range_pips=cbdr_pips,
                    )
                    result.advanced_score = adv.advanced_score
                    # Add advanced findings as confluence factors
                    if adv.crt_setups:
                        result.advanced_factors.append(f"CRT({len(adv.crt_setups)})")
                    if adv.turtle_soups:
                        result.advanced_factors.append(f"TurtleSoup({len(adv.turtle_soups)})")
                    if adv.unicorn_zones:
                        result.advanced_factors.append(f"Unicorn({len(adv.unicorn_zones)})")
                    if adv.venom_setup:
                        result.advanced_factors.append("Venom")
                    if adv.propulsion_blocks:
                        result.advanced_factors.append(f"PropBlock({len(adv.propulsion_blocks)})")
                    if adv.rejection_blocks:
                        result.advanced_factors.append(f"RejBlock({len(adv.rejection_blocks)})")
                    if adv.ipda_levels:
                        from analysis.ict.advanced import is_near_ipda_level
                        if is_near_ipda_level(result.current_price, adv.ipda_levels):
                            result.advanced_factors.append("near_IPDA")
                    if result.is_macro_time:
                        result.advanced_factors.append("MACRO_TIME")
                    if result.has_judas_swing:
                        result.advanced_factors.append(f"JudasSwing({result.judas_direction})")
                    if result.asian_range:
                        # Check if price swept Asian range (confluence with Judas)
                        if direction == Direction.BULLISH and result.current_price > result.asian_range[1]:
                            result.advanced_factors.append("AsianRange_swept_low")
                        elif direction == Direction.BEARISH and result.current_price < result.asian_range[0]:
                            result.advanced_factors.append("AsianRange_swept_high")
                    if result.ndog_count > 0:
                        result.advanced_factors.append(f"NDOG({result.ndog_count})")
                    if cbdr_data and adv.intraday_profile.value != "invalid":
                        result.advanced_factors.append(f"Profile_{adv.intraday_profile.value}")
            except Exception as e:
                print(f"  [{symbol}] Advanced ICT analysis error: {e}", flush=True)

            # -- Step 8g: Fibonacci extension TP levels --
            if range_high > range_low and direction in (Direction.BULLISH, Direction.BEARISH):
                rng = range_high - range_low
                fib_ratios = [1.272, 1.618, 2.0, 2.618]
                if direction == Direction.BULLISH:
                    result.fib_tp_levels = [round(range_low + rng * r, 5) for r in fib_ratios]
                else:
                    result.fib_tp_levels = [round(range_high - rng * r, 5) for r in fib_ratios]

            # -- Step 9: Score! --
            if score_both_directions:
                # HTF is NEUTRAL — score both directions, pick the stronger one
                best_score = None
                for try_dir in (Direction.BULLISH, Direction.BEARISH):
                    try_dol = get_draw_on_liquidity(levels, result.current_price, bias=try_dir) if levels else None
                    try_score = score_ict_setup(
                        current_price=result.current_price,
                        direction=try_dir,
                        session_info=session_info,
                        structure_events=structure_events,
                        swings=swings,
                        fvgs=fvgs,
                        obs=obs,
                        liquidity_sweeps=sweeps,
                        draw_on_liquidity=try_dol,
                        has_smt_divergence=has_smt,
                        range_high=range_high,
                        range_low=range_low,
                    )
                    if best_score is None or try_score.total > best_score.total:
                        best_score = try_score
                        dol = try_dol
                        direction = try_dir
                score = best_score
            else:
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

            # -- Step 10b: Apply advanced ICT confluence bonus --
            # Add PO3 phase and CISD as confluence factors
            if po3_result and po3_result.phase != PO3Phase.UNKNOWN:
                result.advanced_factors.insert(0, f"PO3_{po3_result.phase.value}")
            if result.has_cisd:
                result.advanced_factors.insert(0, "CISD")

            # Advanced concepts (CRT, Unicorn, CISD, etc.) add up to +10 points
            # as confluence evidence — they don't replace the core score.
            if result.advanced_factors:
                bonus = min(len(result.advanced_factors) * 2.5, 10.0)
                result.total_score = min(100, result.total_score + bonus)
                result.confluence_factors.extend(result.advanced_factors)
                result.confluence_factors.append(f"adv_bonus(+{bonus:.0f})")
                # Re-grade after bonus
                if result.total_score >= 80:
                    result.grade = "A"
                elif result.total_score >= 65:
                    result.grade = "B"
                elif result.total_score >= 50:
                    result.grade = "C"
                elif result.total_score >= 35:
                    result.grade = "D"
                else:
                    result.grade = "INVALID"

            # Confidence = normalized score (0-1)
            result.confidence = min(result.total_score / 100.0, 1.0)

            # Risk level based on grade
            if score.grade in (SignalGrade.A,):
                result.risk_level = "1% per trade (FTMO)"
            elif score.grade == SignalGrade.B:
                result.risk_level = "0.5% per trade (reduced)"
            elif score.grade == SignalGrade.C:
                result.risk_level = "0.25% per trade (pullback only)"
            else:
                result.risk_level = "SKIP"

            # LTF analysis summary + ATR
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

                # Compute ATR(14) on M15 for SL distance guidance
                if len(df_ltf) >= 15:
                    highs = df_ltf["high"].astype(float)
                    lows = df_ltf["low"].astype(float)
                    closes = df_ltf["close"].astype(float)
                    tr = pd.concat([
                        highs - lows,
                        (highs - closes.shift(1)).abs(),
                        (lows - closes.shift(1)).abs(),
                    ], axis=1).max(axis=1)
                    result.atr_m15 = round(float(tr.iloc[-14:].mean()), 5)

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

    # Price range validation uses shared config.PRICE_RANGES / config.price_in_range
    # Single source of truth — no duplicate dicts to go out of sync.

    def _price_in_range(self, symbol: str, price: float) -> bool:
        """Return False if price is outside the known valid range for this symbol."""
        return price_in_range(symbol, price)

    def _collect_data(self, symbol: str) -> dict[str, pd.DataFrame | None]:
        """
        Collect OHLCV data across H4, H1, M15 timeframes.

        Uses H4 cache to avoid unnecessary chart switches.
        Price-validates every dataframe to catch cross-symbol contamination
        (TV Desktop streams one chart — symbol switches can race with data reads).

        Returns:
            {"H4": df_or_None, "H1": df_or_None, "M15": df_or_None}
        """
        dfs: dict[str, pd.DataFrame | None] = {"H4": None, "H1": None, "M15": None}
        cfg = self.config

        # Hold exclusive chart access for the entire switch+verify+collect
        # sequence. Without this, another thread (position manager, strategy
        # engine) can switch the chart to a different symbol between our
        # set_symbol() and get_ohlcv() calls, causing cross-symbol contamination.
        with self.client.chart_session():
            return self._collect_multi_tf_locked(symbol, dfs, cfg)

    def _collect_multi_tf_locked(
        self,
        symbol: str,
        dfs: dict[str, pd.DataFrame | None],
        cfg: Any,
    ) -> dict[str, pd.DataFrame | None]:
        """Inner collect logic — called while holding chart_session lock."""
        # Switch to the correct symbol and poll until BOTH the quote symbol AND
        # the live price are consistent with the target.
        # TV Desktop lags: the quote symbol name can update before the price stream
        # stabilises, so we validate the price against known ranges as a second gate.
        target_sym = symbol.split(":")[-1]
        # Step 1: Switch symbol and wait for chart_ready (CLI polls up to 10s internally)
        # Retry the full switch+verify sequence up to 2 times on failure.
        symbol_confirmed = False
        for switch_attempt in range(2):
            try:
                result = self.client.set_symbol(symbol, require_ready=True)
                if not result.get("chart_ready", False):
                    if switch_attempt == 0:
                        time.sleep(3.0)
                        continue
                    print(f"[WARN] Chart not ready for {symbol} after set_symbol — skipping", flush=True)
                    return dfs

                # TV Desktop needs significant time to fully load OHLCV data
                # after the symbol name appears in the quote widget. The quote
                # symbol updates immediately but bars lag behind by 3-8 seconds.
                time.sleep(5.0)

                # Verify quote confirms the symbol with a live price
                quote = self.client.get_quote()
                chart_sym = quote.get("symbol", "").split(":")[-1]
                if chart_sym != target_sym:
                    if switch_attempt == 0:
                        time.sleep(3.0)
                        continue
                    print(f"[WARN] Quote symbol mismatch: expected {target_sym}, got {chart_sym} — skipping", flush=True)
                    return dfs

                live_price = float(quote.get("last") or quote.get("lp") or quote.get("close") or 0)
                if live_price <= 0:
                    if switch_attempt == 0:
                        time.sleep(3.0)
                        continue
                    print(f"[WARN] {symbol} quote returned zero price — skipping", flush=True)
                    return dfs

                # Cross-check against Alpaca live feed (primary defense for crypto)
                price_ok, alpaca_price = self.price_verifier.verify(symbol, live_price)
                if not price_ok:
                    print(
                        f"[REJECT] {symbol}: TV price {live_price:.4f} doesn't match "
                        f"Alpaca {alpaca_price:.4f} — contaminated data, skipping",
                        flush=True,
                    )
                    return dfs

                # Safety net: price range check (catches forex/commodities not on Alpaca)
                if not self._price_in_range(symbol, live_price):
                    print(
                        f"[WARN] {symbol}: symbol confirmed but price {live_price:.4f} "
                        f"fails range check — data may be stale, skipping",
                        flush=True,
                    )
                    return dfs

                # Store verified price for cross-checking OHLCV data
                self._last_verified_price = live_price
                symbol_confirmed = True
                break

            except TVClientError as e:
                if switch_attempt == 0:
                    time.sleep(3.0)
                    continue
                print(f"[WARN] Failed to switch to {symbol}: {e}", flush=True)
                return dfs

        if not symbol_confirmed:
            return dfs

        # Chart is confirmed: symbol name matches, price verified against Alpaca, in range.
        # All subsequent OHLCV fetches switch timeframe only (symbol is locked).

        def _fetch_tf(timeframe: str, count: int) -> pd.DataFrame | None:
            """Switch timeframe (symbol already confirmed) and fetch verified OHLCV."""
            try:
                tf_result = self.client.set_timeframe(timeframe)
                # Always give chart time to load new timeframe data
                wait = 4.0 if not tf_result.get("chart_ready", True) else 2.5
                time.sleep(wait)

                # Read OHLCV with post-read symbol verification
                raw = self.client.get_ohlcv_verified(symbol, count=count)
                if raw is None:
                    # Symbol drift detected during OHLCV read
                    return None

                df = bars_to_dataframe(raw)
                valid, _ = validate_dataframe(df)
                if not valid:
                    return None

                # Comprehensive price range validation — check multiple bars
                # to catch contamination even when ranges partially overlap.
                if not df.empty:
                    check_prices = [
                        float(df["close"].iloc[-1]),
                        float(df["high"].iloc[-1]),
                        float(df["low"].iloc[-1]),
                    ]
                    if len(df) > 10:
                        check_prices.append(float(df["close"].iloc[len(df)//2]))
                    for p in check_prices:
                        if p > 0 and not self._price_in_range(symbol, p):
                            print(
                                f"[WARN] OHLCV contamination on {symbol} {timeframe}: "
                                f"price {p:.4f} is out of valid range — discarding",
                                flush=True,
                            )
                            return None

                    # Cross-check: OHLCV prices should be close to the verified
                    # live quote price (within 5% for same-day data).
                    last_close = float(df["close"].iloc[-1])
                    if hasattr(self, '_last_verified_price') and self._last_verified_price > 0:
                        deviation = abs(last_close - self._last_verified_price) / self._last_verified_price
                        if deviation > 0.05:
                            print(
                                f"[WARN] OHLCV data mismatch on {symbol} {timeframe}: "
                                f"bars show {last_close:.4f} but verified price is "
                                f"{self._last_verified_price:.4f} ({deviation:.1%} off) — discarding",
                                flush=True,
                            )
                            return None

                return df
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

        # Store for reuse by other components (e.g. StrategyEngine)
        # Map internal TF labels → TV resolution strings
        self._last_collected_dfs = {}
        tf_to_tv = {"H4": cfg.htf, "H1": cfg.itf, "M15": cfg.ltf}
        for label, df_val in dfs.items():
            if df_val is not None:
                tv_tf = tf_to_tv.get(label, label)
                self._last_collected_dfs[tv_tf] = df_val

        return dfs

    def _get_smt_data(self, smt_symbol: str) -> pd.DataFrame | None:
        """Fetch OHLCV for a correlated symbol (for SMT divergence check)."""
        try:
            tv_symbol = self.config.tv_symbol(smt_symbol)
            with self.client.chart_session():
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
