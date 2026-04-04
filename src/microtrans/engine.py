from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from typing import Any

from .agent_stub import generate_strategy_once
from .binance_public import BinancePublic
from .config_loader import load_config
from .filters import FilterResult, diagnostic_table, evaluate_market, format_filter_human
from .strategy_params import StrategyParams
from .virtual_wallet import VirtualWallet


@dataclass
class EngineState:
    symbol: str
    active: bool = False
    params: StrategyParams | None = None
    cycle_id: int = 0
    equity_start: float | None = None
    last_filter: FilterResult | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def _paper_fill_roll(probability: float, nonce: int) -> bool:
    """Sorteio estável por nonce (mesma sequência de ticks → mesmos fills)."""
    if probability >= 1.0:
        return True
    if probability <= 0.0:
        return False
    x = ((nonce * 1664525 + 1013904223) & 0xFFFFFFFF) / float(0x100000000)
    return x < probability


def fetch_snapshot(client: BinancePublic, symbol: str, cfg: dict[str, Any]):
    fcfg = cfg["filters"]
    kl = client.klines(
        symbol,
        fcfg["kline_interval"],
        limit=int(fcfg["kline_limit"]),
    )
    df = client.klines_to_df(kl)
    depth = client.depth(symbol, limit=int(fcfg["depth_limit"]))
    try:
        t24 = client.ticker_24h(symbol)
    except Exception:
        t24 = None
    return df, depth, t24


def preview_book_top_roundtrip(
    wallet: VirtualWallet,
    depth: dict[str, Any],
    order_size_quote: float,
    eng_cfg: dict[str, Any],
) -> dict[str, Any]:
    """
    Estima economia de uma ida/volta no topo do livro (taxas + spread), sem mutar carteira.
    Usado no snapshot do Gemini para alinhar spread/tamanho à realidade de book_top.
    """
    bids = (depth or {}).get("bids") or []
    asks = (depth or {}).get("asks") or []
    if not bids or not asks:
        return {"ok": False, "reason": "empty_book"}
    raw_bid = float(bids[0][0])
    raw_ask = float(asks[0][0])
    if raw_bid <= 0 or raw_ask <= 0 or raw_ask <= raw_bid:
        return {"ok": False, "reason": "bad_book"}
    slip = float(eng_cfg.get("paper_slippage_bps", 0.0)) / 10000.0
    denom = raw_ask + raw_bid
    spread_frac = (raw_ask - raw_bid) / denom if denom > 0 else 0.0
    slip_cap = max(0.0, 0.5 * spread_frac - 1e-12)
    slip_use = min(slip, slip_cap)
    bid = raw_bid * (1.0 + slip_use)
    ask = raw_ask * (1.0 - slip_use)
    if ask <= bid:
        bid, ask = raw_bid, raw_ask
    midBOOK = 0.5 * (raw_bid + raw_ask)
    book_spread_bps = (raw_ask - raw_bid) / midBOOK * 10000.0 if midBOOK > 0 else 0.0
    half = max(order_size_quote, 1e-12) / 2.0
    fee_r = float(wallet.fee_bps) / 10000.0
    base_hat = half / bid
    gross_hat = (ask - bid) * base_hat
    fee_buy_hat = half * fee_r
    fee_sell_hat = (base_hat * ask) * fee_r
    net_hat = gross_hat - fee_buy_hat - fee_sell_hat
    skip = bool(eng_cfg.get("paper_skip_unprofitable_book_roundtrip", True))
    out: dict[str, Any] = {
        "ok": True,
        "book_spread_bps": round(book_spread_bps, 4),
        "bid_exec": round(bid, 8),
        "ask_exec": round(ask, 8),
        "gross_edge_quote_est": round(gross_hat, 8),
        "fees_quote_est": round(fee_buy_hat + fee_sell_hat, 8),
        "net_edge_quote_est": round(net_hat, 8),
        "roundtrip_profitable_estimate": net_hat >= 0.0,
        "would_skip_roundtrip": skip and net_hat < 0.0,
    }
    if slip_use != slip:
        out["slippage_capped_to_book"] = True
    return out


