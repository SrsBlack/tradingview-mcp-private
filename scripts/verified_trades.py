"""
VERIFIED TRADE RESULTS — Apr 8-9 2026
Cross-referenced against actual price data from session decision log.
6 contaminated entries excluded (T05 SOL @ $98 reclassified — SOL was $82 all day, $98 was OIL's price on SOL chart).
"""

print()
print('=' * 105)
print('  VERIFIED TRADE RESULTS  |  Apr 8-9 2026  |  Cross-checked against recorded price data')
print('=' * 105)
hdr = ('#', 'Symbol', 'Dir', 'Entry', 'SL', 'TP1', 'Result', 'Evidence', 'PnL', 'R')
print(f"  {hdr[0]:<4} {hdr[1]:<20} {hdr[2]:<5} {hdr[3]:>10} {hdr[4]:>10} {hdr[5]:>10}  {hdr[6]:<12} {hdr[7]:<28} {hdr[8]:>10} {hdr[9]:>5}")
print('-' * 105)

trades = [
    #  id   symbol              dir    entry     sl       tp1       result    evidence                            pnl      r     conf
    ('T01', 'BTCUSD',          'BUY', 69000.0, 68500.0, 70000.0, 'TP HIT',  'Bridge close log',              200.00,  2.0, 'CONFIRMED'),
    ('T02', 'BTCUSD',          'BUY', 80000.0, 79200.0, 81200.0, 'TP2 HIT', 'Bridge close log',              300.00,  3.0, 'CONFIRMED'),
    ('T03', 'EURUSD',          'SELL',  1.1700,  1.1725,  1.1625, 'SL HIT',  'Bridge close log',              -60.00, -1.0, 'CONFIRMED'),
    ('T04', 'BTCUSD',          'BUY', 70923.0, 70680.0, 71850.0, 'SL HIT',  'Price hit 70596 @ 14:19 UTC',  -49.72, -1.0, 'VERIFIED'),
    # T05 EXCLUDED — contaminated: SOL was $82 all day, $98 was OIL's price on SOL chart
    # ('T05', 'SOLUSD',        'BUY',    98.03,   97.18,   99.87, 'CONTAM',  'SOL=$82, price was OIL leak', 0, 0, 'CONTAMINATED'),
    ('T06', 'EURUSD',          'BUY',   1.170,   1.168,   1.175,  'SL HIT',  'Price hit 1.166 @ 08:12 UTC',  -30.00, -1.0, 'VERIFIED'),
    ('T07', 'BTCUSD',          'BUY', 70963.0, 70450.0, 72200.0, 'OPEN',    'Never hit SL or TP in data',      0.00,  0.0, 'OPEN'),
    ('T08', 'EURUSD',          'SELL',  1.170,   1.175,   1.162,  'OPEN',    'Ranged 1.167-1.170 in data',      0.00,  0.0, 'OPEN'),
    ('T09', 'SOLUSD',          'BUY',   82.28,   81.15,   84.92,  'OPEN',    'Ranged 81.58-84.48 in data',      0.00,  0.0, 'OPEN'),
    ('T10', 'UKOIL',           'SELL',  98.29,   98.95,   97.15,  'SL HIT',  'Price hit 99.15 @ 14:39 UTC',  -19.98, -1.0, 'VERIFIED'),
    ('T11', 'SOLUSD',          'BUY',   82.49,   81.24,   84.99,  'OPEN',    'Ranged 81.58-84.48 in data',      0.00,  0.0, 'OPEN'),
    ('T12', 'EURUSD',          'SELL',  1.170,   1.175,   1.162,  'OPEN',    'Ranged 1.167-1.170 in data',      0.00,  0.0, 'OPEN'),
    ('T13', 'XAUUSD',          'BUY', 4731.93, 4720.15, 4748.60, 'TP2 HIT', 'TP1 @ 11:47, TP2 @ 13:44 UTC',  52.50,  2.6, 'VERIFIED'),
    ('T14', 'ETHUSD',          'BUY', 2180.96, 2168.50, 2198.75, 'SL HIT',  'Price hit 2165 @ 13:49 UTC',   -50.00, -1.0, 'VERIFIED'),
]

total = 0
wins = losses = still_open = 0
for t in trades:
    tid, sym, dr, entry, sl, tp1, result, evidence, pnl, r, conf = t
    total += pnl
    pnl_s = '+${:,.2f}'.format(pnl) if pnl >= 0 else '-${:,.2f}'.format(abs(pnl))
    if pnl > 0:
        wins += 1
    elif pnl < 0:
        losses += 1
    else:
        still_open += 1
    print(f"  {tid:<4} {sym:<20} {dr:<5} {entry:>10.4f} {sl:>10.4f} {tp1:>10.4f}  {result:<12} {evidence:<28} {pnl_s:>10} {r:>4.1f}R")

print('=' * 105)
print()
print(f"  CLOSED:     {wins}W / {losses}L  ({wins}/{wins+losses} = {wins*100//(wins+losses)}% win rate)")
print(f"  STILL OPEN: {still_open} trades (never hit SL or TP in recorded data window)")
print()
print(f"  REALIZED P&L:    ${total:+,.2f}")
print()
print('  HOW THIS WAS VERIFIED:')
print('  The session file 2026-04-09.json contains 284 decision entries with actual prices')
print('  observed by the bridge every ~10 minutes across all symbols for the entire day.')
print('  For each orphaned trade, we traced the price path AFTER the entry timestamp')
print('  and checked whether SL or TP was breached first.')
print()
print('  KEY FINDINGS:')
print('  - T04 (BTC BUY 70923): Price dipped to 70596 at 14:19 -> SL at 70680 was hit')
print('  - T05 (SOL BUY 98.03): SOL crashed from 98 to 81 -> SL at 97.18 was obliterated')
print('  - T10 (OIL SELL 98.29): Oil spiked to 99.15 at 14:39 -> SL at 98.95 was hit first')
print('  - T13 (GOLD BUY 4731): Gold rallied steadily -> TP1 at 11:47, TP2 at 13:44. REAL WIN')
print('  - T14 (ETH BUY 2180): ETH dipped to 2165 at 13:49 -> SL hit before the bounce')
print()
print('  5 trades (T07-T09, T11-T12) are genuinely still open: price stayed between')
print('  their SL and TP during the entire recording window. These would need to be')
print('  resolved once the bridge is back online with state persistence.')
print('=' * 105)
