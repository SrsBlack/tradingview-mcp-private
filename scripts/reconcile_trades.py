"""
Clean trade P&L reconciliation — Apr 9 2026
Excludes 4 contaminated entries. Estimates outcome for orphaned trades.
"""

CURRENT = {
    'BTCUSD':  79000.0,   # dropped from 80k+ on tariff shock
    'ETHUSD':  1550.0,    # dropped hard
    'SOLUSD':  108.0,     # slight recovery
    'EURUSD':  1.1050,    # EUR surged on USD weakness
    'XAUUSD':  4767.0,    # confirmed spot gold price Apr 9 2026
    'UKOIL':   62.0,      # oil tanked on recession fears
}

def calc_pnl(symbol, direction, entry, sl, tp, tp2, lot):
    base = symbol.split(':')[-1]
    cur = CURRENT.get(base, entry)

    if direction == 'BUY':
        hit_sl  = cur <= sl
        hit_tp2 = bool(tp2) and cur >= tp2
        hit_tp  = cur >= tp
    else:
        hit_sl  = cur >= sl
        hit_tp2 = bool(tp2) and cur <= tp2
        hit_tp  = cur <= tp

    if hit_tp2 and tp2:
        exit_price, outcome = tp2, 'TP2 HIT'
    elif hit_tp:
        exit_price, outcome = tp, 'TP HIT'
    elif hit_sl:
        exit_price, outcome = sl, 'SL HIT'
    else:
        exit_price, outcome = cur, 'OPEN'

    diff = (exit_price - entry) if direction == 'BUY' else (entry - exit_price)
    pnl  = round(diff * lot, 2)
    sl_dist = abs(entry - sl) or 1
    r    = round(diff / sl_dist, 2)
    return outcome, exit_price, pnl, r, cur


trades = [
    # status: CLOSED = confirmed by log; ORPHANED = bridge restarted, estimate from price
    dict(id='T1',  sym='BTCUSD',             dir='BUY',  entry=69000.0, sl=68500.0, tp=70000.0,  tp2=None,     lot=0.20,    status='CLOSED',   c_exit=70000.0,  c_pnl=200.0,  c_r=2.0,  c_rsn='TP'),
    dict(id='T2',  sym='BTCUSD',             dir='BUY',  entry=80000.0, sl=79200.0, tp=81200.0,  tp2=82400.0,  lot=0.125,   status='CLOSED',   c_exit=82400.0,  c_pnl=300.0,  c_r=3.0,  c_rsn='TP2'),
    dict(id='T3',  sym='OANDA:EURUSD',       dir='SELL', entry=1.17,    sl=1.1725,  tp=1.1625,   tp2=1.1536,   lot=24000.0, status='CLOSED',   c_exit=1.1725,   c_pnl=-60.0,  c_r=-1.0, c_rsn='SL'),
    dict(id='T4',  sym='COINBASE:ETHUSD',    dir='SELL', entry=47958.0, sl=48420.0, tp=47200.0,  tp2=31573.0,  lot=0.1082,  status='ORPHANED'),
    dict(id='T5',  sym='BITSTAMP:BTCUSD',    dir='BUY',  entry=70923.0, sl=70680.0, tp=71850.0,  tp2=72950.0,  lot=0.2045,  status='ORPHANED'),
    dict(id='T6',  sym='COINBASE:SOLUSD',    dir='BUY',  entry=98.03,   sl=97.18,   tp=99.87,    tp2=101.71,   lot=35.0824, status='ORPHANED'),
    dict(id='T7',  sym='OANDA:EURUSD',       dir='BUY',  entry=1.17,    sl=1.168,   tp=1.175,    tp2=1.1813,   lot=15000.0, status='ORPHANED'),
    dict(id='T8',  sym='BITSTAMP:BTCUSD',    dir='BUY',  entry=70963.0, sl=70450.0, tp=72200.0,  tp2=73600.0,  lot=0.0975,  status='ORPHANED'),
    dict(id='T9',  sym='OANDA:EURUSD',       dir='SELL', entry=1.17,    sl=1.175,   tp=1.162,    tp2=1.149,    lot=12000.0, status='ORPHANED'),
    dict(id='T10', sym='COINBASE:SOLUSD',    dir='BUY',  entry=82.28,   sl=81.15,   tp=84.92,    tp2=87.56,    lot=8.8496,  status='ORPHANED'),
    dict(id='T11', sym='TVC:UKOIL',          dir='SELL', entry=98.29,   sl=98.95,   tp=97.15,    tp2=95.58,    lot=30.303,  status='ORPHANED'),
    dict(id='T12', sym='COINBASE:SOLUSD',    dir='BUY',  entry=82.49,   sl=81.24,   tp=84.99,    tp2=87.49,    lot=8.0,     status='ORPHANED'),
    dict(id='T13', sym='OANDA:EURUSD',       dir='SELL', entry=1.17,    sl=1.175,   tp=1.162,    tp2=1.149,    lot=12000.0, status='ORPHANED'),
    dict(id='T14', sym='OANDA:XAUUSD',       dir='BUY',  entry=4731.93, sl=4720.15, tp=4748.6,   tp2=4762.85,  lot=1.6978,  status='ORPHANED'),
    dict(id='T15', sym='COINBASE:ETHUSD',    dir='BUY',  entry=2180.96, sl=2168.5,  tp=2198.75,  tp2=2227.44,  lot=4.0128,  status='ORPHANED'),
]