def paper_spread_roundtrip(
    wallet: VirtualWallet,
    mid: float,
    spread_bps: float,
    order_size_quote: float,
    *,
    depth: dict[str, Any] | None = None,
    eng_cfg: dict[str, Any] | None = None,
    fill_nonce: int = 0,
) -> dict[str, Any]:
    """
    Simula uma ida e volta: compra mais barato no book sintético, vende mais caro (alinhado em quantidade).

    Modos (`engine.paper_pricing`):
    - synthetic_mid: bid/ask em torno do mid com spread_bps (legado, otimista vs book real).
    - book_top: executa no melhor bid / melhor ask observados (+ paper_slippage_bps contra você).
    """
    eng_cfg = eng_cfg or {}
    mode_raw = (eng_cfg.get("paper_pricing") or "book_top").strip().lower()
    if mode_raw in ("book", "book_top", "touch"):
        mode = "book_top"
    else:
        mode = "synthetic_mid"

    p_fill = float(eng_cfg.get("paper_fill_probability", 1.0))
    if not _paper_fill_roll(p_fill, fill_nonce):
        return {
            "ok": False,
            "reason": "paper_no_fill",
            "paper_pricing": mode,
            "paper_fill_probability": p_fill,
        }

    slip = float(eng_cfg.get("paper_slippage_bps", 0.0)) / 10000.0
    half = order_size_quote / 2.0
    slip_note: str | None = None

    if mode == "book_top":
        bids = (depth or {}).get("bids") or []
        asks = (depth or {}).get("asks") or []
        if not bids or not asks:
            return {"ok": False, "reason": "empty_book", "paper_pricing": mode}
        raw_bid = float(bids[0][0])
        raw_ask = float(asks[0][0])
        if raw_bid <= 0 or raw_ask <= 0 or raw_ask <= raw_bid:
            return {"ok": False, "reason": "bad_book", "paper_pricing": mode}
        # Slippage simétrica não pode exceder ~metade do spread relativo senão ask_eff <= bid_eff (book apertado).
        denom = raw_ask + raw_bid
        spread_frac = (raw_ask - raw_bid) / denom if denom > 0 else 0.0
        slip_cap = max(0.0, 0.5 * spread_frac - 1e-12)
        slip_use = min(slip, slip_cap)
        if slip > slip_cap + 1e-18:
            slip_note = f"slippage_pedido_bps={slip * 10000:.4g} aplicado_bps={slip_use * 10000:.4g} (teto pelo book)"
        bid = raw_bid * (1.0 + slip_use)
        ask = raw_ask * (1.0 - slip_use)
        if ask <= bid:
            bid, ask = raw_bid, raw_ask
            slip_note = (slip_note + "; " if slip_note else "") + "fallback_topo_livro_sem_slippage"
        mid_ref = 0.5 * (bid + ask)
    else:
        if mid <= 0:
            return {"ok": False, "reason": "bad_mid", "paper_pricing": mode}
        bid = mid * (1.0 - spread_bps / 10000.0)
        ask = mid * (1.0 + spread_bps / 10000.0)
        bid = bid * (1.0 + slip)
        ask = ask * (1.0 - slip)
        if bid <= 0 or ask <= 0 or ask <= bid:
            return {"ok": False, "reason": "spread_collapsed_after_slip", "paper_pricing": mode}
        mid_ref = mid

    # Em book_top o spread observado costuma ser << taxas; voltas “compra+venda” viram só pagamento de fee.
    if mode == "book_top" and eng_cfg.get("paper_skip_unprofitable_book_roundtrip", True):
        fee_r = float(wallet.fee_bps) / 10000.0
        base_hat = half / bid
        gross_hat = (ask - bid) * base_hat
        fee_buy_hat = half * fee_r
        fee_sell_hat = (base_hat * ask) * fee_r
        net_hat = gross_hat - fee_buy_hat - fee_sell_hat
        if net_hat < 0.0:
            out_skip: dict[str, Any] = {
                "ok": False,
                "reason": "book_top_spread_smaller_than_fees",
                "paper_pricing": mode,
                "gross_edge_quote": gross_hat,
                "estimated_net_quote": round(net_hat, 8),
            }
            if slip_note:
                out_skip["slip_note"] = slip_note
            return out_skip

    tr_buy = wallet.buy(bid, half, tag="mm_bid")
    if tr_buy is None:
        return {"ok": False, "reason": "buy_failed", "paper_pricing": mode}
    base = tr_buy.base_qty
    tr_sell = wallet.sell(ask, base, tag="mm_ask")
    if tr_sell is None:
        return {"ok": False, "reason": "sell_failed", "paper_pricing": mode}
    gross = (ask - bid) * base
    fees = tr_buy.fee_quote + tr_sell.fee_quote
    out_ok: dict[str, Any] = {
        "ok": True,
        "mid": mid_ref,
        "bid": bid,
        "ask": ask,
        "base": base,
        "gross_edge_quote": gross,
        "fees_quote": fees,
        "net_quote": gross - fees,
        "paper_pricing": mode,
    }
    if mode == "book_top" and slip_note:
        out_ok["slip_note"] = slip_note
    return out_ok


