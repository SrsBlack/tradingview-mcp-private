"""
Full ICT Analysis Pipeline — MT5-primary OHLCV feed into the trading-ai-v2 ICT scorer.

Data source (as of 2026-04-23):
  PRIMARY:  MT5 via bridge.mt5_data.MT5DataCollector (fast, multi-symbol, no chart switching).
  FALLBACK: TradingView Desktop via CDP (bridge.tv_client) if MT5 returns insufficient bars.

For each symbol:
  1. Collect OHLCV across W1/D1 (context), H4 (bias), H1 (intermediate), M15 (trigger)
  2. Run structure, FVG, OB, liquidity, session, SMT, intermarket analysis
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


def _resample_ohlcv(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Resample intraday OHLCV to a higher timeframe (D=daily, W=weekly).

    Args:
        df: OHLCV DataFrame with DatetimeIndex
        rule: Pandas resample rule ('D' for daily, 'W' for weekly)

    Returns:
        Resampled DataFrame with OHLCV columns, dropping empty periods.
    """
    if df.empty:
        return df
    resampled = df.resample(rule).agg({
        'open': 'first',
        'high': 'max',
        'low': 'min',
        'close': 'last',
        'volume': 'sum',
    }).dropna(subset=['open'])
    return resampled
from bridge.price_verify import PriceVerifier
from bridge.tv_client import TVClient, TVClientError
from bridge.tv_data_adapter import bars_to_dataframe, validate_dataframe

# Ensure trading-ai-v2 is importable
ensure_trading_ai_path()

