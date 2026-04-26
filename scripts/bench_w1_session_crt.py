"""
W1 + SessionCRT super-synergy fire-rate bench (Option 3).

Question this answers: when W1 CRT and SessionCRT both fire AND their
directions agree (both BULLISH or both BEARISH), conviction should be
very high (weekly-fractal swing reversal aligned with today's NY draw).
Is the alignment rare enough to justify a +6 conviction premium without
flat-inflating the score?

Decision rule (per project_kb_next_session_prompt.md Option 3):
  0.5% <= rate <= 3%  -> SHIP `W1+SessionCRT_aligned` synergy at +6
  rate < 0.5%         -> DEFER (too rare to matter operationally)
  rate > 3%           -> DEFER (probably double-counting an existing
                          SessionCRT+KillZone or MultiTF_CRT signal)

Method (matches the bridge call sites):
1. Load tz-aware M15 cache for each symbol. SessionCRT slices by NY
   hour math and needs UTC-aware bars.
2. Resample to W1 (Mon-anchored weekly bars) for the W1 CRT detector.
3. Walk M15 forward in 4h steps (matches bench_session_crt.STEP_M15=16).
4. At each cycle endpoint inside the NY window (the only time the
   bridge calls detect_session_crt):
   a. Slice trailing 96 M15 bars -> SessionCRTSetup or empty.
   b. df_w1.loc[:ts].iloc[:-1] -> detect_crt(lookback=1, tf_label="W1").
      Two W1-active definitions tracked in parallel:
        - ANY:   any setup in the window (matches bridge's CRT_W1(N)
                 factor emission and substring-based synergy predicates).
        - FRESH: most-recent setup's sweep_bar_index == last index in
                 df_w1[:-1] (a CRT that just printed on the latest
                 closed weekly bar — operationally meaningful "fresh").
   c. For each definition: both fire? directions agree?
5. Aggregate per-cycle buckets for both definitions.
6. Decision metric = both_aligned / total_NY_cycles (both views).

Comparison baselines printed:
- W1-CRT alone fire rate (any vs fresh) over NY cycles
- SessionCRT alone fire rate
- Both-fired (regardless of direction)
- Both-aligned (what the +6 synergy would catch)

Usage:
    PYTHONUTF8=1 python scripts/bench_w1_session_crt.py
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path("C:/Users/User/Desktop/trading-ai-v2")))

try:
    from zoneinfo import ZoneInfo
    NY_TZ = ZoneInfo("America/New_York")
except ImportError:  # pragma: no cover
    import pytz  # type: ignore
    NY_TZ = pytz.timezone("America/New_York")  # type: ignore

from analysis.ict.advanced import detect_crt, detect_session_crt  # noqa: E402

CACHE_ROOT = Path("C:/Users/User/Desktop/trading-ai-v2/data/cache")
SYMBOLS = ["XAUUSD", "EURUSD", "GBPUSD", "BTCUSD", "ETHUSD", "SOLUSD", "US500.cash"]

WINDOW_M15 = 96     # 24h trailing window for SessionCRT
STEP_M15 = 16       # 4h step (same as bench_session_crt.py)
W1_MIN_BARS = 6     # need >=5 bars after [:-1] slice
M15_WARMUP = 96 * 7 * 6  # ~6 weeks of M15 before first usable W1


def _load_m15(sym: str) -> pd.DataFrame | None:
    p = CACHE_ROOT / sym / "M15" / "data.parquet"
    if not p.exists():
        return None
    df = pd.read_parquet(p)
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    else:
        df.index = df.index.tz_convert("UTC")
    return df


def _resample_w1(df_m15_aware: pd.DataFrame) -> pd.DataFrame:
    # detect_crt uses .iloc[i] / .iloc[i-1] arithmetic; tz on the index
    # is irrelevant to it. Keep tz-aware to avoid coercion bugs.
    return df_m15_aware.resample("1W-MON", label="left", closed="left").agg({
        "open": "first", "high": "max", "low": "min",
        "close": "last", "volume": "sum",
    }).dropna()


def _is_ny_window(ts_utc: pd.Timestamp) -> bool:
    ny = ts_utc.tz_convert(NY_TZ)
    mins = ny.hour * 60 + ny.minute
    return 7 * 60 <= mins < 17 * 60


def _bench_symbol(sym: str) -> dict:
    df_m15 = _load_m15(sym)
    if df_m15 is None or len(df_m15) < M15_WARMUP + WINDOW_M15:
        return {"symbol": sym, "skipped": True, "reason": "insufficient cache"}

    df_w1 = _resample_w1(df_m15)
    if len(df_w1) < W1_MIN_BARS:
        return {"symbol": sym, "skipped": True, "reason": "<6 W1 bars"}

    ny_cycles = 0
    session_fires = 0

    # ANY view (matches bridge factor emission + substring synergy match)
    w1_any_fires = 0
    both_any_fires = 0
    both_any_aligned = 0
    both_any_opposed = 0

    # FRESH view (most-recent W1 closed bar IS the sweep bar)
    w1_fresh_fires = 0
    both_fresh_fires = 0
    both_fresh_aligned = 0
    both_fresh_opposed = 0

    for end in range(max(WINDOW_M15, M15_WARMUP), len(df_m15) - 1, STEP_M15):
        ts_end = df_m15.index[end - 1]
        if not _is_ny_window(ts_end):
            continue

        ny_cycles += 1
        m15_window = df_m15.iloc[end - WINDOW_M15: end]

        # SessionCRT (bridge call site: 96 M15 bars, NY-window-only)
        s_setups = detect_session_crt(m15_window)
        s_dir = s_setups[0].direction if s_setups else None
        if s_dir is not None:
            session_fires += 1

        # W1 CRT — bridge call site `df_w1.iloc[:-1]` with lookback=1.
        w1_visible = df_w1.loc[:ts_end]
        if len(w1_visible) < W1_MIN_BARS:
            continue
        df_w1_crt = w1_visible.iloc[:-1]
        if len(df_w1_crt) < 3:
            continue
        w1_setups = detect_crt(df_w1_crt, lookback=1, tf_label="W1")

        # ANY: any setup at all
        w1_any_dir = w1_setups[-1].direction if w1_setups else None
        # FRESH: most-recent setup's sweep_bar_index points to the last
        # index in df_w1_crt — i.e. the just-closed weekly bar swept the
        # prior week's range. This is what "a fresh W1 CRT just printed"
        # means operationally.
        w1_fresh_dir = None
        if w1_setups:
            last = w1_setups[-1]
            if last.sweep_bar_index == len(df_w1_crt) - 1:
                w1_fresh_dir = last.direction

        if w1_any_dir is not None:
            w1_any_fires += 1
        if w1_fresh_dir is not None:
            w1_fresh_fires += 1

        if w1_any_dir is not None and s_dir is not None:
            both_any_fires += 1
            if w1_any_dir == s_dir:
                both_any_aligned += 1
            else:
                both_any_opposed += 1

        if w1_fresh_dir is not None and s_dir is not None:
            both_fresh_fires += 1
            if w1_fresh_dir == s_dir:
                both_fresh_aligned += 1
            else:
                both_fresh_opposed += 1

    return {
        "symbol": sym,
        "ny_cycles": ny_cycles,
        "session_fires": session_fires,
        "any": {
            "w1_fires": w1_any_fires,
            "both_fires": both_any_fires,
            "both_aligned": both_any_aligned,
            "both_opposed": both_any_opposed,
        },
        "fresh": {
            "w1_fires": w1_fresh_fires,
            "both_fires": both_fresh_fires,
            "both_aligned": both_fresh_aligned,
            "both_opposed": both_fresh_opposed,
        },
    }


def main() -> int:
    print("=" * 72)
    print("W1 + SessionCRT super-synergy fire-rate bench")
    print("=" * 72)
    print(f"  symbols: {SYMBOLS}")
    print(f"  M15 window: {WINDOW_M15} bars (24h)  step: {STEP_M15} M15 (4h)")
    print(f"  decision: 0.5%-3% aligned -> ship +6, else defer")
    print()

    grand_ny = 0
    grand_session = 0
    grand_any = {"w1": 0, "both": 0, "aligned": 0, "opposed": 0}
    grand_fresh = {"w1": 0, "both": 0, "aligned": 0, "opposed": 0}

    for sym in SYMBOLS:
        print(f"[{sym}] running...", flush=True)
        r = _bench_symbol(sym)
        if r.get("skipped"):
            print(f"  SKIPPED: {r['reason']}")
            continue
        n = r["ny_cycles"]
        a = r["any"]
        f = r["fresh"]
        sess = r["session_fires"]
        print(f"  ny_cycles={n}  session_fires={sess} ({100*sess/n:.2f}%)")
        print(f"  ANY view:   w1_fires={a['w1_fires']} ({100*a['w1_fires']/n:.2f}%) "
              f"both={a['both_fires']} ({100*a['both_fires']/n:.2f}%) "
              f"aligned={a['both_aligned']} ({100*a['both_aligned']/n:.2f}%) "
              f"opposed={a['both_opposed']} ({100*a['both_opposed']/n:.2f}%)")
        print(f"  FRESH view: w1_fires={f['w1_fires']} ({100*f['w1_fires']/n:.2f}%) "
              f"both={f['both_fires']} ({100*f['both_fires']/n:.2f}%) "
              f"aligned={f['both_aligned']} ({100*f['both_aligned']/n:.2f}%) "
              f"opposed={f['both_opposed']} ({100*f['both_opposed']/n:.2f}%)")

        grand_ny += n
        grand_session += sess
        for k, v in (("w1", a["w1_fires"]), ("both", a["both_fires"]),
                     ("aligned", a["both_aligned"]), ("opposed", a["both_opposed"])):
            grand_any[k] += v
        for k, v in (("w1", f["w1_fires"]), ("both", f["both_fires"]),
                     ("aligned", f["both_aligned"]), ("opposed", f["both_opposed"])):
            grand_fresh[k] += v

    print()
    print("=" * 72)
    print("Aggregate")
    print("=" * 72)
    if grand_ny == 0:
        print("ERROR: zero NY-window cycles aggregated")
        return 1

    print(f"  total NY cycles:        {grand_ny}")
    print(f"  SessionCRT fires:       {grand_session} ({100.0*grand_session/grand_ny:.2f}%)")
    print()
    print("  ANY view (any W1 setup in df_w1[:-1] — matches bridge CRT_W1(N) emission):")
    for label, val in (("w1_fires", grand_any["w1"]), ("both", grand_any["both"]),
                       ("aligned", grand_any["aligned"]), ("opposed", grand_any["opposed"])):
        print(f"    {label:<10} {val:>6} ({100.0*val/grand_ny:.2f}%)")
    print()
    print("  FRESH view (most-recent W1 setup is on the latest closed weekly bar):")
    for label, val in (("w1_fires", grand_fresh["w1"]), ("both", grand_fresh["both"]),
                       ("aligned", grand_fresh["aligned"]), ("opposed", grand_fresh["opposed"])):
        print(f"    {label:<10} {val:>6} ({100.0*val/grand_ny:.2f}%)")

    aligned_any_pct = 100.0 * grand_any["aligned"] / grand_ny
    aligned_fresh_pct = 100.0 * grand_fresh["aligned"] / grand_ny

    print()
    print("=" * 72)
    print("Decision")
    print("=" * 72)
    print(f"  ANY-view aligned   = {aligned_any_pct:.2f}%  (substring-match predicate)")
    print(f"  FRESH-view aligned = {aligned_fresh_pct:.2f}%  (just-printed W1 CRT)")
    print()
    print("  Decision rule: 0.5%-3.0% -> SHIP +6, else DEFER")
    print()

    def _verdict(pct: float, view: str) -> str:
        if 0.5 <= pct <= 3.0:
            return f"  {view}: {pct:.2f}% IN [0.5%, 3.0%] band -> SHIP candidate"
        if pct < 0.5:
            return f"  {view}: {pct:.2f}% < 0.5% -> DEFER (too rare)"
        return f"  {view}: {pct:.2f}% > 3.0% -> DEFER (probably flat inflation / double-count)"

    print(_verdict(aligned_any_pct, "ANY view  "))
    print(_verdict(aligned_fresh_pct, "FRESH view"))

    print()
    if grand_any["both"] > 0:
        any_cond_alignment = 100.0 * grand_any["aligned"] / grand_any["both"]
        print(f"  P(directions agree | both fire, ANY view)   = {any_cond_alignment:.1f}%")
    if grand_fresh["both"] > 0:
        fresh_cond_alignment = 100.0 * grand_fresh["aligned"] / grand_fresh["both"]
        print(f"  P(directions agree | both fire, FRESH view) = {fresh_cond_alignment:.1f}%")
    print("  (50%/50% = random; >55% = real directional signal worth a synergy)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