def best_mid(depth: dict[str, Any]) -> float:
    bids = depth.get("bids", [])
    asks = depth.get("asks", [])
    if not bids or not asks:
        return 0.0
    return (float(bids[0][0]) + float(asks[0][0])) / 2.0


def book_preview(depth: dict[str, Any], n: int = 14) -> dict[str, Any]:
    bids = depth.get("bids", [])[:n]
    asks = depth.get("asks", [])[:n]
    return {
        "bids": [[float(p), float(q)] for p, q in bids],
        "asks": [[float(p), float(q)] for p, q in asks],
    }


def market_24h_bar(t24: dict[str, Any] | None) -> dict[str, Any]:
    if not t24:
        return {}
    try:
        return {
            "ultimo": float(t24["lastPrice"]),
            "variacao_pct_24h": float(t24["priceChangePercent"]),
            "high_24h": float(t24["highPrice"]),
            "low_24h": float(t24["lowPrice"]),
            "volume_base_24h": float(t24["volume"]),
            "volume_quote_24h": float(t24["quoteVolume"]),
        }
    except (KeyError, TypeError, ValueError):
        return {}


def market_snapshot(depth: dict[str, Any], t24: dict[str, Any] | None, mid: float) -> dict[str, Any]:
    snap: dict[str, Any] = {
        "book_preview": book_preview(depth),
        "market_24h": market_24h_bar(t24),
    }
    bids = depth.get("bids", [])
    asks = depth.get("asks", [])
    if mid > 0 and bids and asks:
        bid = float(bids[0][0])
        ask = float(asks[0][0])
        snap["best_bid"] = bid
        snap["best_ask"] = ask
        snap["spread_bps"] = (ask - bid) / mid * 10000.0
    return snap


def build_agent_execution_context(
    *,
    symbol: str,
    wallet: VirtualWallet,
    depth: dict[str, Any],
    mid: float,
    t24: dict[str, Any] | None,
    eng_cfg: dict[str, Any],
    agent_cfg: dict[str, Any],
    fr_metrics: dict[str, float],
    data_source: str = "live",
) -> dict[str, Any]:
    """Observáveis passados ao LLM (Gemini): preço, livro, taxas, modo de precificação, limites."""
    mode_raw = (eng_cfg.get("paper_pricing") or "book_top").strip().lower()
    pricing = "book_top" if mode_raw in ("book", "book_top", "touch") else "synthetic_mid"
    fee_bps = float(wallet.fee_bps)
    cushion = float(eng_cfg.get("paper_spread_cushion_bps", 2.0))
    enforce_floor = bool(eng_cfg.get("enforce_min_spread_for_fees", True))
    min_spread_engine_floor_bps = (2.0 * fee_bps + cushion) if enforce_floor else 0.0
    gemini_floor = float(agent_cfg.get("gemini_min_spread_bps", 15.0))

    m24 = market_24h_bar(t24)
    probe_order_q = float(agent_cfg.get("default_order_size_quote", 50.0))
    probe_order_q = min(max(probe_order_q, 1e-9), wallet.quote_balance * 0.5)

    bids = depth.get("bids") or []
    asks = depth.get("asks") or []
    bb = float(bids[0][0]) if bids else 0.0
    ba = float(asks[0][0]) if asks else 0.0
    spread_bps_touch = (ba - bb) / mid * 10000.0 if mid > 0 and ba > bb else 0.0

    preview = preview_book_top_roundtrip(wallet, depth, probe_order_q, eng_cfg)

    last_px = float(fr_metrics.get("last_price", mid))
    atr_pct = float(fr_metrics.get("atr_pct", 0.0))

    ctx: dict[str, Any] = {
        "data_source": data_source,
        "symbol": symbol,
        "pricing_mode": pricing,
        "pricing_mode_help": (
            "book_top: compra no bid e vende no ask observados; o parâmetro spread_bps NÃO amplia esses preços — "
            "define referência para piso do motor e modo synthetic_mid. "
            "synthetic_mid: bid/ask são derivados do mid ± spread_bps (útil para stress sem livro real)."
        ),
        "fee_bps_per_leg": fee_bps,
        "fee_two_legs_naive_bps": round(2.0 * fee_bps, 4),
        "min_spread_engine_floor_bps": round(min_spread_engine_floor_bps, 4),
        "gemini_spread_floor_bps": gemini_floor,
        "paper_skip_unprofitable_book_roundtrip": bool(
            eng_cfg.get("paper_skip_unprofitable_book_roundtrip", True)
        ),
        "paper_slippage_bps": float(eng_cfg.get("paper_slippage_bps", 0.0)),
        "paper_fill_probability": float(eng_cfg.get("paper_fill_probability", 1.0)),
        "mid": round(mid, 8),
        "last_price_filters": round(last_px, 8),
        "best_bid": round(bb, 8),
        "best_ask": round(ba, 8),
        "touch_spread_bps": round(spread_bps_touch, 4),
        "quote_balance": round(wallet.quote_balance, 6),
        "base_balance": round(wallet.base_balance, 8),
        "equity_quote_at_mid": round(wallet.equity_quote(mid), 6),
        "probe_order_size_quote_for_preview": round(probe_order_q, 6),
        "book_top_roundtrip_preview": preview,
        "market_24h": m24,
        "atr_pct": round(atr_pct, 6),
    }
    return ctx


