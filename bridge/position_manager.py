"""
Position lifecycle management — SL/TP checking, MT5 sync, paper shadow, reconciliation.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any

from bridge.config import price_in_range, tv_to_ftmo_symbol, ftmo_to_tv_symbol, TV_TO_FTMO
from bridge.decision_types import TradeDecision, PaperPosition
from bridge.tv_client import TVClient, TVClientError
from bridge.price_verify import PriceVerifier
from bridge.session_store import SessionStore
from bridge.state_store import StateStore
from bridge.trade_drawings import TradeDrawingManager


class PositionManager:
    """Manages open position lifecycle: price checks, SL/TP detection, MT5 sync."""

    def __init__(
        self,
        executor: Any,
        paper_shadow: Any,
        tv_client: TVClient,
        price_verifier: PriceVerifier,
        session: SessionStore,
        state_store: StateStore,
        paper_state_store: StateStore,
        drawings: TradeDrawingManager,
        mode: str,
    ):
        self.executor = executor
        self.paper_shadow = paper_shadow
        self.tv_client = tv_client
        self.price_verifier = price_verifier
        self.session = session
        self.state_store = state_store
        self.paper_state_store = paper_state_store
        self.drawings = drawings
        self.mode = mode
        # Dedup: tickets recently closed by MT5 sync (avoid double P&L counting)
        self._recently_closed: set[int] = set()

    @staticmethod
    def _infer_trade_type(entry: float, sl: float, tp: float) -> str:
        """Infer trade type from SL/TP distance for adopted positions.

        Uses risk:reward ratio as a proxy — wider TP targets imply swing trades.
        """
        if entry <= 0 or sl == 0:
            return "intraday"
        sl_dist = abs(entry - sl)
        tp_dist = abs(tp - entry) if tp > 0 else 0
        if sl_dist == 0:
            return "intraday"
        rr = tp_dist / sl_dist if sl_dist > 0 else 0
        # Wide TP (>2.5 R:R) with wide SL → swing
        if rr >= 2.5 and sl_dist / entry > 0.003:
            return "swing"
        return "intraday"

    def check_positions_sync(self) -> list[dict]:
        """Get current prices and check positions.

        Uses Alpaca API directly for crypto (fast, reliable, no chart switching).
        Falls back to TradingView chart quotes for non-crypto symbols.
        Also checks MT5 for live positions closed broker-side.
        """
        prices: dict[str, float] = {}

        # Step 1: Check if MT5 already closed any live positions (broker-side SL/TP)
        broker_closed_events = self._sync_mt5_closed_positions()

        for pos in self.executor.open_positions.values():
            try:
                # Try Alpaca first for crypto — fast, no chart switching
                alpaca_price = self.price_verifier.get_alpaca_price(pos.symbol)
                if alpaca_price and alpaca_price > 0:
                    if price_in_range(pos.symbol, alpaca_price):
                        prices[pos.symbol] = alpaca_price
                        continue
                    else:
                        print(f"[POSITIONS] {pos.symbol} Alpaca price {alpaca_price:.4f} FAILED range check", flush=True)

                # Try MT5 for forex/indices/commodities — no chart switching needed
                mt5_sym = pos.symbol.split(":")[-1].replace(".cash", "")
                mt5_price = self.price_verifier.get_mt5_price(mt5_sym)
                if mt5_price and mt5_price > 0:
                    if price_in_range(pos.symbol, mt5_price):
                        prices[pos.symbol] = mt5_price
                        continue
                    else:
                        print(f"[POSITIONS] {pos.symbol} MT5 price {mt5_price:.4f} FAILED range check", flush=True)

                # NEVER use TradingView for position price checks.
                # Switching the chart to check a position's price causes the
                # analysis loop's chart to get stuck on the wrong symbol.
                # Alpaca (crypto) + MT5 (everything else) cover all symbols.
                print(f"[POSITIONS] {pos.symbol} — no price from Alpaca or MT5, skipping this cycle", flush=True)

            except TVClientError as e:
                print(f"[POSITIONS] TVClient error for {pos.symbol}: {e}", flush=True)

        events: list[dict] = list(broker_closed_events)
        if prices:
            pos_events = self.executor.check_positions(prices)
            # Filter out tickets already closed by MT5 sync (prevent double P&L)
            for ev in pos_events:
                if ev.get("ticket") not in self._recently_closed:
                    events.append(ev)
        # Prune dedup set to avoid unbounded growth (keep last 100)
        if len(self._recently_closed) > 100:
            self._recently_closed = set(list(self._recently_closed)[-50:])
        return events

    def check_paper_positions(self) -> list[dict]:
        """Check paper shadow positions — Alpaca-first for crypto, TV fallback."""
        if self.paper_shadow is self.executor or not self.paper_shadow.open_positions:
            return []

        shadow_prices: dict[str, float] = {}
        for pos in self.paper_shadow.open_positions.values():
            try:
                alpaca_price = self.price_verifier.get_alpaca_price(pos.symbol)
                if alpaca_price and alpaca_price > 0 and price_in_range(pos.symbol, alpaca_price):
                    shadow_prices[pos.symbol] = alpaca_price
                    continue

                # Try MT5 — strip .cash suffix and exchange prefix for lookup
                mt5_sym = pos.symbol.split(":")[-1].replace(".cash", "")
                mt5_price = self.price_verifier.get_mt5_price(mt5_sym)
                if mt5_price and mt5_price > 0 and price_in_range(pos.symbol, mt5_price):
                    shadow_prices[pos.symbol] = mt5_price
                    continue

                # Last resort: TradingView chart — SKIP for closed-market symbols
                # to avoid switching the chart away from live symbols and blocking
                # the analysis loop. If both Alpaca and MT5 fail, just skip this cycle.
                # The position will be checked again next cycle.
            except Exception as e:
                print(f"[PAPER] Price fetch error for {pos.symbol}: {e}", flush=True)

        if shadow_prices:
            return self.paper_shadow.check_positions(shadow_prices)
        if self.paper_shadow.open_positions and not shadow_prices:
            syms = [p.symbol for p in self.paper_shadow.open_positions.values()]
            print(f"[PAPER] WARNING: no prices resolved for paper positions {syms} — skipping check", flush=True)
        return []

    def mirror_live_to_paper(self, restored_positions: list[dict],
                             fallback_lots_fn: Any = None) -> None:
        """Mirror bridge-opened live positions into paper shadow.

        Only mirrors positions from restored_positions (which come from the
        bridge's own state_store — NOT from MT5 directly).
        """
        paper_symbols_tickets = {
            (p.symbol, p.entry_price) for p in self.paper_shadow.open_positions.values()
        }

        mirrored = 0
        for pos_dict in restored_positions:
            key = (pos_dict.get("symbol", ""), pos_dict.get("entry_price", 0))
            if key in paper_symbols_tickets:
                continue

            decision = TradeDecision(
                action=pos_dict.get("direction", "BUY"),
                symbol=pos_dict.get("symbol", ""),
                entry_price=pos_dict.get("entry_price", 0),
                sl_price=pos_dict.get("sl_price", 0),
                tp_price=pos_dict.get("tp_price", 0),
                tp2_price=pos_dict.get("tp2_price", 0),
                confidence=80,
                risk_pct=pos_dict.get("risk_pct", 0.0075),
                reasoning="Mirrored from live MT5 position on startup",
                grade=pos_dict.get("ict_grade", "B"),
                ict_score=pos_dict.get("ict_score", 0),
                model_used="mirror",
            )
            lot_size = pos_dict.get("lot_size")
            if lot_size is None and fallback_lots_fn:
                lot_size = fallback_lots_fn(decision)
            if lot_size is None:
                lot_size = 0.01
            try:
                result = self.paper_shadow.open_position(decision, lot_size=lot_size)
                if result["success"]:
                    mirrored += 1
                    print(f"  [PAPER] Mirrored live #{pos_dict.get('ticket')} "
                          f"{pos_dict.get('symbol')} @ {pos_dict.get('entry_price')}", flush=True)
            except Exception as e:
                print(f"  [PAPER] Mirror error: {e}", flush=True)

        if mirrored:
            self.paper_state_store.save(self.paper_shadow, "paper_shadow")
            print(f"[PAPER] Mirrored {mirrored} live position(s) into paper shadow", flush=True)

    def reconcile_restored(self) -> None:
        """Check restored positions against live prices — close any that hit SL/TP while bridge was down."""
        if not self.executor.open_positions:
            return

        print("[RECONCILE] Checking restored positions against live prices...", flush=True)

        # Check MT5 for broker-side closes first
        from bridge.live_executor_adapter import LiveExecutorAdapter
        if isinstance(self.executor, LiveExecutorAdapter):
            self._sync_mt5_closed_positions()
            if not self.executor.open_positions:
                print("  [RECONCILE] All positions were closed broker-side (MT5)", flush=True)
                return

        to_close: list[tuple[int, str, float]] = []

        for ticket, pos in list(self.executor.open_positions.items()):
            try:
                # Try Alpaca / MT5 first to avoid chart contention
                price = 0.0
                alpaca_price = self.price_verifier.get_alpaca_price(pos.symbol)
                if alpaca_price and alpaca_price > 0 and price_in_range(pos.symbol, alpaca_price):
                    price = alpaca_price
                if not price:
                    mt5_price = self.price_verifier.get_mt5_price(pos.symbol)
                    if mt5_price and mt5_price > 0 and price_in_range(pos.symbol, mt5_price):
                        price = mt5_price

                if not price:
                    # Also try MT5 with .cash suffix stripped
                    mt5_sym2 = pos.symbol.split(":")[-1].replace(".cash", "")
                    mt5_price2 = self.price_verifier.get_mt5_price(mt5_sym2)
                    if mt5_price2 and mt5_price2 > 0 and price_in_range(pos.symbol, mt5_price2):
                        price = mt5_price2
                if price <= 0:
                    continue

                price_ok, _ = self.price_verifier.verify(pos.symbol, price)
                if not price_ok:
                    print(f"  [RECONCILE] {pos.symbol} price verification failed — skipping", flush=True)
                    continue

                if not price_in_range(pos.symbol, price):
                    continue

                from bridge.risk_bridge import calculate_pnl as calc_pnl
                # Determine effective TP (skip TP check if no TP is set)
                effective_tp = pos.tp2_price if pos.tp2_price > 0 else pos.tp_price

                if pos.direction == "BUY":
                    if pos.sl_price > 0 and price <= pos.sl_price:
                        to_close.append((ticket, "SL (while offline)", pos.sl_price))
                    elif effective_tp > 0 and price >= effective_tp:
                        to_close.append((ticket, "TP (while offline)", effective_tp))
                    else:
                        pnl = calc_pnl(pos.symbol.split(":")[-1], pos.entry_price, price, pos.lot_size, pos.direction)
                        print(f"  [RECONCILE] #{ticket} {pos.symbol} STILL OPEN — price {price:.4f} (PnL {pnl:+.2f})", flush=True)
                else:
                    if pos.sl_price > 0 and price >= pos.sl_price:
                        to_close.append((ticket, "SL (while offline)", pos.sl_price))
                    elif effective_tp > 0 and price <= effective_tp:
                        to_close.append((ticket, "TP (while offline)", effective_tp))
                    else:
                        pnl = calc_pnl(pos.symbol.split(":")[-1], pos.entry_price, price, pos.lot_size, pos.direction)
                        print(f"  [RECONCILE] #{ticket} {pos.symbol} STILL OPEN — price {price:.4f} (PnL {pnl:+.2f})", flush=True)

            except TVClientError as e:
                print(f"  [RECONCILE] Error checking {pos.symbol}: {e}", flush=True)

        for ticket, reason, exit_price in to_close:
            pos = self.executor.open_positions.get(ticket)
            if not pos:
                continue
            from bridge.risk_bridge import calculate_pnl as calc_pnl
            pnl = calc_pnl(pos.symbol.split(":")[-1], pos.entry_price, exit_price, pos.lot_size, pos.direction)
            print(
                f"  [RECONCILE] CLOSING #{ticket} {pos.symbol} — {reason} "
                f"(entry {pos.entry_price:.4f} -> exit {exit_price:.4f}, PnL {pnl:+.2f})",
                flush=True,
            )
            # Use close_position_by_ticket instead of check_positions.
            # check_positions() passes exit_price as the "current market price" which
            # re-triggers SL/TP detection and queues a second MT5 close order — causing
            # double closes and the "@ 0.00000" log when MT5 rejects the redundant order.
            # close_position_by_ticket sends exactly one explicit close and updates state.
            from bridge.live_executor_adapter import LiveExecutorAdapter
            if isinstance(self.executor, LiveExecutorAdapter):
                event = self.executor.close_position_by_ticket(ticket, reason=reason)
                if event:
                    event["reason"] = reason
                    event["exit_price"] = exit_price  # use the SL/TP level, not current_price
                    self.session.log_trade({"event": "CLOSE", **event})
            else:
                # Paper executor: use check_positions with the exit price (no MT5 side-effects)
                prices = {pos.symbol: exit_price}
                events = self.executor.check_positions(prices)
                for event in events:
                    event["reason"] = reason
                    self.session.log_trade({"event": "CLOSE", **event})

        if to_close:
            self.state_store.save(self.executor, self.mode)
            print(f"  [RECONCILE] Closed {len(to_close)} position(s) that hit SL/TP while offline", flush=True)
        elif self.executor.open_positions:
            print(f"  [RECONCILE] All {len(self.executor.open_positions)} position(s) still valid", flush=True)

    def _sync_mt5_closed_positions(self) -> list[dict]:
        """Check MT5 for positions closed broker-side (SL/TP hit on server).

        Returns a list of close events so the caller can forward them to
        alerts/ledger. Historically this logged to the session store only,
        which bypassed the ledger DB and caused positions to remain "open"
        forever in the dashboard.
        """
        events: list[dict] = []
        from bridge.live_executor_adapter import LiveExecutorAdapter
        if not isinstance(self.executor, LiveExecutorAdapter):
            return events
        try:
            import MetaTrader5 as mt5
            if not mt5.terminal_info():
                return events

            to_close = []
            # NOTE: acquire executor._positions_lock when iterating open_positions
            lock = getattr(self.executor, '_positions_lock', None)
            positions_snapshot = {}
            if lock:
                with lock:
                    positions_snapshot = dict(self.executor.open_positions)
            else:
                positions_snapshot = dict(self.executor.open_positions)
            for ticket, pos in positions_snapshot.items():
                mt5_pos = mt5.positions_get(ticket=ticket)
                if mt5_pos is None or len(mt5_pos) == 0:
                    now = datetime.now(timezone.utc)
                    deals = mt5.history_deals_get(
                        now - timedelta(days=3), now,
                        position=ticket
                    )
                    exit_price = pos.current_price
                    pnl = 0.0
                    reason = "BROKER_CLOSE"
                    if deals:
                        ftmo_sym = tv_to_ftmo_symbol(pos.symbol)
                        close_deals = [d for d in deals if d.entry == 1 and d.symbol == ftmo_sym]
                        if close_deals:
                            last_deal = close_deals[-1]
                            exit_price = last_deal.price
                            pnl = last_deal.profit
                            if last_deal.comment and "sl" in last_deal.comment.lower():
                                reason = "SL"
                            elif last_deal.comment and "tp" in last_deal.comment.lower():
                                reason = "TP"

                    to_close.append((ticket, reason, exit_price, pnl))

            for ticket, reason, exit_price, mt5_pnl in to_close:
                from bridge.risk_bridge import calculate_pnl as calc_pnl

                # Acquire lock for all state mutations to avoid races with check_positions
                if lock:
                    lock.acquire()
                try:
                    pos = self.executor.open_positions.get(ticket)
                    if pos is None:
                        continue  # another thread already removed it
                    local_pnl = calc_pnl(pos.symbol, pos.entry_price, exit_price, pos.lot_size, pos.direction)
                    risk_pnl = abs(calc_pnl(pos.symbol, pos.entry_price, pos.sl_price, pos.lot_size, pos.direction))
                    r_multiple = round(local_pnl / risk_pnl, 2) if risk_pnl > 0 else 0.0

                    print(
                        f"  [MT5_SYNC] #{ticket} {pos.symbol} closed by broker ({reason}) "
                        f"Entry={pos.entry_price:.2f} Exit={exit_price:.2f} "
                        f"PnL={local_pnl:+.2f} ({r_multiple:+.1f}R)",
                        flush=True,
                    )

                    self.executor.balance += local_pnl
                    if local_pnl >= 0:
                        self.executor.wins += 1
                    else:
                        self.executor.losses += 1
                        # Set per-symbol loss cooldown if executor supports it
                        if hasattr(self.executor, 'set_symbol_loss_cooldown'):
                            self.executor.set_symbol_loss_cooldown(pos.symbol)
                        # Global loss cooldown — pause all trading after any loss
                        if hasattr(self.executor, 'set_global_loss_cooldown'):
                            self.executor.set_global_loss_cooldown()
                    del self.executor.open_positions[ticket]
                finally:
                    if lock:
                        lock.release()

                # Track recently closed to prevent double P&L counting
                self._recently_closed.add(ticket)
                events.append({
                    "ticket": ticket,
                    "symbol": pos.symbol,
                    "direction": pos.direction,
                    "entry": pos.entry_price,
                    "exit_price": exit_price,
                    "pnl": round(local_pnl, 2),
                    "r_multiple": r_multiple,
                    "reason": reason,
                    "balance": round(self.executor.balance, 2),
                    "mt5_pnl": round(mt5_pnl, 2) if mt5_pnl else None,
                })
            if events:
                # Re-sync balance from MT5 to correct for tick_value drift
                try:
                    info = mt5.account_info()
                    if info:
                        self.executor.balance = info.balance
                        print(f"  [SYNC] Balance re-synced from MT5: ${info.balance:,.2f}", flush=True)
                except Exception as e:
                    print(f"  [SYNC] MT5 balance re-sync failed: {e}", flush=True)
                self.state_store.save(self.executor, self.mode)

        except ImportError:
            pass
        except Exception as e:
            print(f"[MT5_SYNC] Error checking MT5 positions: {e}", flush=True)
        return events

    def reconcile_mt5_on_startup(self, watchlist: list[str]) -> list[dict]:
        """Scan MT5 for ICT_Bridge positions missing from bridge state.

        On startup, query MT5 for ALL open positions with "ICT_Bridge" in the
        comment. Any position not already in self.executor.open_positions gets
        re-adopted into bridge state so we can track its SL/TP/close.

        Also checks recent deal history for ICT_Bridge trades that closed while
        the bridge had no record of them (the US30 #425415101 scenario).

        Returns list of newly adopted position dicts (for display).
        """
        from bridge.live_executor_adapter import LiveExecutorAdapter
        if not isinstance(self.executor, LiveExecutorAdapter):
            return []

        try:
            import MetaTrader5 as mt5
            if not mt5.terminal_info():
                print("[MT5_RECON] MT5 terminal not connected — skipping reconciliation", flush=True)
                return []
        except ImportError:
            return []

        # Build reverse map: FTMO symbol -> full TV symbol (with exchange prefix)
        # e.g. "US30.cash" -> "CBOT:YM1!"
        ftmo_to_full_tv: dict[str, str] = {}
        for tv_sym in watchlist:
            base = tv_sym.split(":")[-1]
            ftmo_sym = TV_TO_FTMO.get(base, base)
            ftmo_to_full_tv[ftmo_sym] = tv_sym

        known_tickets = set(self.executor.open_positions.keys())
        adopted: list[dict] = []

        # --- Phase 1: Find open MT5 positions tagged ICT_Bridge not in bridge state ---
        try:
            all_positions = mt5.positions_get()
            print(f"[MT5_RECON] MT5 returned {len(all_positions) if all_positions else 0} total positions, known_tickets={known_tickets}", flush=True)
            if all_positions:
                for pos in all_positions:
                    comment = pos.comment or ""
                    if "ICT_Bridge" not in comment:
                        continue
                    if pos.ticket in known_tickets:
                        print(f"  [MT5_RECON] #{pos.ticket} {pos.symbol} already known — skipping", flush=True)
                        continue

                    # Convert MT5 symbol back to TV symbol
                    mt5_sym = pos.symbol
                    tv_symbol = ftmo_to_full_tv.get(mt5_sym, mt5_sym)

                    direction = "BUY" if pos.type == 0 else "SELL"

                    # Try to get SL/TP from the MT5 position
                    sl_price = pos.sl if pos.sl > 0 else 0.0
                    tp_price = pos.tp if pos.tp > 0 else 0.0

                    # Restore persisted position state if this ticket was
                    # tracked before the restart. Without this, we lose trail
                    # progress, TP targets, grade, and reasoning — which
                    # silently disables TP management for adopted positions.
                    persisted = getattr(self.executor, "_persisted_trail_state", {}) or {}
                    trail_state = persisted.get(str(pos.ticket)) or {}
                    restored_trail = trail_state.get("trailing_sl", sl_price)
                    restored_tp1_hit = bool(trail_state.get("tp1_hit", False))
                    restored_desync = bool(trail_state.get("trail_desync", False))
                    restored_desired = trail_state.get("desired_sl", restored_trail)
                    # Two-tier TP / grade / reasoning — previously dropped
                    # at restart, causing silent TP-management disable.
                    restored_tp = float(trail_state.get("tp_price", 0.0) or 0.0)
                    restored_tp2 = float(trail_state.get("tp2_price", 0.0) or 0.0)
                    restored_grade = str(trail_state.get("ict_grade", "") or "?")
                    restored_score = float(trail_state.get("ict_score", 0.0) or 0.0)
                    restored_trade_type = str(trail_state.get("trade_type", "") or "")
                    restored_risk = float(trail_state.get("risk_pct", 0.0) or 0.01)
                    restored_opened = str(trail_state.get("opened_at", "") or "")
                    restored_reasoning = str(trail_state.get("reasoning", "") or "")

                    # Prefer the broker's actual SL if it's MORE favorable than
                    # what we had persisted — broker-side trail (rare) or manual
                    # adjustment should not be walked back.
                    if direction == "BUY" and sl_price > restored_trail:
                        restored_trail = sl_price
                    elif direction == "SELL" and 0 < sl_price < restored_trail:
                        restored_trail = sl_price

                    # Prefer persisted TP over MT5's tp field — for two-tier
                    # trades the bridge intentionally sets MT5 tp=0 and manages
                    # TP1/TP2 internally. If nothing persisted (first run ever),
                    # fall back to MT5's tp field.
                    effective_tp = restored_tp if restored_tp > 0 else tp_price
                    effective_trade_type = (
                        restored_trade_type
                        or self._infer_trade_type(pos.price_open, sl_price, effective_tp)
                    )
                    effective_reasoning = (
                        restored_reasoning
                        or f"Adopted from MT5 on startup (comment: {comment})"
                    )
                    effective_opened = (
                        restored_opened
                        or datetime.fromtimestamp(pos.time, tz=timezone.utc).isoformat()
                    )

                    paper_pos = PaperPosition(
                        ticket=pos.ticket,
                        symbol=tv_symbol,
                        direction=direction,
                        entry_price=pos.price_open,
                        sl_price=sl_price,
                        tp_price=effective_tp,
                        tp2_price=restored_tp2,
                        lot_size=pos.volume,
                        risk_pct=restored_risk,
                        opened_at=effective_opened,
                        ict_grade=restored_grade,
                        ict_score=restored_score,
                        reasoning=effective_reasoning,
                        trade_type=effective_trade_type,
                        trailing_sl=restored_trail,
                        tp1_hit=restored_tp1_hit,
                        current_price=pos.price_current,
                    )
                    # Attach desync recovery flags so next check cycle re-syncs SL
                    if restored_desync:
                        paper_pos._trail_desync = True
                        paper_pos._desired_sl = restored_desired
                    self.executor.open_positions[pos.ticket] = paper_pos
                    if trail_state:
                        print(
                            f"  [MT5_RECON] #{pos.ticket} restored trail state: "
                            f"trailing_sl={restored_trail} tp1_hit={restored_tp1_hit} "
                            f"desync={restored_desync}",
                            flush=True,
                        )

                    # CRITICAL: also register with the underlying LiveExecutor's
                    # open_tickets dict. modify_sl/close_position check this dict
                    # and silently return False if the ticket isn't there — which
                    # is why trailing SL never syncs to MT5 for re-adopted trades.
                    if hasattr(self.executor, "_live") and hasattr(self.executor._live, "open_tickets"):
                        self.executor._live.open_tickets[pos.ticket] = {
                            "symbol": mt5_sym,           # FTMO-side symbol for MT5 API
                            "tv_symbol": tv_symbol,
                            "direction": direction,
                            "entry_price": pos.price_open,
                            "sl_price": sl_price,
                            "tp_price": tp_price,
                            "lot_size": pos.volume,
                            "opened_at": paper_pos.opened_at,
                            "adopted": True,
                        }

                    # Advance ticket counter
                    if hasattr(self.executor, "_next_ticket"):
                        self.executor._next_ticket = max(
                            self.executor._next_ticket, pos.ticket + 1
                        )

                    unrealized = pos.profit
                    print(
                        f"  [MT5_RECON] ADOPTED #{pos.ticket} {direction} {tv_symbol} "
                        f"({mt5_sym}) Entry={pos.price_open:.2f} "
                        f"SL={sl_price:.2f} TP={tp_price:.2f} "
                        f"Lots={pos.volume} Unrealized={unrealized:+.2f}",
                        flush=True,
                    )
                    adopted.append(paper_pos.to_dict())

        except Exception as e:
            print(f"[MT5_RECON] Error scanning open positions: {e}", flush=True)

        # --- Phase 2: Find recently closed ICT_Bridge deals not in bridge state ---
        try:
            now = datetime.now(timezone.utc)
            # Look back 7 days to catch swing trades
            deals = mt5.history_deals_get(now - timedelta(days=7), now)
            if deals:
                # Group close deals (entry==1) by position ticket
                close_deals: dict[int, Any] = {}
                for d in deals:
                    if d.entry == 1 and d.comment and "ICT_Bridge" in d.comment:
                        close_deals[d.position_id] = d

                # Also find open deals to get entry prices
                open_deals: dict[int, Any] = {}
                for d in deals:
                    if d.entry == 0 and d.position_id in close_deals:
                        open_deals[d.position_id] = d

                for pos_id, close_deal in close_deals.items():
                    if pos_id in known_tickets:
                        continue  # already tracked
                    if pos_id in {p["ticket"] for p in adopted}:
                        continue  # just adopted as open

                    mt5_sym = close_deal.symbol
                    tv_symbol = ftmo_to_full_tv.get(mt5_sym, mt5_sym)
                    entry_deal = open_deals.get(pos_id)
                    entry_price = entry_deal.price if entry_deal else 0.0

                    reason = "BROKER_CLOSE"
                    if close_deal.comment:
                        lc = close_deal.comment.lower()
                        if "tp" in lc:
                            reason = "TP"
                        elif "sl" in lc:
                            reason = "SL"

                    print(
                        f"  [MT5_RECON] MISSED CLOSE #{pos_id} {tv_symbol} ({mt5_sym}) "
                        f"Entry={entry_price:.2f} Exit={close_deal.price:.2f} "
                        f"PnL={close_deal.profit:+.2f} Reason={reason} "
                        f"@ {datetime.fromtimestamp(close_deal.time, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')}",
                        flush=True,
                    )

        except Exception as e:
            print(f"[MT5_RECON] Error scanning deal history: {e}", flush=True)

        if adopted:
            self.state_store.save(self.executor, self.mode)
            print(f"[MT5_RECON] Adopted {len(adopted)} position(s) from MT5 into bridge state", flush=True)
        else:
            print("[MT5_RECON] All MT5 ICT_Bridge positions accounted for", flush=True)

        # --- Safety net: ensure open_tickets is populated for ALL known positions ---
        # state_store.restore_into should handle this, but verify and fix any gaps.
        if hasattr(self.executor, "_live") and hasattr(self.executor._live, "open_tickets"):
            live_tickets = self.executor._live.open_tickets
            for ticket, pos_data in self.executor.open_positions.items():
                if ticket not in live_tickets:
                    from bridge.config import tv_to_ftmo_symbol
                    ftmo_sym = tv_to_ftmo_symbol(pos_data.symbol)
                    live_tickets[ticket] = {
                        "symbol": ftmo_sym,
                        "tv_symbol": pos_data.symbol,
                        "direction": pos_data.direction,
                        "entry_price": pos_data.entry_price,
                        "sl_price": pos_data.sl_price,
                        "tp_price": pos_data.tp_price,
                        "tp2_price": getattr(pos_data, "tp2_price", 0.0),
                        "tp1_hit": getattr(pos_data, "tp1_hit", False),
                        "lot_size": pos_data.lot_size,
                        "opened_at": getattr(pos_data, "opened_at", ""),
                    }
            n_tickets = len(live_tickets)
            if n_tickets > 0:
                print(f"[MT5_RECON] open_tickets verified: {n_tickets} position(s) registered for MT5 modify/close", flush=True)

            # Sync any trailing SL that advanced in-memory but never reached MT5
            # (happens when previous session had open_tickets empty due to the old bug)
            import MetaTrader5 as mt5
            import threading
            synced = 0
            for ticket, pos_data in self.executor.open_positions.items():
                trailing = getattr(pos_data, "trailing_sl", 0.0)
                if trailing <= 0 or trailing == pos_data.sl_price:
                    continue
                # Check if MT5's SL is behind the in-memory trailing
                mt5_pos = mt5.positions_get(ticket=ticket)
                if not mt5_pos:
                    continue
                mt5_sl = mt5_pos[0].sl
                needs_sync = False
                if pos_data.direction == "BUY" and trailing > mt5_sl:
                    needs_sync = True
                elif pos_data.direction == "SELL" and (mt5_sl == 0 or trailing < mt5_sl):
                    needs_sync = True
                if needs_sync:
                    # Run in a thread to avoid "event loop already running" error
                    import asyncio
                    result_holder = [False]
                    def _sync_sl():
                        loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(loop)
                        try:
                            result_holder[0] = loop.run_until_complete(
                                self.executor._live.modify_sl(ticket, trailing)
                            )
                        finally:
                            loop.close()
                    t = threading.Thread(target=_sync_sl, daemon=True)
                    t.start()
                    t.join(timeout=10)
                    if result_holder[0]:
                        synced += 1
                        print(
                            f"  [MT5_RECON] #{ticket} trailing SL synced to MT5: {mt5_sl:.2f} -> {trailing:.2f}",
                            flush=True,
                        )
                    else:
                        print(f"  [MT5_RECON] #{ticket} trailing SL sync FAILED ({trailing:.2f})", flush=True)
            if synced > 0:
                print(f"[MT5_RECON] Synced {synced} stale trailing SL(s) to MT5", flush=True)

        return adopted
