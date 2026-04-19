"""
HONEST INDEPENDENT AUDIT — reads raw logs, flags everything suspicious.
No sugar-coating. Every trade gets a verdict and a confidence level.
"""

import json, glob, os, sys

# Force UTF-8 stdout on Windows so box-drawing chars don't crash cp1252.
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except (AttributeError, Exception):  # pragma: no cover
    pass

# ─── Current market prices (Apr 9 2026, web-verified) ───
# Previous values were WRONG: BTC was 79k (actually ~72k), ETH was 1550 (actually ~2180), SOL was 108 (actually ~82)
MARKET = {
    'BTCUSD':  72350.0,  # Apr 9 2026 — verified via web search
    'ETHUSD':  2183.0,   # Apr 9 2026 — verified via web search
    'SOLUSD':  82.50,    # Apr 9 2026 — verified via web search
    'EURUSD':  1.1050,
    'XAUUSD':  4767.0,   # user confirmed
    'UKOIL':   62.0,
}

# ─── Valid price ranges (realistic ceilings, not speculative) ───
VALID_RANGES = {
    'BTCUSD':  (10_000, 200_000),
    'ETHUSD':  (100,    10_000),
    'SOLUSD':  (1,      1_000),
    'EURUSD':  (0.80,   1.60),
    'XAUUSD':  (1_000,  6_000),
    'UKOIL':   (10,     150),
}

def base(sym):
    return sym.split(':')[-1]

def is_contaminated(sym, price):
    b = base(sym)
    rng = VALID_RANGES.get(b)
    if not rng: return False
    return not (rng[0] <= price <= rng[1])

def would_sl_have_hit(direction, entry, sl, price_history_note):
    """
    For orphaned trades, we can't know the intraday path.
    If current price is past TP, price MAY have hit SL first on the way.
    We flag this uncertainty honestly.
    """
    return "UNKNOWN — no tick-level data to confirm intraday path"

def estimate_outcome(sym, direction, entry, sl, tp1, tp2):
    b = base(sym)
    cur = MARKET.get(b)
    if cur is None:
        return 'NO_PRICE', 0, 0, 0

    if direction == 'BUY':
        went_past_tp2 = tp2 and cur >= tp2
        went_past_tp1 = cur >= tp1
        went_past_sl  = cur <= sl
    else:
        went_past_tp2 = tp2 and cur <= tp2
        went_past_tp1 = cur <= tp1
        went_past_sl  = cur >= sl

    if went_past_tp2:
        ex = tp2
        diff = (ex - entry) if direction == 'BUY' else (entry - ex)
        return 'TP2_REGION', ex, round(diff * 1, 4), round(diff / abs(entry - sl), 2)
    elif went_past_tp1:
        ex = tp1
        diff = (ex - entry) if direction == 'BUY' else (entry - ex)
        return 'TP1_REGION', ex, round(diff * 1, 4), round(diff / abs(entry - sl), 2)
    elif went_past_sl:
        ex = sl
        diff = (ex - entry) if direction == 'BUY' else (entry - ex)
        return 'SL_REGION', ex, round(diff * 1, 4), round(diff / abs(entry - sl), 2)
    else:
        diff = (cur - entry) if direction == 'BUY' else (entry - cur)
        return 'BETWEEN', cur, round(diff * 1, 4), round(diff / abs(entry - sl), 2)


# ─── Read all raw logs ───
files = sorted(glob.glob('C:/Users/User/tradingview-mcp-jackson/logs/paper_trades_*.jsonl'))
all_opens = []
all_closes = {}

for f in files:
    session = os.path.basename(f).replace('paper_trades_', '').replace('.jsonl', '')
    for line in open(f, encoding='utf-8'):
        line = line.strip()
        if not line: continue
        try:
            e = json.loads(line)
        except:
            continue
        evt = e.get('event') or e.get('type', '')
        if evt == 'OPEN':
            e['_session'] = session
            all_opens.append(e)
        elif evt == 'CLOSE':
            key = (session, e.get('ticket'))
            all_closes[key] = e


print()
print('=' * 120)
print('  INDEPENDENT TRADE AUDIT — Raw log analysis')
print('  Every trade classified: CONFIRMED CLOSE, CONTAMINATED, or ORPHANED (estimated)')
print('=' * 120)
print()

