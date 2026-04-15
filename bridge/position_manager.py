"""
Position lifecycle management — SL/TP checking, MT5 sync, paper shadow, reconciliation.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any

from bridge.config import price_in_range
from bridge.decision_types import TradeDecision
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

                # Fallback: TradingView chart quote
                target_sym = pos.symbol.split(":")[-1]
                result = self.tv_client.set_symbol(pos.symbol, require_ready=True)
                if not result.get("chart_ready", False):
                    print(f"[POSITIONS] Chart not ready for {pos.symbol} — skipping this cycle", flush=True)
                    continue

                quote = self.tv_client.get_quote()
                chart_sym = quote.get("symbol", "").split(":")[-1]
                if chart_sym != target_sym:
                    print(f"[POSITIONS] Symbol mismatch: expected {target_sym}, got {chart_sym}", flush=True)
                    continue

                p = float(quote.get("last") or quote.get("lp") or quote.get("close") or 0)
                if p <= 0:
                    print(f"[POSITIONS] Zero price for {pos.symbol}", flush=True)
                    continue

                if not price_in_range(pos.symbol, p):
                    print(f"[POSITIONS] {pos.symbol} price {p:.4f} FAILED range check", flush=True)
                    continue

                prices[pos.symbol] = p

            except TVClientError as e:
                print(f"[POSITIONS] TVClient error for {pos.symbol}: {e}", flush=True)

        events: list[dict] = list(broker_closed_events)
        if prices:
            events.extend(self.executor.check_positions(prices))
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

                sym_base = pos.symbol.split(":")[-1]
                result = self.tv_client.set_symbol(pos.symbol, require_ready=True)
                if not result.get("chart_ready", False):
                    continue
                quote = self.tv_client.get_quote()
                chart_sym = quote.get("symbol", "").split(":")[-1]
                if chart_sym != sym_base:
                    continue
                p = float(quote.get("last") or quote.get("lp") or quote.get("close") or 0)
                if p > 0 and price_in_range(pos.symbol, p):
                    shadow_prices[pos.symbol] = p
            except Exception:
                pass

        if shadow_prices:
            return self.paper_shadow.check_positions(shadow_prices)
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
                target_sym = pos.symbol.split(":")[-1]
                result = self.tv_client.set_symbol(pos.symbol, require_ready=True)
                if not result.get("chart_ready", False):
                    print(f"  [RECONCILE] {pos.symbol} chart not ready — will check in position loop", flush=True)
                    continue

                quote = self.tv_client.get_quote()
                chart_sym = quote.get("symbol", "").split(":")[-1]
                if chart_sym != target_sym:
                    print(f"  [RECONCILE] Symbol mismatch for {pos.symbol} — skipping", flush=True)
                    continue

                price = float(quote.get("last") or quote.get("lp") or quote.get("close") or 0)
                if price <= 0:
                    continue

                price_ok, _ = self.price_verifier.verify(pos.symbol, price)
                if not price_ok:
                    print(f"  [RECONCILE] {pos.symbol} price verification failed — skipping", flush=True)
                    continue

                if not price_in_range(pos.symbol, price):
                    continue

                if pos.direction == "BUY":
                    if price <= pos.sl_price:
                        to_close.append((ticket, "SL (while offline)", pos.sl_price))
                    elif price >= (pos.tp2_price if pos.tp2_price > 0 else pos.tp_price):
                        exit_p = pos.tp2_price if pos.tp2_price > 0 else pos.tp_price
                        to_close.append((ticket, "TP (while offline)", exit_p))
                    else:
                        pnl = (price - pos.entry_price) * pos.lot_size
                        print(f"  [RECONCILE] #{ticket} {pos.symbol} STILL OPEN — price {price:.4f} (PnL {pnl:+.2f})", flush=True)
                else:
                    if price >= pos.sl_price:
                        to_close.append((ticket, "SL (while offline)", pos.sl_price))
                    elif price <= (pos.tp2_price if pos.tp2_price > 0 else pos.tp_price):
                        exit_p = pos.tp2_price if pos.tp2_price > 0 else pos.tp_price
                        to_close.append((ticket, "TP (while offline)", exit_p))
                    else:
                        pnl = (pos.entry_price - price) * pos.lot_size
                        print(f"  [RECONCILE] #{ticket} {pos.symbol} STILL OPEN — price {price:.4f} (PnL {pnl:+.2f})", flush=True)

            except TVClientError as e:
                print(f"  [RECONCILE] Error checking {pos.symbol}: {e}", flush=True)

        for ticket, reason, exit_price in to_close:
            pos = self.executor.open_positions.get(ticket)
            if not pos:
                continue
            if pos.direction == "BUY":
                pnl = (exit_price - pos.entry_price) * pos.lot_size
            else:
                pnl = (pos.entry_price - exit_price) * pos.lot_size
            print(
                f"  [RECONCILE] CLOSING #{ticket} {pos.symbol} — {reason} "
                f"(entry {pos.entry_price:.4f} -> exit {exit_price:.4f}, PnL {pnl:+.2f})",
                flush=True,
            )
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
            for ticket, pos in self.executor.open_positions.items():
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
                        mt5_sym = pos.symbol.split(":")[-1]
                        close_deals = [d for d in deals if d.entry == 1 and d.symbol == mt5_sym]
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
                pos = self.executor.open_positions[ticket]
                if pos.direction == "BUY":
                    local_pnl = (exit_price - pos.entry_price) * pos.lot_size
                else:
                    local_pnl = (pos.entry_price - exit_price) * pos.lot_size
                sl_dist = abs(pos.entry_price - pos.sl_price)
                r_multiple = round(local_pnl / (sl_dist * pos.lot_size), 2) if sl_dist > 0 else 0.0

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
                del self.executor.open_positions[ticket]

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
                self.state_store.save(self.executor, self.mode)

        except ImportError:
            pass
        except Exception as e:
            print(f"[MT5_SYNC] Error checking MT5 positions: {e}", flush=True)
        return events