def apply_paper_spread_floor(
    params: StrategyParams,
    wallet: VirtualWallet,
    eng_cfg: dict[str, Any],
) -> StrategyParams:
    """
    Garante spread mínimo em bps para que a ida/volta em papel não seja só pagamento de taxas.
    Heurística: spread >= 2 * fee_bps + cushion (compra e venda cobram em quote).
    """
    if not eng_cfg.get("enforce_min_spread_for_fees", True):
        return params
    fee = float(wallet.fee_bps)
    cushion = float(eng_cfg.get("paper_spread_cushion_bps", 2.0))
    floor = 2.0 * fee + cushion
    if params.spread_bps >= floor:
        return params
    meta = dict(params.meta)
    meta["spread_floor_bps"] = floor
    meta["spread_before_floor"] = params.spread_bps
    return replace(params, spread_bps=round(floor, 4), meta=meta)


class MarketMakingEngine:
    """Orquestra filtros → agente (1x) → passos de papel até filtros caírem."""

    def __init__(
        self,
        symbol: str,
        wallet: VirtualWallet,
        client: BinancePublic | None = None,
        cfg: dict[str, Any] | None = None,
        filter_logger: logging.Logger | None = None,
        agent_logger: logging.Logger | None = None,
    ):
        self.symbol = symbol.upper()
        self.wallet = wallet
        self.client = client or BinancePublic()
        self.cfg = cfg or load_config()
        self.filter_logger = filter_logger or logging.getLogger("filter")
        self.agent_logger = agent_logger or logging.getLogger("agent")
        self.state = EngineState(symbol=self.symbol)

    def tick(self, silent_logs: bool = False) -> dict[str, Any]:
        df, depth, t24 = fetch_snapshot(self.client, self.symbol, self.cfg)
        return self.tick_with_data(
            df,
            depth,
            t24,
            silent_filters=silent_logs,
            silent_human=silent_logs,
        )

    def tick_with_data(
        self,
        df,
        depth: dict[str, Any],
        t24: dict[str, Any] | None,
        silent_filters: bool = False,
        silent_human: bool = False,
    ) -> dict[str, Any]:
        fr = evaluate_market(
            self.symbol,
            df,
            depth,
            t24,
            self.cfg,
            self.filter_logger,
            silent=silent_filters,
        )
        self.state.last_filter = fr
        # Mercado voltou a NÃO APTO com motor parado: libera orçamento para próxima janela Gemini.
        if not fr.apt and not self.state.active:
            self.state.extra.pop("gemini_budget_exhausted", None)
        mid = best_mid(depth)
        out: dict[str, Any] = {
            "symbol": self.symbol,
            "filter_apt": fr.apt,
            "mid": mid,
            "metrics": fr.metrics,
            "reasons": fr.reasons,
            "filter_diagnostic": diagnostic_table(fr, self.cfg["filters"]),
            "filter_human": format_filter_human(
                self.symbol,
                fr.apt,
                fr.metrics,
                fr.reasons,
                self.cfg["filters"],
            ),
        }
        out.update(market_snapshot(depth, t24, mid))

        eng = self.cfg["engine"]
        if self.state.active and self.state.params:
            eq = self.wallet.equity_quote(mid)
            start = self.state.equity_start
            if start and start > 0:
                pnl_c = eq - start
                tp_pct = float(eng.get("exit_take_profit_pct", 0.0) or 0.0)
                tp_q = float(eng.get("exit_take_profit_quote", 0.0) or 0.0)
                if tp_pct > 0 and pnl_c >= start * (tp_pct / 100.0):
                    self._stop_cycle("take_profit_pct", mid, out)
                    return out
                if tp_q > 0 and pnl_c >= tp_q:
                    self._stop_cycle("take_profit_quote", mid, out)
                    return out
            dd_limit = float(eng.get("exit_drawdown_pct", 3.0))
            if self.state.equity_start and eq < self.state.equity_start * (1.0 - dd_limit / 100.0):
                self._stop_cycle("drawdown", mid, out)
                return out

        if fr.apt and not self.state.active:
            ex_ctx = build_agent_execution_context(
                symbol=self.symbol,
                wallet=self.wallet,
                depth=depth,
                mid=mid,
                t24=t24,
                eng_cfg=eng,
                agent_cfg=self.cfg.get("agent") or {},
                fr_metrics=fr.metrics,
                data_source=str(self.cfg.get("_agent_data_source", "live")),
            )
            params = generate_strategy_once(
                self.symbol,
                fr.metrics,
                self.cfg,
                self.agent_logger,
                silent=silent_human,
                filter_apt=fr.apt,
                reasons=fr.reasons,
                state_extra=self.state.extra,
                execution_context=ex_ctx,
            )
            params = apply_paper_spread_floor(params, self.wallet, eng)
            self.state.params = params
            self.state.active = True
            self.state.cycle_id += 1
            self.state.extra["paper_ticks"] = 0
            self.state.extra.pop("gemini_budget_exhausted", None)
            self.state.equity_start = self.wallet.equity_quote(mid)
            out["event"] = "cycle_start"
            out["params"] = params.to_dict()
            if not silent_human:
                self.agent_logger.info(
                    "Ciclo %s iniciado | patrimonio inicial do ciclo (quote): %.4f",
                    self.state.cycle_id,
                    self.state.equity_start or 0.0,
                )

        elif not fr.apt and self.state.active and eng.get("require_filters_for_continue", True):
            self._stop_cycle("filters_failed", mid, out)
            return out

        if self.state.active and self.state.params and mid > 0:
            self.state.extra["paper_ticks"] = int(self.state.extra.get("paper_ticks", 0)) + 1
            nonce = int(self.state.cycle_id) * 1_000_000 + int(self.state.extra["paper_ticks"])
            step = paper_spread_roundtrip(
                self.wallet,
                mid,
                self.state.params.spread_bps,
                min(self.state.params.order_size_quote, self.wallet.quote_balance * 0.5),
                depth=depth,
                eng_cfg=eng,
                fill_nonce=nonce,
            )
            out["paper_step"] = step
            if not silent_human and step.get("ok"):
                px = step.get("paper_pricing", "synthetic_mid")
                self.agent_logger.info(
                    "Papel (%s): compra %.4f, venda %.4f | "
                    "margem bruta %.6f quote, taxas %.6f, liquido %.6f quote",
                    px,
                    float(step.get("bid", 0)),
                    float(step.get("ask", 0)),
                    float(step.get("gross_edge_quote", 0)),
                    float(step.get("fees_quote", 0)),
                    float(step.get("net_quote", 0)),
                )

        out["wallet"] = self.wallet.summary(mid)
        out["engine"] = {
            "active": self.state.active,
            "cycle_id": self.state.cycle_id,
            "params": self.state.params.to_dict() if self.state.params else None,
        }
        return out

    def _stop_cycle(self, reason: str, mid: float, out: dict[str, Any]) -> None:
        eq = self.wallet.equity_quote(mid)
        start = self.state.equity_start
        pnl = (eq - start) if start else 0.0
        self.filter_logger.info(
            "Fim do ciclo %s | motivo: %s | PnL do ciclo (quote): %+.4f | patrimonio atual (quote): %.4f",
            self.state.cycle_id,
            reason,
            pnl,
            eq,
        )
        self.state.active = False
        self.state.params = None
        self.state.equity_start = None
        self.state.extra.pop("gemini_budget_exhausted", None)
        out["event"] = "cycle_end"
        out["cycle_end_reason"] = reason
        out["wallet"] = self.wallet.summary(mid)
        out["engine"] = {
            "active": self.state.active,
            "cycle_id": self.state.cycle_id,
            "params": None,
        }
