"""Position and Trade Management Service for TokenIQ.

Authoritatively synchronizes positions with Delta Exchange, computes realized/unrealized P&L,
records completed trades, and monitors automated stop-loss and take-profit exit triggers.
"""

import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from sqlalchemy.orm import Session
from app.config import settings
from app.core.logger import logger
from app.core.state import runtime_state
from app.exchange.client import exchange_client
from app.exchange.models import ExchangePosition
from app.models.position import Position
from app.models.trade import Trade
from app.models.order import Order
from app.models.event import SystemEvent


class PositionManager:
    """Synchronizes positions, computes P&L, and enforces risk-based exit conditions."""

    async def synchronize_positions(
        self,
        db: Session,
        client: Optional["DeltaExchangeClient"] = None,
    ) -> List[Position]:
        """Fetches authoritative positions from Delta Exchange and updates local database."""
        c = client or exchange_client
        try:
            exchange_positions: List[ExchangePosition] = await c.get_positions()
        except Exception as e:
            logger.error(f"Failed to fetch exchange positions for reconciliation: {e}")
            return []

        synced_positions = []
        now = datetime.now(timezone.utc)

        for ep in exchange_positions:
            pos = db.query(Position).filter(Position.instrument == ep.product_symbol).first()
            if not pos:
                pos = Position(
                    instrument=ep.product_symbol,
                    size=ep.size,
                    entry_price=ep.entry_price,
                    mark_price=ep.mark_price,
                    unrealized_pnl=ep.unrealized_pnl,
                    realized_pnl=ep.realized_pnl,
                    liquidation_price=ep.liquidation_price,
                    updated_at=now,
                )
                db.add(pos)
            else:
                pos.size = ep.size
                pos.entry_price = ep.entry_price
                pos.mark_price = ep.mark_price
                pos.unrealized_pnl = ep.unrealized_pnl
                pos.realized_pnl = ep.realized_pnl
                pos.liquidation_price = ep.liquidation_price
                pos.updated_at = now

            synced_positions.append(pos)

        active_symbols = {ep.product_symbol for ep in exchange_positions}
        for p in db.query(Position).all():
            if p.instrument not in active_symbols and p.size != 0.0:
                p.size = 0.0
                p.unrealized_pnl = 0.0
                p.updated_at = now

        db.commit()
        runtime_state.last_reconciliation_time = now.isoformat()
        return synced_positions

    def record_fill_as_trade(
        self,
        db: Session,
        order: Order,
        fill_price: float,
        fill_qty: float,
    ) -> Trade:
        """Creates a Trade record upon order fill and computes realized P&L if closing a position."""
        pos = db.query(Position).filter(Position.instrument == order.instrument).first()
        prev_size = pos.size if pos else 0.0
        prev_entry = pos.entry_price if pos else fill_price

        realized_pnl = 0.0
        fees = round(fill_price * fill_qty * 0.0005, 4)  # 5 bps estimated fee

        # Calculate realized PnL if this fill reduces or closes existing position
        if prev_size > 0 and order.side == "sell":
            # Closing long position
            closed_qty = min(prev_size, fill_qty)
            realized_pnl = (fill_price - prev_entry) * closed_qty - fees
        elif prev_size < 0 and order.side == "buy":
            # Closing short position
            closed_qty = min(abs(prev_size), fill_qty)
            realized_pnl = (prev_entry - fill_price) * closed_qty - fees

        trade = Trade(
            order_id=order.internal_id,
            instrument=order.instrument,
            side=order.side,
            fill_price=fill_price,
            fill_quantity=fill_qty,
            fees=fees,
            realized_pnl=round(realized_pnl, 2),
            timestamp=datetime.now(timezone.utc),
        )
        db.add(trade)

        # Update or create Position record
        now_dt = datetime.now(timezone.utc)
        if not pos:
            new_pos_size = fill_qty if order.side == "buy" else -fill_qty
            pos = Position(
                instrument=order.instrument,
                size=round(new_pos_size, 4),
                entry_price=fill_price,
                mark_price=fill_price,
                unrealized_pnl=0.0,
                realized_pnl=round(realized_pnl, 2),
                updated_at=now_dt,
            )
            db.add(pos)
        else:
            if order.side == "buy":
                if prev_size >= 0:
                    new_size = prev_size + fill_qty
                    new_entry = (prev_entry * prev_size + fill_price * fill_qty) / new_size if new_size > 0 else fill_price
                else:
                    new_size = prev_size + fill_qty
                    new_entry = prev_entry if new_size < 0 else (fill_price if new_size > 0 else 0.0)
            else:  # sell
                if prev_size <= 0:
                    new_size = prev_size - fill_qty
                    new_entry = (prev_entry * abs(prev_size) + fill_price * fill_qty) / abs(new_size) if abs(new_size) > 0 else fill_price
                else:
                    new_size = prev_size - fill_qty
                    new_entry = prev_entry if new_size > 0 else (fill_price if new_size < 0 else 0.0)
            pos.size = round(new_size, 4)
            pos.entry_price = round(new_entry, 2)
            pos.mark_price = fill_price
            pos.realized_pnl = round((pos.realized_pnl or 0.0) + realized_pnl, 2)
            pos.updated_at = now_dt

        if realized_pnl != 0.0:
            runtime_state.add_realized_pnl(realized_pnl)
            if realized_pnl > 0:
                runtime_state.increment_metric("winning_trades")
            else:
                runtime_state.increment_metric("losing_trades")

        runtime_state.increment_metric("trades_completed")
        db.commit()
        db.refresh(trade)

        logger.info(
            f"Trade recorded for order {order.internal_id}: {order.side.upper()} {fill_qty} @ {fill_price} | "
            f"Realized PnL: ${realized_pnl:.2f}"
        )
        return trade

    def check_stop_loss_or_take_profit(
        self,
        pos: Position,
        current_price: float,
    ) -> Optional[Tuple[str, str]]:
        """Checks if current position has breached stop-loss or take-profit thresholds.

        Returns (action: 'BUY'|'SELL', reason) if exit triggered, otherwise None.
        """
        if pos.size == 0 or pos.entry_price <= 0:
            return None

        # Long position
        if pos.size > 0:
            pnl_pct = (current_price - pos.entry_price) / pos.entry_price
            if pnl_pct <= -settings.STOP_LOSS_PCT:
                return "SELL", f"Stop-loss breached: loss is {pnl_pct*100:.2f}% <= -{settings.STOP_LOSS_PCT*100:.1f}%"
            elif pnl_pct >= settings.TAKE_PROFIT_PCT:
                return "SELL", f"Take-profit hit: gain is +{pnl_pct*100:.2f}% >= +{settings.TAKE_PROFIT_PCT*100:.1f}%"

        # Short position
        elif pos.size < 0:
            pnl_pct = (pos.entry_price - current_price) / pos.entry_price
            if pnl_pct <= -settings.STOP_LOSS_PCT:
                return "BUY", f"Stop-loss breached: loss is {pnl_pct*100:.2f}% <= -{settings.STOP_LOSS_PCT*100:.1f}%"
            elif pnl_pct >= settings.TAKE_PROFIT_PCT:
                return "BUY", f"Take-profit hit: gain is +{pnl_pct*100:.2f}% >= +{settings.TAKE_PROFIT_PCT*100:.1f}%"

        return None

    async def close_position(
        self,
        db: Session,
        instrument: str,
        client: Optional["DeltaExchangeClient"] = None,
    ) -> Dict[str, Any]:
        """Closes an open position authoritatively on exchange and records realized P&L."""
        from app.exchange.models import ExchangeOrderPayload
        c = client or exchange_client
        pos = db.query(Position).filter(Position.instrument == instrument).first()
        if not pos or abs(pos.size) == 0:
            return {"success": False, "message": f"No open position found for {instrument}"}

        opposing_side = "sell" if pos.size > 0 else "buy"
        qty = abs(pos.size)

        payload = ExchangeOrderPayload(
            product_symbol=instrument,
            size=qty,
            side=opposing_side,
            order_type="market_order",
            client_order_id=f"close_{instrument}_{int(time.time()*1000)}" if 'time' in globals() else f"close_{instrument}",
        )

        try:
            result = await c.place_order(payload)
            if not result.success and not result.order_id:
                return {"success": False, "message": f"Exchange rejected close order: {result.error_message}"}

            fill_price = result.fill_price or pos.mark_price or 65000.0

            # Calculate realized P&L
            fees = round(fill_price * qty * 0.0005, 4)
            if pos.size > 0:
                realized_pnl = (fill_price - pos.entry_price) * qty - fees
            else:
                realized_pnl = (pos.entry_price - fill_price) * qty - fees

            trade = Trade(
                order_id=result.order_id or "close_order",
                instrument=instrument,
                side=opposing_side,
                fill_price=fill_price,
                fill_quantity=qty,
                fees=fees,
                realized_pnl=round(realized_pnl, 2),
                timestamp=datetime.now(timezone.utc),
            )
            db.add(trade)

            pos.realized_pnl += round(realized_pnl, 2)
            pos.size = 0.0
            pos.unrealized_pnl = 0.0
            pos.updated_at = datetime.now(timezone.utc)

            runtime_state.add_realized_pnl(realized_pnl)
            runtime_state.increment_metric("trades_completed")
            if realized_pnl > 0:
                runtime_state.increment_metric("winning_trades")
            else:
                runtime_state.increment_metric("losing_trades")

            db.commit()
            db.refresh(pos)

            return {
                "success": True,
                "message": f"Successfully closed {instrument} position.",
                "fill_price": fill_price,
                "closed_quantity": qty,
                "realized_pnl": round(realized_pnl, 2),
                "fees": fees,
            }
        except Exception as e:
            logger.error(f"Failed to execute position close for {instrument}: {e}")
            return {"success": False, "message": str(e)}


position_manager = PositionManager()
