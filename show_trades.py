"""
Valid paper trades — Apr 8-9 2026
5 contaminated entries removed. T4 (ETHUSD @ 47958) also excluded — ETH ATH is ~4800, never 47k.
"""

CURRENT = {
    'BTCUSD': 72350.0,  # Apr 9 2026 — verified via web search
    'ETHUSD': 2183.0,   # Apr 9 2026 — verified via web search
    'SOLUSD': 82.50,    # Apr 9 2026 — verified via web search
    'EURUSD': 1.1050,
    'XAUUSD': 4767.0,   # user confirmed
    'UKOIL':  62.0,
}

def calc(sym, direction, entry, sl, tp, tp2, lot):
    base = sym.split(':')[-1]
    cur = CURRENT[base]
    if direction == 'BUY':
        if tp2 and cur >= tp2: ex, rsn = tp2, 'TP2 HIT'
        elif cur >= tp:        ex, rsn = tp,  'TP1 HIT'
        elif cur <= sl:        ex, rsn = sl,  'SL HIT'
        else:                  ex, rsn = cur, 'OPEN'
    else:
        if tp2 and cur <= tp2: ex, rsn = tp2, 'TP2 HIT'
        elif cur <= tp:        ex, rsn = tp,  'TP1 HIT'
        elif cur >= sl:        ex, rsn = sl,  'SL HIT'
        else:                  ex, rsn = cur, 'OPEN'
    diff = (ex - entry) if direction == 'BUY' else (entry - ex)
    pnl  = round(diff * lot, 2)
    r    = round(diff / abs(entry - sl), 2)
    return rsn, ex, pnl, r

# (id, symbol, dir, entry, sl, tp1, tp2, lot, confirmed, conf_pnl, conf_r, conf_rsn)
trades = [
    ('T01','BTCUSD',           'BUY',  69000.00, 68500.00, 70000.00, None,    0.2000, True,  200.0,  2.0, 'TP1'),
    ('T02','BTCUSD',           'BUY',  80000.00, 79200.00, 81200.00, 82400.0, 0.1250, True,  300.0,  3.0, 'TP2'),
    ('T03','OANDA:EURUSD',     'SELL',  1.1700,   1.1725,   1.1625,  1.1536,  24000, True,  -60.0, -1.0, 'SL'),
    ('T04','BITSTAMP:BTCUSD',  'BUY',  70923.00, 70680.00, 71850.00, 72950.0, 0.2045, False, None, None, None),
    ('T05','COINBASE:SOLUSD',  'BUY',     98.03,    97.18,    99.87, 101.71, 35.0824, False, None, None, None),
    ('T06','OANDA:EURUSD',     'BUY',   1.1700,    1.1680,   1.1750,  1.1813, 15000, False, None, None, None),
    ('T07','BITSTAMP:BTCUSD',  'BUY',  70963.00, 70450.00, 72200.00, 73600.0, 0.0975, False, None, None, None),
    ('T08','OANDA:EURUSD',     'SELL',  1.1700,   1.1750,   1.1620,  1.1490,  12000, False, None, None, None),
    ('T09','COINBASE:SOLUSD',  'BUY',     82.28,    81.15,    84.92,   87.56,  8.8496,False, None, None, None),
    ('T10','TVC:UKOIL',        'SELL',    98.29,    98.95,    97.15,   95.58,  30.303,False, None, None, None),
    ('T11','COINBASE:SOLUSD',  'BUY',     82.49,    81.24,    84.99,   87.49,  8.0,   False, None, None, None),
    ('T12','OANDA:EURUSD',     'SELL',  1.1700,   1.1750,   1.1620,  1.1490,  12000, False, None, None, None),
    ('T13','OANDA:XAUUSD',     'BUY', 4731.93,  4720.15,  4748.60, 4762.85,  1.6978, False, None, None, None),
    ('T14','COINBASE:ETHUSD',  'BUY', 2180.96,  2168.50,  2198.75, 2227.44,  4.0128, False, None, None, None),
]

W = 112
print()
print('=' * W)
print('  VALID PAPER TRADES  |  Apr 8-9 2026  |  5 contaminated entries excluded')
print('=' * W)
hdr = ('  #', 'Symbol', 'Dir', 'Entry', 'SL', 'TP1', 'TP2', 'Result', 'Exit', 'PnL', 'R')
print(f"  {hdr[0]:<4} {hdr[1]:<24} {hdr[2]:<5} {hdr[3]:>10} {hdr[4]:>10} {hdr[5]:>10} {hdr[6]:>10}  {hdr[7]:<18} {hdr[8]:>10} {hdr[9]:>10} {hdr[10]:>5}")
print('-' * W)

total = 0.0
wins = losses = 0

for row in trades:
    tid, sym, dr, entry, sl, tp, tp2, lot, confirmed, c_pnl, c_r, c_rsn = row
    if confirmed:
        rsn = c_rsn + ' confirmed'
        if c_rsn == 'TP2': ex = tp2
        elif c_rsn == 'TP1': ex = tp
        else: ex = sl
        pnl = c_pnl
        r   = c_r
    else:
        rsn, ex, pnl, r = calc(sym, dr, entry, sl, tp, tp2, lot)
        rsn += ' (est)'

    total += pnl
    pnl_s = '+${:,.2f}'.format(pnl) if pnl >= 0 else '-${:,.2f}'.format(abs(pnl))
    tp2_s = '{:.4f}'.format(tp2) if tp2 else '—'
    if pnl >= 0: wins += 1
    else:        losses += 1

    print(f"  {tid:<4} {sym:<24} {dr:<5} {entry:>10.4f} {sl:>10.4f} {tp:>10.4f} {tp2_s:>10}  {rsn:<18} {ex:>10.4f} {pnl_s:>10} {r:>4.1f}R")

print('=' * W)
print()
print(f"  Wins: {wins}  |  Losses: {losses}  |  Win Rate: {wins*100//(wins+losses)}%  |  Total Paper P&L: ${total:+,.2f}")
print()
print('  Current prices used:')
for k, v in CURRENT.items():
    print(f'    {k}: ${v:,.2f}')
print()
print('  Legend:')
print('    confirmed = close event found in session log (bridge logged the close)')
print('    (est)     = bridge restarted before close; outcome derived from SL/TP vs current price')
print('    Excluded  = 5 entries with impossible prices (cross-symbol contamination)')
print('=' * W)
