"""
Smoke test: LIMIT BUY abaixo do melhor bid (tende a não executar de imediato) + cancelamento.
Usa exchangeInfo para stepSize, tickSize, minNotional.
"""

from __future__ import annotations

import uuid
from decimal import Decimal, ROUND_FLOOR
from typing import Any

from .binance_public import BinancePublic
from .binance_signed import BinanceSigned


def _filters_by_type(symbol_info: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for f in symbol_info.get("filters", []) or []:
        t = str(f.get("filterType", ""))
        if t:
            out[t] = f
    return out


def _symbol_entry(exchange_info: dict[str, Any], symbol: str) -> dict[str, Any] | None:
    sym = symbol.upper()
    for s in exchange_info.get("symbols", []) or []:
        if str(s.get("symbol", "")).upper() == sym:
            return s
    return None


def run_limit_buy_place_and_cancel(
    symbol: str,
    *,
    pub: BinancePublic,
    signed: BinanceSigned,
    ticks_below_best_bid: int = 20,
) -> dict[str, Any]:
    """
    Coloca BUY LIMIT GTC `ticks_below_best_bid` ticks abaixo do melhor bid; cancela em seguida.

    `ticks_below_best_bid` default 20 — costuma manter a ordem fora do match imediato
    e dentro de filtros típicos (PERCENT_PRICE, etc.) em majors testnet.
    """
    sym = symbol.upper()
    info = pub.exchange_info(sym)
    se = _symbol_entry(info, sym)
    if not se:
        return {"ok": False, "error": "symbol_not_in_exchange_info", "symbol": sym}

    ft = _filters_by_type(se)
    lot = ft.get("LOT_SIZE") or {}
    pf = ft.get("PRICE_FILTER") or {}
    notion_f = ft.get("MIN_NOTIONAL") or ft.get("NOTIONAL") or {}

    step_size = Decimal(str(lot.get("stepSize", "0.00001")))
    tick_size = Decimal(str(pf.get("tickSize", "0.01")))
    min_qty = Decimal(str(lot.get("minQty", "0")))
    min_price = Decimal(str(pf.get("minPrice", "0")))
    min_notional = Decimal(str(notion_f.get("minNotional", notion_f.get("notional", "5"))))

    depth = pub.depth(sym, 20)
    bids = depth.get("bids") or []
    if not bids:
        return {"ok": False, "error": "empty_book", "symbol": sym}

    best_bid = Decimal(str(bids[0][0]))
    raw_price = best_bid - Decimal(ticks_below_best_bid) * tick_size
    if raw_price < min_price:
        raw_price = min_price
    price_dec = (raw_price / tick_size).to_integral_value(rounding=ROUND_FLOOR) * tick_size
    if price_dec <= 0 or price_dec >= best_bid:
        cand = best_bid - tick_size
        price_dec = max(cand, min_price)
        price_dec = (price_dec / tick_size).to_integral_value(rounding=ROUND_FLOOR) * tick_size

    need_qty = (min_notional * Decimal("1.05")) / price_dec
    qty_dec = (need_qty / step_size).to_integral_value(rounding=ROUND_FLOOR) * step_size
    if qty_dec < min_qty:
        qty_dec = min_qty
    if qty_dec * price_dec < min_notional:
        need_qty2 = (min_notional * Decimal("1.1")) / price_dec
        qty_dec = (need_qty2 / step_size).to_integral_value(rounding=ROUND_FLOOR) * step_size
        if qty_dec < min_qty:
            qty_dec = min_qty

    price_s = _strip_trailing_zeros(format(price_dec, "f"))
    qty_s = _strip_trailing_zeros(format(qty_dec, "f"))

    cid = f"mt{uuid.uuid4().hex[:12]}"
    new = signed.new_order(
        sym,
        "BUY",
        "LIMIT",
        time_in_force="GTC",
        quantity=qty_s,
        price=price_s,
        new_client_order_id=cid,
    )
    oid = new.get("orderId")
    cancel: dict[str, Any] = {}
    if oid is not None:
        cancel = signed.cancel_order(sym, order_id=int(oid))

    return {
        "ok": True,
        "symbol": sym,
        "best_bid": str(best_bid),
        "price": price_s,
        "quantity": qty_s,
        "notional_approx": str(qty_dec * price_dec),
        "new_order": new,
        "cancel": cancel,
    }


def _strip_trailing_zeros(s: str) -> str:
    if "." not in s:
        return s
    return s.rstrip("0").rstrip(".") or "0"