from analysis.structure import detect_swings, classify_structure, get_current_bias, detect_mss, SwingPoint, StructureEvent
from analysis.fvg import detect_fvgs, get_active_fvgs, price_in_fvg, get_ce_level, price_near_ce, detect_fvg_stacks, get_reload_zone, detect_implied_fvgs, detect_rdrb, FVGZone
from analysis.order_blocks import detect_order_blocks, get_active_obs, detect_hidden_obs, OrderBlock
from analysis.liquidity import scan_sweeps, get_draw_on_liquidity, swing_to_liquidity, build_liquidity_map, detect_equal_levels_clustered, get_equal_level_targets, LiquidityLevel, LiquiditySweep
from analysis.sessions import get_session_info, SessionInfo
from analysis.smt import detect_smt_divergence, SMT_PAIRS
from analysis.ict.scorer import score_ict_setup, ICTScoreBreakdown, get_pd_zone, pd_aligned_with_bias
from analysis.ict.core import detect_cisd, get_latest_cisd, detect_po3_phase, PO3Phase, detect_judas_swing, JudasSwing
from analysis.ict.advanced import run_advanced_analysis, AdvancedAnalysis, detect_market_maker_model, detect_suspension_blocks, get_quarterly_bias
from analysis.sessions import get_asian_range, get_ndog, get_cbdr, get_midnight_range, get_weekly_bias, is_seek_and_destroy
from analysis.volume_profile import build_volume_profile
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

    # Dealing range (for SL liquidity zone check)
    range_high: float = 0.0
    range_low: float = 0.0
    swing_lows: list[float] = field(default_factory=list)  # recent M15 swing lows
    swing_highs: list[float] = field(default_factory=list)  # recent M15 swing highs

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
    nwog_count: int = 0  # New Week Opening Gaps detected
    key_opens: dict | None = None  # D_OPEN, W_OPEN, M_OPEN, Q_OPEN prices
    cbdr_range: tuple[float, float] | None = None
    advanced_score: float = 0.0
    advanced_factors: list[str] = field(default_factory=list)

    # Volume profile (POC/VAH/VAL/HVN/LVN on M15 — wired 2026-04-26).
    # vp_hvn_zones / vp_lvn_zones are list of (low_price, high_price) tuples
    # derived from VolumeNode midpoints +/- bucket_width/2 so overlap checks
    # can use simple range arithmetic.
    vp_poc: float = 0.0
    vp_vah: float = 0.0
    vp_val: float = 0.0
    vp_hvn_zones: list[tuple[float, float]] = field(default_factory=list)
    vp_lvn_zones: list[tuple[float, float]] = field(default_factory=list)

    # Active OB zones on M15 — list of (bottom, top) tuples, exposed so
    # synergy_scorer can do real OB-overlaps-HVN-bucket checks instead of
    # the legacy FVG_stack/HiddenOB proxy.
    ob_zones: list[tuple[float, float]] = field(default_factory=list)

    # Liquidity voids — LVN zones (low_price, high_price) that have not been
    # traversed by recent price action and now sit as magnets above/below the
    # market. Voids ABOVE current price = bullish draw (price likely fills up).
    # Voids BELOW current price = bearish draw (price likely fills down).
    # Derived from vp_lvn_zones in step 8e7. Empty list when volume profile
    # didn't run (df too short or all buckets had volume).
    liquidity_voids: list[tuple[float, float]] = field(default_factory=list)

    # Optimal FVG entry zone (CE price for limit order targeting)
    fvg_entry_price: float = 0.0  # CE of nearest retracement FVG
    fvg_entry_zone: str = ""      # Description: "FVG 24160-24185, CE=24172"

    # Fibonacci extension TP levels
    fib_tp_levels: list[float] = field(default_factory=list)

    # Synergy & gate explanations (from cross_correlations.json evaluation)
    synergy_explanations: list[str] = field(default_factory=list)
    gate_violations: list[str] = field(default_factory=list)

    # IPDA & Quarterly Shift
    ipda_ranges: dict | None = None  # IPDA 20/40/60-day ranges
    quarterly_shift: dict | None = None  # Quarterly shift detection

    # HTF FVG obstacle detection
    htf_fvg_obstacle: bool = False  # Price is inside or near an opposing HTF FVG
    htf_fvg_obstacle_zone: str = ""  # Description of the opposing FVG zone

    # HTF (H4) dealing range for macro premium/discount assessment
    htf_range_high: float = 0.0  # H4 dealing range high
    htf_range_low: float = 0.0   # H4 dealing range low
    htf_pd_zone: str = ""        # Premium/discount on H4 dealing range
    htf_pd_aligned: bool = False  # P/D alignment on H4 range

    # Multi-timeframe bias alignment
    w1_bias: str = ""         # Weekly structure bias
    d1_bias: str = ""         # Daily structure bias
    mtf_aligned: bool = False  # All timeframes agree on direction
    mtf_alignment: str = ""   # "W1:BULL D1:BULL H4:BULL" summary

    # HTF pullback detection — is the higher timeframe actively retracing?
    htf_pullback_active: bool = False  # True = H4 making consecutive lower/higher closes against bias
    htf_pullback_bars: int = 0         # How many consecutive pullback bars

    # Intermarket context (DXY, US10Y, VIX)
    intermarket_conflict: bool = False
    intermarket_explanation: str = ""
    vix_risk_multiplier: float = 1.0

    # Economic calendar (news blackout)
    news_blackout: bool = False
    news_event: str = ""
    news_minutes: int = 0

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
                "nwog_count": self.nwog_count,
                "key_opens": self.key_opens or {},
                "cbdr_range": list(self.cbdr_range) if self.cbdr_range else None,
                "advanced_score": round(self.advanced_score, 1),
                "advanced_factors": self.advanced_factors,
                "fib_tp_levels": [round(l, 5) for l in self.fib_tp_levels],
                "ipda_ranges": self.ipda_ranges,
                "quarterly_shift": self.quarterly_shift,
                "htf_fvg_obstacle": self.htf_fvg_obstacle,
                "htf_fvg_obstacle_zone": self.htf_fvg_obstacle_zone,
                "htf_range_high": self.htf_range_high,
                "htf_range_low": self.htf_range_low,
                "htf_pd_zone": self.htf_pd_zone,
                "htf_pd_aligned": self.htf_pd_aligned,
                "w1_bias": self.w1_bias,
                "d1_bias": self.d1_bias,
                "mtf_aligned": self.mtf_aligned,
                "mtf_alignment": self.mtf_alignment,
                "intermarket_conflict": self.intermarket_conflict,
                "intermarket_explanation": self.intermarket_explanation,
                "vix_risk_multiplier": self.vix_risk_multiplier,
                "news_blackout": self.news_blackout,
                "news_event": self.news_event,
                "volume_profile": {
                    "poc": round(self.vp_poc, 5) if self.vp_poc else 0.0,
                    "vah": round(self.vp_vah, 5) if self.vp_vah else 0.0,
                    "val": round(self.vp_val, 5) if self.vp_val else 0.0,
                    "hvn_count": len(self.vp_hvn_zones),
                    "lvn_count": len(self.vp_lvn_zones),
                    "void_count": len(self.liquidity_voids),
                },
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

    Fetches multi-timeframe OHLCV from MT5 (primary) or TradingView (fallback),
    and runs the complete trading-ai-v2 ICT scoring chain.
    """

    def __init__(self, config: BridgeConfig | None = None, client: TVClient | None = None):
        self.config = config or get_bridge_config()
        self.client = client or TVClient()
        self.price_verifier = PriceVerifier()

        # MT5 data collector — primary data source (no chart switching needed)
        try:
            from bridge.mt5_data import MT5DataCollector
            self._mt5_data = MT5DataCollector(self.config)
            self._use_mt5 = True
            print("[ICT] Using MT5 for OHLCV data (no TradingView chart switching)", flush=True)
        except Exception:
            self._mt5_data = None
            self._use_mt5 = False

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

            df_w1 = dfs.get("W1")   # Weekly — real MT5 data (not resampled)
            df_d1 = dfs.get("D1")   # Daily — real MT5 data (not resampled)
            df_htf = dfs.get("H4")
            df_itf = dfs.get("H1")
            df_ltf = dfs.get("M15")

            # Debug: log bar counts for each timeframe
            h4_bars = len(df_htf) if df_htf is not None else 0
            h1_bars = len(df_itf) if df_itf is not None else 0
            m15_bars = len(df_ltf) if df_ltf is not None else 0
            if h4_bars + h1_bars + m15_bars > 0:
                print(f"  [{symbol}] OHLCV: H4={h4_bars} H1={h1_bars} M15={m15_bars} bars", flush=True)

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
            # Exclude the last H4 bar (still forming) to prevent phantom
            # swings/structure from incomplete candles. M15 is fine as-is
            # because 15-min candles close quickly.
            df_htf_closed = df_htf.iloc[:-1] if (df_htf is not None and len(df_htf) > 20) else df_htf
            df_structure = df_htf_closed if (df_htf_closed is not None and len(df_htf_closed) >= 20) else df_primary

            # H4 swing lookback = 5. KEEP IN SYNC with bridge/live_executor_adapter.py
            # _get_tf_bias `tf_config["H4"]` — entry and exit must compute bias on
            # identical swings, otherwise a trade entered as BULLISH can be killed
            # as BEARISH 30 minutes later by a different lookback.
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

            # -- Step 2a2: HTF pullback detection --
            # ICT: Don't enter during an active pullback. Wait for the pullback
            # to complete (sweep + displacement) before entering.
            # Detect: if H4 bias is BULLISH but last 3+ H4 closes are descending,
            # the pullback is still in progress. Same for BEARISH + ascending closes.
            if df_htf_closed is not None and len(df_htf_closed) >= 5:
                h4_closes = df_htf_closed["close"].iloc[-5:].tolist()
                consecutive_down = 0
                consecutive_up = 0
                for i in range(1, len(h4_closes)):
                    if h4_closes[i] < h4_closes[i-1]:
                        consecutive_down += 1
                        consecutive_up = 0
                    elif h4_closes[i] > h4_closes[i-1]:
                        consecutive_up += 1
                        consecutive_down = 0
                    else:
                        consecutive_down = 0
                        consecutive_up = 0

                # Bullish bias but price making lower closes = pullback in progress
                if htf_bias == Direction.BULLISH and consecutive_down >= 3:
                    result.htf_pullback_active = True
                    result.htf_pullback_bars = consecutive_down
                    result.advanced_factors.append(f"HTF_pullback({consecutive_down}_bars_down)")
                # Bearish bias but price making higher closes = pullback in progress
                elif htf_bias == Direction.BEARISH and consecutive_up >= 3:
                    result.htf_pullback_active = True
                    result.htf_pullback_bars = consecutive_up
                    result.advanced_factors.append(f"HTF_pullback({consecutive_up}_bars_up)")

            # Use HTF bias as the directional context
            # When NEUTRAL, we'll score both directions and pick the stronger one
            direction = htf_bias
            score_both_directions = (htf_bias == Direction.NEUTRAL)

            # -- Step 2b: Weekly and Daily structure --
            # Use REAL D1/W1 data from MT5 (preferred) or resample from H4 (fallback)
            w1_bias = Direction.NEUTRAL
            d1_bias = Direction.NEUTRAL
            d1_fvgs: list[FVGZone] = []  # Daily FVGs — critical for ICT
            try:
                # Daily structure — real D1 bars from MT5
                df_daily = df_d1.iloc[:-1] if df_d1 is not None and len(df_d1) > 10 else None
                if df_daily is None and df_htf_closed is not None and len(df_htf_closed) >= 30:
                    df_daily = _resample_ohlcv(df_htf_closed, 'D')  # fallback

                if df_daily is not None and len(df_daily) >= 10:
                    # D1 swing lookback = 3. KEEP IN SYNC with bridge/live_executor_adapter.py
                    # _get_tf_bias `tf_config["D1"]`.
                    d1_swings = detect_swings(df_daily, lookback=3)
                    _, d1_events = classify_structure(d1_swings)
                    d1_bias = get_current_bias(d1_events)
                    result.d1_bias = d1_bias.name

                    # Daily FVGs — ICT says these are the macro zones price respects
                    d1_fvgs = detect_fvgs(df_daily, max_age_bars=20, min_quality=FVGQuality.DEFENSIVE)
                    if d1_fvgs:
                        result.advanced_factors.append(f"D1_FVGs({len(d1_fvgs)})")

                # Weekly structure — real W1 bars from MT5
                df_weekly = df_w1.iloc[:-1] if df_w1 is not None and len(df_w1) > 4 else None
                if df_weekly is None and df_htf_closed is not None and len(df_htf_closed) >= 30:
                    df_weekly = _resample_ohlcv(df_htf_closed, 'W')  # fallback

                if df_weekly is not None and len(df_weekly) >= 4:
                    # W1 swing lookback = 2. KEEP IN SYNC with bridge/live_executor_adapter.py
                    # _get_tf_bias `tf_config["W1"]`.
                    w1_swings = detect_swings(df_weekly, lookback=2)
                    _, w1_events = classify_structure(w1_swings)
                    w1_bias = get_current_bias(w1_events)
                    result.w1_bias = w1_bias.name

                    # Weekly FVGs
                    w1_fvgs = detect_fvgs(df_weekly, max_age_bars=10, min_quality=FVGQuality.DEFENSIVE)
                    if w1_fvgs:
                        result.advanced_factors.append(f"W1_FVGs({len(w1_fvgs)})")
            except Exception as e:
                print(f"  [{symbol}] W1/D1 analysis error: {e}", flush=True)

            # Multi-timeframe alignment check
            biases = {
                "W1": w1_bias if w1_bias != Direction.NEUTRAL else None,
                "D1": d1_bias if d1_bias != Direction.NEUTRAL else None,
                "H4": htf_bias if htf_bias != Direction.NEUTRAL else None,
            }
            active_biases = {k: v for k, v in biases.items() if v is not None}
            if active_biases:
                all_same = len(set(active_biases.values())) == 1
                result.mtf_aligned = all_same and direction in active_biases.values()
                parts = [f"{k}:{v.name}" for k, v in biases.items() if v is not None]
                result.mtf_alignment = " ".join(parts) if parts else ""
                if result.mtf_aligned:
                    result.advanced_factors.append("MTF_aligned")
                elif len(active_biases) >= 2 and not all_same:
                    result.advanced_factors.append("MTF_conflict")

            # -- Step 3: FVG detection (on M15 for precision) --
            fvgs: list[FVGZone] = []
            df_fvg = df_ltf if (df_ltf is not None and len(df_ltf) >= 10) else df_primary
            if df_fvg is not None and len(df_fvg) >= 10:
                fvgs = detect_fvgs(df_fvg, max_age_bars=50, min_quality=FVGQuality.VERY_AGGRESSIVE)

            # -- Step 3b: HTF FVG detection (H4 for macro obstacles) --
            # CRITICAL: exclude the last H4 bar — it's still forming.
            # An FVG requires 3 COMPLETED candles. Using the forming candle
            # creates phantom FVGs that don't actually exist yet.
            # (ETH 2300-2316 phantom FVG bug, 2026-04-23)
            htf_fvgs: list[FVGZone] = []
            if df_htf_closed is not None and len(df_htf_closed) >= 10:
                htf_fvgs = detect_fvgs(df_htf_closed, max_age_bars=30, min_quality=FVGQuality.DEFENSIVE)

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
                # Surface active OB price ranges for downstream synergy checks
                # (OB-overlaps-HVN, etc). Stored as (bottom, top) tuples.
                try:
                    active_obs = get_active_obs(obs)
                    result.ob_zones = [
                        (float(ob.bottom), float(ob.top)) for ob in active_obs
                    ]
                except Exception:
                    pass

            # -- Step 5: Liquidity detection --
            sweeps: list[LiquiditySweep] = []
            dol: LiquidityLevel | None = None
            levels: list = []
            df_liq = df_ltf if (df_ltf is not None and len(df_ltf) >= 20) else df_primary
            if df_liq is not None and len(df_liq) >= 20:
                liq_swings = detect_swings(df_liq, lookback=5)
                # Build comprehensive liquidity map: swings + PDH/PDL + PWH/PWL + equal levels
                levels = build_liquidity_map(df_liq, liq_swings)
                # Extract key institutional opens for the Claude prompt
                result.key_opens = {
                    lv.source: lv.price for lv in levels
                    if lv.source in ("D_OPEN", "W_OPEN", "M_OPEN", "Q_OPEN")
                }
                # Scan recent bars for sweeps (64 bars = 16 hours on M15 — covers London→NY)
                sweeps = scan_sweeps(levels, df_liq, lookback_bars=64)
                result.sweep_detected = len(sweeps) > 0

                # Filter: only significant sweeps count (PDH/PDL, PWH/PWL, Asian, equal H/L)
                # Minor swing sweeps are noise — ICT requires sweeping a KNOWN level.
                if sweeps:
                    significant_sources = {
                        "PDH", "PDL", "PWH", "PWL", "PMH", "PML",
                        "equal_highs", "equal_lows", "session_high", "session_low",
                        "D_OPEN", "W_OPEN", "M_OPEN", "Q_OPEN",
                    }
                    significant_sweeps = [
                        s for s in sweeps if s.level.source in significant_sources
                    ]
                    result.sweep_detected = len(significant_sweeps) > 0
                    if significant_sweeps:
                        swept_sources = {s.level.source for s in significant_sweeps}
                        result.advanced_factors.append(
                            "sweep_of_" + "+".join(sorted(swept_sources))
                        )
                    else:
                        # Had sweeps but none were significant — downgrade to minor
                        result.advanced_factors.append("minor_sweep_only")
                    # Replace sweeps list so displacement check uses only significant sweeps
                    sweeps = significant_sweeps

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

                    # ICT 2022 model: displacement requires structure shift too
                    # Check if structure_score indicates CHoCH/BOS occurred
                    if result.displacement_confirmed and result.structure_score < 15:
                        # Displacement without strong structure = partial confirmation only
                        result.advanced_factors.append("displacement_no_structure")
                        # Don't un-confirm displacement, but flag it as weaker

                # Add H4 liquidity levels for macro targets (DOL)
                # Use closed H4 bars only — forming candle creates phantom levels
                if df_htf_closed is not None and len(df_htf_closed) >= 20:
                    htf_liq_swings = detect_swings(df_htf_closed, lookback=5)
                    htf_levels = build_liquidity_map(df_htf_closed, htf_liq_swings)
                    # Merge HTF levels into the main map (dedup by price proximity)
                    for lv in htf_levels:
                        if not any(
                            abs(existing.price - lv.price) <= abs(lv.price * 0.001)
                            and existing.liquidity_type == lv.liquidity_type
                            for existing in levels
                        ):
                            levels.append(lv)

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

            # Store dealing range + swing levels for SL liquidity zone check
            result.range_high = range_high
            result.range_low = range_low
            result.swing_lows = [float(s.price) for s in ltf_lows[-5:]] if ltf_lows else []
            result.swing_highs = [float(s.price) for s in ltf_highs[-5:]] if ltf_highs else []

            # -- Step 8a2: HTF (H4) dealing range for macro premium/discount --
            # ICT methodology: H4 defines the macro dealing range.
            # M15 range is for OTE entry precision, H4 range is for zone assessment.
            if df_htf_closed is not None and len(df_htf_closed) >= 20:
                htf_swing_list = detect_swings(df_htf_closed, lookback=5)
                htf_highs = [s for s in htf_swing_list if s.swing_type == "swing_high"]
                htf_lows = [s for s in htf_swing_list if s.swing_type == "swing_low"]
                if htf_highs and htf_lows:
                    result.htf_range_high = float(max(s.price for s in htf_highs[-3:]))
                    result.htf_range_low = float(min(s.price for s in htf_lows[-3:]))
                else:
                    result.htf_range_high = float(df_htf_closed["high"].max())
                    result.htf_range_low = float(df_htf_closed["low"].min())

                if result.htf_range_high > result.htf_range_low:
                    result.htf_pd_zone = get_pd_zone(result.current_price, result.htf_range_high, result.htf_range_low)
                    result.htf_pd_aligned = pd_aligned_with_bias(
                        result.current_price, result.htf_range_high, result.htf_range_low, direction
                    )

            # -- Step 8b: Premium/Discount zone --
            if range_high > range_low:
                result.pd_zone = get_pd_zone(result.current_price, range_high, range_low)
                result.pd_aligned = pd_aligned_with_bias(
                    result.current_price, range_high, range_low, direction
                )

            # -- Step 8b2: HTF FVG obstacle check --
            # A bullish trade entering a bearish HTF FVG = buying into resistance
            # A bearish trade entering a bullish HTF FVG = selling into support
            if htf_fvgs and direction != Direction.NEUTRAL:
                for fvg in htf_fvgs:
                    # Check if price is inside or approaching an opposing FVG
                    opposing = (
                        (direction == Direction.BULLISH and fvg.direction == Direction.BEARISH) or
                        (direction == Direction.BEARISH and fvg.direction == Direction.BULLISH)
                    )
                    if opposing:
                        # Price is inside the opposing FVG
                        if fvg.bottom <= result.current_price <= fvg.top:
                            result.htf_fvg_obstacle = True
                            result.htf_fvg_obstacle_zone = f"INSIDE bearish H4 FVG {fvg.bottom:.2f}-{fvg.top:.2f}"
                            result.advanced_factors.append("HTF_FVG_obstacle")
                            break
                        # Price is approaching the opposing FVG (within 0.5% of price)
                        dist_to_fvg = min(abs(result.current_price - fvg.bottom), abs(result.current_price - fvg.top))
                        proximity_pct = dist_to_fvg / result.current_price * 100
                        if proximity_pct < 0.5:
                            if (direction == Direction.BULLISH and fvg.bottom > result.current_price) or \
                               (direction == Direction.BEARISH and fvg.top < result.current_price):
                                result.htf_fvg_obstacle = True
                                result.htf_fvg_obstacle_zone = f"APPROACHING bearish H4 FVG {fvg.bottom:.2f}-{fvg.top:.2f} ({proximity_pct:.2f}% away)"
                                result.advanced_factors.append("HTF_FVG_nearby")
                                break

            # -- Step 8b3: Daily FVG check --
            # D1 FVGs are the MOST important macro zones in ICT. Price gravitates
            # toward unfilled daily FVGs. If price is inside a D1 FVG that aligns
            # with our direction, it's a high-conviction entry zone.
            if d1_fvgs and direction != Direction.NEUTRAL:
                for fvg in d1_fvgs:
                    if fvg.bottom <= result.current_price <= fvg.top:
                        if fvg.direction == direction:
                            # Price inside a D1 FVG in OUR direction = strong support
                            result.advanced_factors.append(
                                f"inside_D1_FVG({fvg.direction.name} {fvg.bottom:.2f}-{fvg.top:.2f})"
                            )
                        else:
                            # Price inside an opposing D1 FVG = macro resistance
                            result.advanced_factors.append(
                                f"D1_FVG_obstacle({fvg.direction.name} {fvg.bottom:.2f}-{fvg.top:.2f})"
                            )
                        break
                    # Check proximity — price approaching a D1 FVG = draw on liquidity
                    dist_pct = min(
                        abs(result.current_price - fvg.bottom),
                        abs(result.current_price - fvg.top)
                    ) / result.current_price * 100
                    if dist_pct < 1.0:  # Within 1% of a D1 FVG
                        ce = (fvg.top + fvg.bottom) / 2
                        result.advanced_factors.append(
                            f"near_D1_FVG({fvg.direction.name} CE={ce:.2f}, {dist_pct:.1f}% away)"
                        )
                        break

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

            # -- Step 8e5: NWOG (New Week Opening Gaps) --
            if df_primary is not None and len(df_primary) >= 96:
                try:
                    from analysis.sessions import get_nwog
                    nwogs = get_nwog(df_primary)
                    result.nwog_count = len(nwogs)
                except Exception:
                    pass

            # -- Step 8e6: CBDR (Central Bank Dealers Range) --
            cbdr_data = None
            if df_primary is not None and len(df_primary) >= 20:
                try:
                    cbdr_data = get_cbdr(df_primary)
                    if cbdr_data:
                        result.cbdr_range = cbdr_data
                except Exception:
                    pass

            # -- Step 8e7: Volume profile (POC/VAH/VAL + HVN/LVN zones on M15).
            # Window: last 96 M15 bars (~24h). 30 buckets = default.
            # Zones are converted from bucket midpoints to (low, high) tuples
            # using bucket_width = (price_max - price_min) / buckets so that
            # overlap checks (OB-at-HVN, void-detection) use range arithmetic.
            if df_primary is not None and len(df_primary) >= 20:
                try:
                    vp_window = df_primary.iloc[-96:] if len(df_primary) >= 96 else df_primary
                    vp_buckets = 30
                    vp = build_volume_profile(vp_window, buckets=vp_buckets)
                    if vp is not None:
                        vp_price_min = float(vp_window["low"].min())
                        vp_price_max = float(vp_window["high"].max())
                        vp_bucket_width = (vp_price_max - vp_price_min) / vp_buckets
                        half = vp_bucket_width / 2.0
                        result.vp_poc = vp.poc
                        result.vp_vah = vp.vah
                        result.vp_val = vp.val
                        result.vp_hvn_zones = [
                            (n.price - half, n.price + half) for n in vp.hvns
                        ]
                        result.vp_lvn_zones = [
                            (n.price - half, n.price + half) for n in vp.lvns
                        ]
                        # Liquidity voids: LVN zones that sit clearly above or
                        # below current price (so they act as magnets).
                        # Zones that contain current price are already being
                        # traversed and aren't useful "draw" targets.
                        cp = result.current_price
                        if cp > 0:
                            result.liquidity_voids = [
                                (lo, hi) for (lo, hi) in result.vp_lvn_zones
                                if hi < cp or lo > cp
                            ]
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
                    if result.nwog_count > 0:
                        result.advanced_factors.append(f"NWOG({result.nwog_count})")
                    if cbdr_data and adv.intraday_profile.value != "invalid":
                        result.advanced_factors.append(f"Profile_{adv.intraday_profile.value}")

                # -- Step 8f2: New ICT concepts --
                # MSS (Market Structure Shift) — CHoCH + displacement = strongest reversal signal
                mss_events = detect_mss(structure_events, df_structure)
                if mss_events:
                    latest_mss = mss_events[-1]
                    if latest_mss.direction == direction:
                        result.advanced_factors.append(f"MSS({latest_mss.displacement_ratio:.1f}x)")

                # Consequent Encroachment — price near FVG midpoint = highest probability entry
                active_fvgs = get_active_fvgs(fvgs, result.current_price) if fvgs else []
                for fvg in active_fvgs[:3]:
                    if price_near_ce(result.current_price, fvg):
                        result.advanced_factors.append("CE_entry")
                        break

                # Optimal FVG entry zone — prefer FVG+OB overlap (institutional zone)
                # ICT: price often skips the nearest FVG and retraces deeper
                # into the FVG that overlaps with an Order Block.
                # Priority: 1) FVG+OB overlap, 2) FVG in OTE zone, 3) nearest FVG
                if fvgs and direction != Direction.NEUTRAL:
                    retracement_fvgs = []
                    for fvg in fvgs:
                        ce = get_ce_level(fvg)
                        is_retracement = (
                            (direction == Direction.BULLISH and fvg.direction == Direction.BULLISH and ce < result.current_price) or
                            (direction == Direction.BEARISH and fvg.direction == Direction.BEARISH and ce > result.current_price)
                        )
                        if not is_retracement:
                            continue

                        # Score this FVG: OB overlap = highest priority
                        priority = 0
                        ob_overlap = False
                        for ob in obs:
                            # Check if FVG overlaps with OB (price ranges intersect)
                            if ob.bottom <= fvg.top and ob.top >= fvg.bottom:
                                ob_overlap = True
                                priority = 3  # Highest: FVG+OB stack
                                break

                        # Check if FVG is in OTE zone (0.618-0.786 retracement)
                        if not ob_overlap and range_high > range_low:
                            rng = range_high - range_low
                            if direction == Direction.BULLISH:
                                ote_low = range_low + rng * 0.618
                                ote_high = range_low + rng * 0.786
                            else:
                                ote_low = range_high - rng * 0.786
                                ote_high = range_high - rng * 0.618
                            if ote_low <= ce <= ote_high:
                                priority = 2  # Medium: FVG in OTE zone

                        if priority == 0:
                            priority = 1  # Lowest: plain FVG

                        retracement_fvgs.append((priority, abs(result.current_price - ce), ce, fvg, ob_overlap))

                    if retracement_fvgs:
                        # Sort by priority (highest first), then by distance (nearest first)
                        retracement_fvgs.sort(key=lambda x: (-x[0], x[1]))
                        _, _, best_ce, best_fvg, has_ob = retracement_fvgs[0]
                        result.fvg_entry_price = best_ce
                        label = "FVG+OB" if has_ob else "FVG"
                        result.fvg_entry_zone = f"{label} {best_fvg.bottom:.2f}-{best_fvg.top:.2f}, CE={best_ce:.2f}"

                # FVG Stacking — overlapping FVGs = stronger institutional zone
                stacks = detect_fvg_stacks(fvgs) if len(fvgs) >= 2 else []
                for stack in stacks:
                    if stack.combined_low <= result.current_price <= stack.combined_high:
                        result.advanced_factors.append(f"FVG_stack({stack.strength})")
                        break

                # Equal Highs/Lows — liquidity targets institutions hunt
                eq_levels = detect_equal_levels_clustered(swings) if swings else []
                for eq in eq_levels:
                    dist_pct = abs(result.current_price - eq.price) / result.current_price
                    if dist_pct < 0.01:
                        result.advanced_factors.append(f"EQ_{eq.level_type}({eq.count})")
                        break

                # Market Maker Model — full institutional cycle detection
                mm_model = detect_market_maker_model(df_primary, swings, sweeps, direction) if df_primary is not None and len(df_primary) >= 30 else None
                if mm_model and mm_model.confidence >= 0.6:
                    result.advanced_factors.append(f"MM_{mm_model.model_type}({mm_model.confidence:.0%})")

                # Implied FVG — hidden body-to-body gaps (exclude forming candle)
                ifvgs = detect_implied_fvgs(df_primary.iloc[:-1]) if df_primary is not None and len(df_primary) >= 10 else []
                if ifvgs:
                    for ifvg in ifvgs[-3:]:
                        if ifvg.bottom <= result.current_price <= ifvg.top:
                            result.advanced_factors.append("IFVG")
                            break

                # Suspension Blocks — mini-consolidation within displacement
                sus_blocks = detect_suspension_blocks(df_primary) if df_primary is not None and len(df_primary) >= 20 else []
                if sus_blocks:
                    for sb in sus_blocks[-3:]:
                        if sb.low <= result.current_price <= sb.high and sb.direction == direction:
                            result.advanced_factors.append("SuspBlock")
                            break

                # RDRB — previously filled FVGs retested as support/resistance
                rdrbs = detect_rdrb(fvgs, df_primary) if fvgs and df_primary is not None else []
                if rdrbs:
                    for rdrb in rdrbs[-3:]:
                        if rdrb.bottom <= result.current_price <= rdrb.top:
                            result.advanced_factors.append("RDRB")
                            break

                # Hidden Order Blocks — wick-based institutional rejection
                hidden_obs = detect_hidden_obs(df_primary, obs) if df_primary is not None and obs else []
                if hidden_obs:
                    for hob in hidden_obs[-3:]:
                        hob_mid = (hob.top + hob.bottom) / 2
                        dist = abs(result.current_price - hob_mid) / result.current_price
                        if dist < 0.005:
                            result.advanced_factors.append("HiddenOB")
                            break

                # Midnight Range — daily bias framework
                mid_range = get_midnight_range(df_primary) if df_primary is not None else None
                if mid_range:
                    if direction == Direction.BULLISH and result.current_price > mid_range[1]:
                        result.advanced_factors.append("above_MidnightRange")
                    elif direction == Direction.BEARISH and result.current_price < mid_range[0]:
                        result.advanced_factors.append("below_MidnightRange")

                # Weekly Bias — Sunday-to-Wednesday directional intent
                weekly_dir = get_weekly_bias(df_primary) if df_primary is not None and len(df_primary) >= 96 else Direction.NEUTRAL
                if weekly_dir == direction and weekly_dir != Direction.NEUTRAL:
                    result.advanced_factors.append(f"WeeklyBias_{weekly_dir.name}")

                # IPDA Ranges — institutional 20/40/60-day delivery targets
                if df_htf_closed is not None and len(df_htf_closed) >= 60:
                    try:
                        from analysis.liquidity import get_ipda_ranges
                        result.ipda_ranges = get_ipda_ranges(df_htf_closed)
                    except Exception:
                        pass

                # Quarterly Shift — largest-scale directional signal
                if df_htf_closed is not None and len(df_htf_closed) >= 96:
                    try:
                        from analysis.sessions import detect_quarterly_shift
                        q_shift = detect_quarterly_shift(df_htf_closed)
                        if q_shift:
                            result.quarterly_shift = q_shift
                            result.advanced_factors.append(
                                f"QShift_{q_shift['direction']}({q_shift['strength']})"
                            )
                    except Exception:
                        pass

                # Quarterly Theory — seasonal institutional cycle
                q_bias = get_quarterly_bias(datetime.now(timezone.utc))
                result.advanced_factors.append(f"Q_{q_bias}")

                # Seek & Destroy Friday — high-impact event day
                is_sd, sd_event = is_seek_and_destroy(datetime.now(timezone.utc))
                if is_sd:
                    result.advanced_factors.append(f"SeekDestroy({sd_event})")

            except Exception as e:
                print(f"  [{symbol}] Advanced ICT analysis error: {e}", flush=True)

            # -- Step 8f3: Intermarket analysis (DXY, US10Y, VIX) --
            try:
                from bridge.intermarket import get_intermarket_context, check_intermarket_conflict
                imkt_ctx = get_intermarket_context(self.config.internal_symbol(symbol))
                result.vix_risk_multiplier = imkt_ctx.vix_risk_multiplier
                if direction != Direction.NEUTRAL:
                    dir_str = "BUY" if direction == Direction.BULLISH else "SELL"
                    is_conflict, severity, imkt_reason = check_intermarket_conflict(
                        self.config.internal_symbol(symbol), dir_str, imkt_ctx
                    )
                    result.intermarket_conflict = is_conflict
                    result.intermarket_explanation = imkt_reason
                    if is_conflict:
                        result.advanced_factors.append(f"Intermarket_conflict({severity})")
                    if imkt_ctx.vix_risk_multiplier < 1.0:
                        result.advanced_factors.append(f"VIX_{imkt_ctx.vix_level:.0f}")
            except Exception as e:
                print(f"  [{symbol}] Intermarket analysis error: {e}", flush=True)

            # -- Step 8f4: Economic calendar (news blackout) --
            try:
                from bridge.economic_calendar import is_news_blackout
                blackout, event_name, minutes = is_news_blackout(self.config.internal_symbol(symbol))
                result.news_blackout = blackout
                result.news_event = event_name
                result.news_minutes = minutes
                if blackout:
                    result.advanced_factors.append(f"NewsBlackout({event_name})")
            except Exception as e:
                print(f"  [{symbol}] Economic calendar error: {e}", flush=True)

            # -- Step 8g: Fibonacci extension TP levels --
            # Prefer H4 range for Fibonacci (macro move), fallback to M15
            fib_high = result.htf_range_high if result.htf_range_high > 0 else range_high
            fib_low = result.htf_range_low if result.htf_range_low > 0 else range_low
            if fib_high > fib_low and direction in (Direction.BULLISH, Direction.BEARISH):
                rng = fib_high - fib_low
                fib_ratios = [1.272, 1.618, 2.0, 2.618]
                if direction == Direction.BULLISH:
                    result.fib_tp_levels = [round(fib_low + rng * r, 5) for r in fib_ratios]
                else:
                    result.fib_tp_levels = [round(fib_high - rng * r, 5) for r in fib_ratios]

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

            # Recompute P/D alignment now that direction is finalized
            # (critical for NEUTRAL HTF case where direction was unknown at step 8b)
            if range_high > range_low:
                final_dir = Direction.BULLISH if result.direction == "BULLISH" else Direction.BEARISH
                result.pd_aligned = pd_aligned_with_bias(
                    result.current_price, range_high, range_low, final_dir
                )

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

            # -- Step 10c: Synergy & gate evaluation (cross_correlations.json) --
            # Applies super-additive bonuses for concept combinations (OB+FVG,
            # sweep+SMT, etc.) and penalties for gate violations (OTE in wrong
            # zone, OB without displacement). Turns the knowledge base's
            # cross-connection data into actual grading signal.
            try:
                from bridge.synergy_scorer import evaluate_synergies
                synergy_adj = evaluate_synergies(result)
                if synergy_adj.net_delta != 0:
                    result.total_score = max(
                        0, min(100, result.total_score + synergy_adj.net_delta)
                    )
                    result.confluence_factors.extend(synergy_adj.named_factors)
                    if synergy_adj.bonus_points:
                        result.confluence_factors.append(
                            f"synergy(+{synergy_adj.bonus_points:.0f})"
                        )
                    if synergy_adj.penalty_points:
                        result.confluence_factors.append(
                            f"gate(-{synergy_adj.penalty_points:.0f})"
                        )
                # Stash for prompt enrichment
                result.synergy_explanations = synergy_adj.explanations
                result.gate_violations = synergy_adj.gate_violations
            except Exception as e:
                print(f"  [{symbol}] Synergy scoring error: {e}", flush=True)
                result.synergy_explanations = []
                result.gate_violations = []

            # Re-grade after ALL bonuses/penalties applied
            if result.advanced_factors or getattr(result, 'synergy_explanations', None):
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

        Primary: MT5 (fast, reliable, no chart switching).
        Fallback: TradingView via CDP (legacy, slow, prone to drift).

        Returns:
            {"H4": df_or_None, "H1": df_or_None, "M15": df_or_None}
        """
        # Primary: use MT5 directly — no chart switching needed
        if self._use_mt5 and self._mt5_data is not None:
            dfs = self._mt5_data.collect_data(symbol)
            # Check if we got enough data
            has_data = any(df is not None and len(df) >= 10 for df in dfs.values())
            if has_data:
                # Store for reuse by other components
                self._last_collected_dfs = {}
                tf_to_tv = {"H4": self.config.htf, "H1": self.config.itf, "M15": self.config.ltf}
                for label, df_val in dfs.items():
                    if df_val is not None:
                        tv_tf = tf_to_tv.get(label, label)
                        self._last_collected_dfs[tv_tf] = df_val
                return dfs

        # Fallback: TradingView chart switching (legacy)
        dfs: dict[str, pd.DataFrame | None] = {"H4": None, "H1": None, "M15": None}
        cfg = self.config
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
        for switch_attempt in range(3):
            try:
                result = self.client.set_symbol(symbol, require_ready=True)
                if not result.get("chart_ready", False):
                    if switch_attempt < 2:
                        time.sleep(3.0)
                        continue
                    print(f"[WARN] Chart not ready for {symbol} after set_symbol — skipping", flush=True)
                    return dfs

                # Poll until quote confirms the correct symbol (up to 15s).
                # TV Desktop is slow and inconsistent — chart_ready lies.
                # The quote symbol is the only reliable confirmation.
                quote = {}
                chart_sym = ""
                for poll in range(6):  # 6 x 2.5s = 15s max
                    time.sleep(2.5)
                    try:
                        quote = self.client.get_quote()
                        chart_sym = quote.get("symbol", "").split(":")[-1]
                        if chart_sym == target_sym:
                            break
                    except Exception:
                        pass

                if chart_sym != target_sym:
                    if switch_attempt < 2:
                        # Re-send the symbol switch command
                        continue
                    print(f"[WARN] Quote symbol mismatch: expected {target_sym}, got {chart_sym} — skipping", flush=True)
                    return dfs

                live_price = float(quote.get("last") or quote.get("lp") or quote.get("close") or 0)
                if live_price <= 0:
                    if switch_attempt < 2:
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
        # Primary: MT5
        if self._use_mt5 and self._mt5_data is not None:
            df = self._mt5_data.get_smt_data(smt_symbol, timeframe="M15", count=50)
            if df is not None and len(df) >= 10:
                return df

        # Fallback: TradingView
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
