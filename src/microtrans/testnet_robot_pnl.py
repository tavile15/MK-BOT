from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .binance_public import BinancePublic
from .binance_signed import BinanceSigned
from .symbols import split_pair


def extract_order_ids_from_executor_jsonl(path: Path) -> set[int]:
    ids: set[int] = set()
    if not path.exists():
        return ids
    try:
        for ln in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            s = ln.strip()
            if not s:
                continue
            try:
                row = json.loads(s)
            except Exception:
                continue
            for od in row.get("placed") or []:
                oid = od.get("orderId")
                if oid is None:
                    continue
                try:
                    ids.add(int(oid))
                except Exception:
                    continue
    except Exception:
        return set()
    return ids


def fetch_my_trades_paginated(
    *,
    signed: BinanceSigned,
    symbol: str,
    limit: int = 1000,
    max_pages: int = 100,
) -> tuple[list[dict[str, Any]], int]:
    out: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    from_id: int | None = None
    pages = 0
    for _ in range(max_pages):
        batch = signed.my_trades(symbol, limit=limit, from_id=from_id)
        pages += 1
        if not batch:
            break
        max_trade_id = -1
        fresh_in_page = 0
        for t in batch:
            tid_raw = t.get("id")
            try:
                tid = int(tid_raw) if tid_raw is not None else -1
            except Exception:
                tid = -1
            if tid >= 0 and tid in seen_ids:
                continue
            if tid >= 0:
                seen_ids.add(tid)
                if tid > max_trade_id:
                    max_trade_id = tid
            out.append(t)
            fresh_in_page += 1
        if len(batch) < limit:
            break
        if max_trade_id < 0 or fresh_in_page == 0:
            break
        from_id = max_trade_id + 1
    return out, pages


def compute_robot_pnl_from_real_fills(
    *,
    symbol: str,
    jsonl_path: Path,
    pub: BinancePublic,
    signed: BinanceSigned,
) -> dict[str, Any]:
    sym = str(symbol or "BTCUSDT").strip().upper()
    base_a, quote_a = split_pair(sym)
    order_ids = extract_order_ids_from_executor_jsonl(jsonl_path)
    if not order_ids:
        return {
            "ok": True,
            "symbol": sym,
            "fills_count": 0,
            "orders_count": 0,
            "delta_quote": 0.0,
            "delta_base": 0.0,
            "mark_price": 0.0,
            "pnl_robot_total_quote": 0.0,
            "quote_asset": quote_a,
            "base_asset": base_a,
        }
    trs, pages = fetch_my_trades_paginated(signed=signed, symbol=sym, limit=1000)
    rel = [t for t in trs if int(t.get("orderId") or -1) in order_ids]
    buy_quote = 0.0
    sell_quote = 0.0
    buy_base = 0.0
    sell_base = 0.0
    fee_quote = 0.0
    for t in rel:
        qty = float(t.get("qty") or 0.0)
        qq = float(t.get("quoteQty") or 0.0)
        comm = float(t.get("commission") or 0.0)
        comm_a = str(t.get("commissionAsset") or "").upper()
        is_buyer = bool(t.get("isBuyer"))
        if is_buyer:
            buy_quote += qq
            buy_base += qty
            if comm_a == base_a:
                buy_base -= comm
            elif comm_a == quote_a:
                fee_quote += comm
        else:
            sell_quote += qq
            sell_base += qty
            if comm_a == base_a:
                sell_base += comm
            elif comm_a == quote_a:
                fee_quote += comm
    delta_quote = sell_quote - buy_quote - fee_quote
    delta_base = buy_base - sell_base
    tk = pub.ticker_24h(sym)
    mark = float(tk.get("lastPrice") or 0.0)
    pnl_total = delta_quote + (delta_base * mark if mark > 0 else 0.0)
    return {
        "ok": True,
        "symbol": sym,
        "fills_count": len(rel),
        "fills_scanned_total": len(trs),
        "fills_pages_fetched": pages,
        "orders_count": len(order_ids),
        "delta_quote": delta_quote,
        "delta_base": delta_base,
        "mark_price": mark,
        "pnl_robot_total_quote": pnl_total,
        "quote_asset": quote_a,
        "base_asset": base_a,
    }