W = 100
print()
print('=' * W)
print('  CLEAN TRADE RECONCILIATION — Apr 9 2026 (4 contaminated entries excluded)')
print('=' * W)
print(f"  {'ID':<4} {'Symbol':<22} {'Dir':<5} {'Entry':>10} {'SL':>10} {'TP':>10} {'Outcome':<14} {'Exit':>10} {'PnL':>12} {'R':>5}")
print('-' * W)

total_confirmed = 0.0
total_estimated = 0.0
wins = losses = 0
rows = []

for t in trades:
    if t['status'] == 'CLOSED':
        outcome = t['c_rsn'] + ' (confirmed)'
        exit_p  = t['c_exit']
        pnl     = t['c_pnl']
        r       = t['c_r']
        total_confirmed += pnl
    else:
        outcome, exit_p, pnl, r, _ = calc_pnl(
            t['sym'], t['dir'], t['entry'], t['sl'], t['tp'], t.get('tp2'), t['lot']
        )
        outcome += ' (est.)'
        total_estimated += pnl

    if pnl >= 0:
        wins += 1
        pnl_str = f'+${pnl:,.2f}'
    else:
        losses += 1
        pnl_str = f'-${abs(pnl):,.2f}'

    rows.append((t['id'], t['sym'], t['dir'], t['entry'], t['sl'], t['tp'], outcome, exit_p, pnl_str, r))

for r in rows:
    print(f"  {r[0]:<4} {r[1]:<22} {r[2]:<5} {r[3]:>10.4f} {r[4]:>10.4f} {r[5]:>10.4f} {r[6]:<22} {r[7]:>10.4f} {r[8]:>12} {r[9]:>5.1f}R")

print('=' * W)
print(f"  Confirmed closed:        ${total_confirmed:>+10,.2f}")
print(f"  Orphaned (estimated):    ${total_estimated:>+10,.2f}")
print(f"  TOTAL PAPER P&L:         ${total_confirmed + total_estimated:>+10,.2f}")
print(f"  Win rate:  {wins}/{wins+losses}  ({wins/(wins+losses)*100:.0f}%)")
print()
print('  NOTE: Orphaned = bridge was restarted; no close log exists.')
print('        Outcome estimated from current market price vs SL/TP levels.')
print()
print('  Current prices used:')
for k, v in CURRENT.items():
    print(f'    {k}: {v}')
print()
print('  !! VERIFY prices against live chart — especially ETHUSD @ 47,958 entry')
print('     which may itself be contamination (ETH was ~1,500-2,000 on Apr 9)')
print('=' * W)
