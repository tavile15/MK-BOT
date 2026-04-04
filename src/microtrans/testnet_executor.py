"""
Executor P4t (testnet) para traduzir decisão do motor em ordens LIMIT maker.

Escopo desta primeira versão:
- roda um ciclo (`run_executor_once`) com kill switch, allowlist e teto de notional;
- cancela ordens abertas anteriores do símbolo e publica nova cotação maker (buy/sell quando possível);
- usa MarketMakingEngine para decidir `spread_bps` e `order_size_quote`.
"""

from __future__ import annotations

import copy
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR
from pathlib import Path
from typing import Any, Iterator

from .binance_public import BinancePublic
from .binance_signed import BinanceSigned
from .config_loader import load_config
from .engine import MarketMakingEngine
from .symbols import split_pair
from .virtual_wallet import VirtualWallet


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


def _strip_trailing_zeros(s: str) -> str:
    if "." not in s:
        return s
    return s.rstrip("0").rstrip(".") or "0"


def _quantize_floor(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def _candidate_block_reason(
    *,
    qty: Decimal,
    min_qty: Decimal,
    notional: Decimal,
    min_notional: Decimal,
) -> str | None:
    if qty <= 0:
        return "zero_qty_after_balance"
    if qty < min_qty:
        return "below_min_qty"
    if notional < min_notional:
        return "below_min_notional"
    return None


@dataclass
class ExecLimits:
    max_notional_quote_per_order: float
    kill_switch_file: str
    allow_symbols: list[str]
    relax_liquidity_filter: bool


def _load_limits(cfg: dict[str, Any]) -> ExecLimits:
    ex = dict(cfg.get("execution") or {})
    allow = [str(s).upper().strip() for s in (ex.get("allow_symbols") or []) if str(s).strip()]
    return ExecLimits(
        max_notional_quote_per_order=float(ex.get("max_notional_quote_per_order", 50.0) or 50.0),
        kill_switch_file=str(ex.get("kill_switch_file") or "runtime/kill_switch_testnet.txt"),
        allow_symbols=allow,
        relax_liquidity_filter=bool(ex.get("testnet_relax_liquidity_filter", True)),
    )


def _kill_switch_armed(path_s: str) -> bool:
    p = Path(path_s)
    if not p.exists():
        return False
    txt = p.read_text(encoding="utf-8", errors="ignore").strip().lower()
    if txt == "":
        return True
    return txt in {"1", "on", "true", "armed", "stop"}


def _balances_by_asset(account: dict[str, Any]) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for b in account.get("balances") or []:
        asset = str(b.get("asset") or "").upper()
        if not asset:
            continue
        out[asset] = {
            "free": float(b.get("free") or 0.0),
            "locked": float(b.get("locked") or 0.0),
        }
    return out


def _cancel_all_open_for_symbol(signed: BinanceSigned, symbol: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for od in signed.open_orders(symbol):
        oid = od.get("orderId")
        if oid is None:
            continue
        out.append(signed.cancel_order(symbol, order_id=int(oid)))
    return out


def run_executor_once(
    *,
    symbol: str,
    pub: BinancePublic,
    signed: BinanceSigned,
    cfg: dict[str, Any] | None = None,
    force_heuristic: bool = True,
) -> dict[str, Any]:
    sym = symbol.upper().strip()
    cfg0 = copy.deepcopy(cfg or load_config())
    limits = _load_limits(cfg0)
    if limits.allow_symbols and sym not in set(limits.allow_symbols):
        return {"ok": False, "status": "blocked_allowlist", "symbol": sym, "allow_symbols": limits.allow_symbols}
    if _kill_switch_armed(limits.kill_switch_file):
        return {
            "ok": False,
            "status": "blocked_kill_switch",
            "symbol": sym,
            "kill_switch_file": limits.kill_switch_file,
        }

    if force_heuristic:
        cfg0["agent"] = dict(cfg0.get("agent") or {})
        cfg0["agent"]["provider"] = "heuristic"
    if limits.relax_liquidity_filter:
        cfg0["filters"] = dict(cfg0.get("filters") or {})
        cfg0["filters"]["relax_liquidity_filter"] = True

    info = pub.exchange_info(sym)
    se = _symbol_entry(info, sym)
    if not se:
        return {"ok": False, "status": "symbol_not_found", "symbol": sym}
    ft = _filters_by_type(se)
    lot = ft.get("LOT_SIZE") or {}
    pf = ft.get("PRICE_FILTER") or {}
    notion_f = ft.get("MIN_NOTIONAL") or ft.get("NOTIONAL") or {}
    step_size = Decimal(str(lot.get("stepSize", "0.00001")))
    tick_size = Decimal(str(pf.get("tickSize", "0.01")))
    min_qty = Decimal(str(lot.get("minQty", "0")))
    min_price = Decimal(str(pf.get("minPrice", "0")))
    min_notional = Decimal(str(notion_f.get("minNotional", notion_f.get("notional", "5"))))

    acc = signed.account()
    bals = _balances_by_asset(acc)
    base_asset, quote_asset = split_pair(sym)
    free_quote = float((bals.get(quote_asset) or {}).get("free") or 0.0)
    free_base = float((bals.get(base_asset) or {}).get("free") or 0.0)

    vw = VirtualWallet(
        symbol=sym,
        quote_asset=quote_asset,
        base_asset=base_asset,
        quote_balance=max(free_quote, 0.0),
        base_balance=max(free_base, 0.0),
        fee_bps=float((cfg0.get("backtest") or {}).get("fee_bps", 10)),
    )
    eng = MarketMakingEngine(sym, vw, client=pub, cfg=cfg0)
    out = eng.tick(silent_logs=True)
    params = ((out.get("engine") or {}).get("params") or {})
    agent_source = str(((params.get("meta") or {}).get("source") or "")).strip()
    if not out.get("filter_apt") or not params:
        cancelled = _cancel_all_open_for_symbol(signed, sym)
        return {
            "ok": True,
            "status": "no_quote_filter_or_params",
            "symbol": sym,
            "filter_apt": bool(out.get("filter_apt")),
            "filter_human": out.get("filter_human"),
            "agent_source": agent_source or None,
            "params_present": bool(params),
            "cancelled_count": len(cancelled),
            "event": out.get("event"),
        }

    mid = float(out.get("mid") or 0.0)
    bb = float(out.get("best_bid") or 0.0)
    ba = float(out.get("best_ask") or 0.0)
    if mid <= 0 or bb <= 0 or ba <= 0:
        return {"ok": False, "status": "bad_market_snapshot", "symbol": sym}

    spread_bps = float(params.get("spread_bps") or 0.0)
    order_q = float(params.get("order_size_quote") or 0.0)
    use_q = max(0.0, min(order_q, limits.max_notional_quote_per_order))

    buy_px_d = Decimal(str(max(min_price, bb * (1.0 - spread_bps / 20000.0))))
    sell_px_d = Decimal(str(max(min_price, ba * (1.0 + spread_bps / 20000.0))))
    buy_px_d = _quantize_floor(buy_px_d, tick_size)
    sell_px_d = _quantize_floor(sell_px_d, tick_size)
    if buy_px_d <= 0 or sell_px_d <= 0:
        return {"ok": False, "status": "bad_price_after_quantization", "symbol": sym}

    buy_qty_d = _quantize_floor(Decimal(str(use_q)) / buy_px_d, step_size)
    sell_qty_d = _quantize_floor(Decimal(str(use_q)) / sell_px_d, step_size)
    if buy_qty_d < min_qty:
        buy_qty_d = min_qty
    if sell_qty_d < min_qty:
        sell_qty_d = min_qty

    # Respeita saldo disponível.
    max_buy_qty_by_balance = _quantize_floor(Decimal(str(max(free_quote, 0.0))) / buy_px_d, step_size)
    if buy_qty_d > max_buy_qty_by_balance:
        buy_qty_d = max_buy_qty_by_balance
    max_sell_qty_by_balance = _quantize_floor(Decimal(str(max(free_base, 0.0))), step_size)
    if sell_qty_d > max_sell_qty_by_balance:
        sell_qty_d = max_sell_qty_by_balance

    buy_notional = buy_qty_d * buy_px_d
    sell_notional = sell_qty_d * sell_px_d
    can_buy = buy_qty_d >= min_qty and buy_notional >= min_notional
    can_sell = sell_qty_d >= min_qty and sell_notional >= min_notional
    buy_block_reason = _candidate_block_reason(
        qty=buy_qty_d,
        min_qty=min_qty,
        notional=buy_notional,
        min_notional=min_notional,
    )
    sell_block_reason = _candidate_block_reason(
        qty=sell_qty_d,
        min_qty=min_qty,
        notional=sell_notional,
        min_notional=min_notional,
    )

    cancelled = _cancel_all_open_for_symbol(signed, sym)
    placed: list[dict[str, Any]] = []

    if can_buy:
        cid_b = f"mtb{uuid.uuid4().hex[:10]}"
        placed.append(
            signed.new_order(
                sym,
                "BUY",
                "LIMIT",
                time_in_force="GTC",
                quantity=_strip_trailing_zeros(format(buy_qty_d, "f")),
                price=_strip_trailing_zeros(format(buy_px_d, "f")),
                new_client_order_id=cid_b,
            )
        )
    if can_sell:
        cid_s = f"mts{uuid.uuid4().hex[:10]}"
        placed.append(
            signed.new_order(
                sym,
                "SELL",
                "LIMIT",
                time_in_force="GTC",
                quantity=_strip_trailing_zeros(format(sell_qty_d, "f")),
                price=_strip_trailing_zeros(format(sell_px_d, "f")),
                new_client_order_id=cid_s,
            )
        )

    status = "quoted_placed" if placed else "quoted_blocked_min_notional_balance"
    return {
        "ok": True,
        "status": status,
        "symbol": sym,
        "event": out.get("event"),
        "filter_apt": bool(out.get("filter_apt")),
        "spread_bps": spread_bps,
        "agent_source": agent_source or None,
        "order_size_quote_requested": order_q,
        "order_size_quote_capped": use_q,
        "limits": {
            "max_notional_quote_per_order": limits.max_notional_quote_per_order,
            "kill_switch_file": limits.kill_switch_file,
            "allow_symbols": limits.allow_symbols,
            "relax_liquidity_filter": limits.relax_liquidity_filter,
        },
        "balances_free": {quote_asset: free_quote, base_asset: free_base},
        "cancelled_count": len(cancelled),
        "placed_count": len(placed),
        "placed": placed,
        "candidate": {
            "buy": {
                "price": _strip_trailing_zeros(format(buy_px_d, "f")),
                "qty": _strip_trailing_zeros(format(buy_qty_d, "f")),
                "notional_quote": float(buy_notional),
                "can_place": bool(can_buy),
                "block_reason": buy_block_reason,
            },
            "sell": {
                "price": _strip_trailing_zeros(format(sell_px_d, "f")),
                "qty": _strip_trailing_zeros(format(sell_qty_d, "f")),
                "notional_quote": float(sell_notional),
                "can_place": bool(can_sell),
                "block_reason": sell_block_reason,
            },
        },
    }


def run_executor_loop(
    *,
    symbol: str,
    pub: BinancePublic,
    signed: BinanceSigned,
    interval_sec: float = 15.0,
    max_cycles: int = 0,
    cfg: dict[str, Any] | None = None,
    force_heuristic: bool = True,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for step in run_executor_iter(
        symbol=symbol,
        pub=pub,
        signed=signed,
        interval_sec=interval_sec,
        max_cycles=max_cycles,
        cfg=cfg,
        force_heuristic=force_heuristic,
    ):
        out.append(step)
    return out


def run_executor_iter(
    *,
    symbol: str,
    pub: BinancePublic,
    signed: BinanceSigned,
    interval_sec: float = 15.0,
    max_cycles: int = 0,
    cfg: dict[str, Any] | None = None,
    force_heuristic: bool = True,
) -> Iterator[dict[str, Any]]:
    n = 0
    while True:
        n += 1
        try:
            step = run_executor_once(
                symbol=symbol,
                pub=pub,
                signed=signed,
                cfg=cfg,
                force_heuristic=force_heuristic,
            )
        except Exception as e:
            # Soak 24/7: timeout/erro transitório não deve derrubar o loop.
            step = {
                "ok": False,
                "status": "error_exception",
                "symbol": symbol.upper().strip(),
                "error": str(e),
                "error_type": type(e).__name__,
            }
        step["loop_cycle"] = n
        yield step
        if max_cycles > 0 and n >= max_cycles:
            break
        if step.get("status") == "blocked_kill_switch":
            break
        time.sleep(max(1.0, float(interval_sec)))