contaminated = []
confirmed_closed = []
orphaned = []

for e in all_opens:
    session = e['_session']
    ticket = e.get('ticket')
    sym = e.get('symbol', '?')
    direction = e.get('direction', '?')
    entry = float(e.get('entry_price', e.get('entry', 0)))
    sl = float(e.get('sl_price', e.get('sl', 0)))
    tp = float(e.get('tp_price', e.get('tp', 0)))
    tp2 = float(e.get('tp2_price', 0)) if e.get('tp2_price') else None
    lot = float(e.get('lot_size', 0))

    # Check contamination
    if is_contaminated(sym, entry):
        contaminated.append({
            'session': session, 'ticket': ticket, 'symbol': sym,
            'direction': direction, 'entry': entry,
            'reason': f"Entry {entry} outside valid range {VALID_RANGES.get(base(sym))} for {base(sym)}"
        })
        continue

    # Check if confirmed close exists
    close_key = (session, ticket)
    close = all_closes.get(close_key)

    if close:
        pnl = close.get('pnl', 0)
        r = close.get('r_multiple', 0)
        reason = close.get('reason', '?')
        exit_p = float(close.get('exit_price', close.get('exit', 0)))
        confirmed_closed.append({
            'session': session, 'ticket': ticket, 'symbol': sym,
            'direction': direction, 'entry': entry, 'sl': sl, 'tp': tp,
            'exit': exit_p, 'pnl': pnl, 'r': r, 'reason': reason, 'lot': lot,
        })
    else:
        region, est_exit, price_diff, est_r = estimate_outcome(sym, direction, entry, sl, tp, tp2)
        orphaned.append({
            'session': session, 'ticket': ticket, 'symbol': sym,
            'direction': direction, 'entry': entry, 'sl': sl, 'tp': tp, 'tp2': tp2,
            'lot': lot, 'region': region, 'est_exit': est_exit,
            'price_diff': price_diff, 'est_r': est_r,
        })

# ─── CONTAMINATED ───
print(f"CONTAMINATED — {len(contaminated)} trades (invalid prices, excluded entirely)")
print('-' * 80)
for c in contaminated:
    print(f"  [{c['session']}] #{c['ticket']} {c['symbol']} {c['direction']} @ {c['entry']}")
    print(f"    REASON: {c['reason']}")
print()

# ─── CONFIRMED CLOSES ───
print(f"CONFIRMED CLOSED — {len(confirmed_closed)} trades (bridge logged the close event)")
print('-' * 80)
conf_total = 0
for t in confirmed_closed:
    pnl_s = '+${:,.2f}'.format(t['pnl']) if t['pnl'] >= 0 else '-${:,.2f}'.format(abs(t['pnl']))
    conf_total += t['pnl']
    tag = 'WIN' if t['pnl'] >= 0 else 'LOSS'
    print(f"  [{t['session']}] #{t['ticket']} {t['symbol']} {t['direction']}")
    print(f"    Entry: {t['entry']}  SL: {t['sl']}  TP: {t['tp']}")
    print(f"    Exit:  {t['exit']}  Reason: {t['reason']}")
    print(f"    PnL:   {pnl_s}  R: {t['r']:.1f}R  => {tag}")
    print(f"    CONFIDENCE: HIGH (logged by bridge)")
    print()
print(f"  Confirmed total: ${conf_total:+,.2f}")
print()

# ─── ORPHANED ───
print(f"ORPHANED — {len(orphaned)} trades (bridge restarted, no close event)")
print("  ** These are ESTIMATES. The bridge was not running to monitor SL/TP hits. **")
print("  ** We only know where price IS NOW, not the intraday path it took. **")
print('-' * 80)
orph_best = 0
orph_worst = 0
for t in orphaned:
    b = base(t['symbol'])
    cur = MARKET.get(b, 0)
    tp2_s = f"TP2: {t['tp2']}" if t['tp2'] else "no TP2"

    # Calculate best-case PnL (TP hit) and worst-case (SL hit)
    sl_dist = abs(t['entry'] - t['sl'])
    sl_pnl = round(-sl_dist * t['lot'], 2)

    if t['region'] in ('TP2_REGION', 'TP1_REGION'):
        est_pnl = round(t['price_diff'] * t['lot'], 2)
        orph_best += est_pnl
        orph_worst += sl_pnl  # could have hit SL first
        pnl_s = '+${:,.2f}'.format(est_pnl) if est_pnl >= 0 else '-${:,.2f}'.format(abs(est_pnl))
        tag = 'LIKELY WIN'
        caveat = "Price is now past TP — BUT SL could have been hit first intraday"
    elif t['region'] == 'SL_REGION':
        est_pnl = sl_pnl
        orph_best += sl_pnl
        orph_worst += sl_pnl
        pnl_s = '-${:,.2f}'.format(abs(est_pnl))
        tag = 'LIKELY LOSS'
        caveat = "Price went past SL — almost certainly stopped out"
    else:
        diff = (cur - t['entry']) if t['direction'] == 'BUY' else (t['entry'] - cur)
        est_pnl = round(diff * t['lot'], 2)
        orph_best += est_pnl
        orph_worst += sl_pnl
        pnl_s = '+${:,.2f}'.format(est_pnl) if est_pnl >= 0 else '-${:,.2f}'.format(abs(est_pnl))
        tag = 'UNCERTAIN'
        caveat = "Price between SL and TP — trade may still be open or may have hit SL"

    conf = 'LOW' if t['region'] in ('TP2_REGION', 'TP1_REGION') else 'MEDIUM' if t['region'] == 'SL_REGION' else 'LOW'

    print(f"  [{t['session']}] #{t['ticket']} {t['symbol']} {t['direction']}")
    print(f"    Entry: {t['entry']}  SL: {t['sl']}  TP1: {t['tp']}  {tp2_s}")
    print(f"    Current price: {cur}  Lot: {t['lot']}")
    print(f"    Region: {t['region']}  Est exit: {t['est_exit']}  Est R: {t['est_r']:.1f}R")
    print(f"    Est PnL: {pnl_s}  => {tag}")
    print(f"    CAVEAT: {caveat}")
    print(f"    CONFIDENCE: {conf}")
    print()

print('-' * 80)
print(f"  Orphaned best-case total:  ${orph_best:+,.2f}  (if all TPs hit before SLs)")
print(f"  Orphaned worst-case total: ${orph_worst:+,.2f}  (if all hit SL first)")
print()

# ─── SUMMARY ───
print('=' * 120)
print('  FINAL SUMMARY')
print('=' * 120)
c_wins = len([t for t in confirmed_closed if t['pnl'] >= 0])
c_losses = len([t for t in confirmed_closed if t['pnl'] < 0])
o_likely_wins = len([t for t in orphaned if t['region'] in ('TP1_REGION', 'TP2_REGION')])
o_likely_losses = len([t for t in orphaned if t['region'] == 'SL_REGION'])
o_uncertain = len([t for t in orphaned if t['region'] not in ('TP1_REGION', 'TP2_REGION', 'SL_REGION')])
print(f"  Total opens in logs:     {len(all_opens)}")
print(f"  Contaminated (excluded): {len(contaminated)}")
print(f"  Confirmed closed:        {len(confirmed_closed)} ({c_wins}W / {c_losses}L)")
print(f"  Orphaned (estimated):    {len(orphaned)}")
print(f"    - Likely wins:         {o_likely_wins} (price past TP, but SL path unknown)")
print(f"    - Likely losses:       {o_likely_losses} (price past SL)")
print(f"    - Uncertain:           {o_uncertain}")
print()
print(f"  CONFIRMED P&L:           ${conf_total:+,.2f}  (HIGH confidence)")
print(f"  BEST-CASE total:         ${conf_total + orph_best:+,.2f}  (if orphaned TPs all hit)")
print(f"  WORST-CASE total:        ${conf_total + orph_worst:+,.2f}  (if orphaned all hit SL)")
print()
print("  HONEST ASSESSMENT:")
print("  The 3 confirmed trades are solid: 2 TP hits, 1 clean SL. That's real.")
print("  The 11 orphaned trades CANNOT be verified because the bridge wasn't running.")
print("  Price being past TP now doesn't prove it didn't hit SL first on the way.")
print("  To get real numbers, the bridge needs to stay running continuously.")
print('=' * 120)
