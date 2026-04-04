from __future__ import annotations

import copy
import csv
import io
import json
import logging
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from .binance_public import BinancePublic
from .config_loader import load_config
from .engine import MarketMakingEngine, best_mid
from .symbols import split_pair
from .virtual_wallet import VirtualWallet

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
BT_AUDIT_DIR = PROJECT_ROOT / "auditoria" / "backtest"
BT_MAX_BARS_HARD = 100_000

# Intervalos spot comuns (Binance); validação solta na UI.
BINANCE_KLINE_INTERVALS = (
    "1m",
    "3m",
    "5m",
    "15m",
    "30m",
    "1h",
    "2h",
    "4h",
    "6h",
    "8h",
    "12h",
    "1d",
    "3d",
    "1w",
    "1M",
)


def synthetic_depth(
    mid: float,
    liquidity_quote_sum: float,
    total_spread_bps: float,
) -> dict[str, Any]:
    """
    Book mínimo para `best_mid` e filtros de liquidez.

    `total_spread_bps` é (ask−bid)/mid em bps. Valores ~2 bps com taxa de 10 bps/perna
    tornam toda ida+volta estruturalmente perdedora em `book_top`; use o auto do backtest
    ou defina `backtest.synthetic_book_spread_bps` de forma consciente.
    """
    if mid <= 0:
        return {"bids": [], "asks": []}
    half = max(liquidity_quote_sum / (2.0 * mid), 1e-8)
    h = max(total_spread_bps / 2.0 / 10000.0, 1e-12)
    bid_p = mid * (1.0 - h)
    ask_p = mid * (1.0 + h)
    return {
        "bids": [[f"{bid_p:.8f}", f"{half:.8f}"]],
        "asks": [[f"{ask_p:.8f}", f"{half:.8f}"]],
    }


def synthetic_ticker_24h(df_slice: pd.DataFrame) -> dict[str, Any]:
    qv = float((df_slice["close"] * df_slice["volume"]).sum())
    return {"quoteVolume": str(max(qv, 1.0))}


def _apply_stress_preset(bcfg: dict, root_cfg: dict, preset_name: str | None) -> None:
    if not preset_name:
        return
    presets = (root_cfg.get("backtest") or {}).get("stress_presets") or {}
    block = presets.get(preset_name)
    if not isinstance(block, dict):
        return
    skip = {"stress_presets"}
    for k, v in block.items():
        if k in skip:
            continue
        bcfg[k] = v


def flatten_bt_history_row(bt_run_id: str, row: dict[str, Any]) -> dict[str, Any]:
    ps = row.get("paper_step") or {}
    eng = row.get("engine") or {}
    w = row.get("wallet") or {}
    out: dict[str, Any] = {
        "bt_run_id": bt_run_id,
        "bar_index": row.get("i"),
        "bar_time_utc": row.get("time"),
        "filter_apt": row.get("filter_apt"),
        "mid": row.get("mid"),
        "event": row.get("event") or "",
        "cycle_end_reason": row.get("cycle_end_reason") or "",
        "engine_cycle_id": eng.get("cycle_id"),
        "engine_active": eng.get("active"),
        "paper_ok": ps.get("ok"),
        "paper_reason": ps.get("reason") or "",
        "paper_pricing": ps.get("paper_pricing") or "",
        "paper_net_quote": ps.get("net_quote"),
        "paper_gross_quote": ps.get("gross_edge_quote"),
        "paper_fees_quote": ps.get("fees_quote"),
        "patrimonio_quote": w.get("patrimonio_quote"),
        "n_operacoes": w.get("n_operacoes"),
    }
    params = row.get("params")
    if isinstance(params, dict):
        out["params_spread_bps"] = params.get("spread_bps")
        out["params_order_size_quote"] = params.get("order_size_quote")
        out["params_max_inventory_base"] = params.get("max_inventory_base")
        meta = params.get("meta") if isinstance(params.get("meta"), dict) else {}
        out["params_agent_source"] = meta.get("source") or ""
        warn = meta.get("warnings")
        out["params_warnings_json"] = json.dumps(warn, ensure_ascii=False) if warn is not None else ""
    else:
        out["params_spread_bps"] = ""
        out["params_order_size_quote"] = ""
        out["params_max_inventory_base"] = ""
        out["params_agent_source"] = ""
        out["params_warnings_json"] = ""
    return out


def build_bt_audit_csv(bt_run_id: str, history: list[dict[str, Any]]) -> str:
    rows = [flatten_bt_history_row(bt_run_id, h) for h in history]
    if not rows:
        return ""
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()), extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()


def extract_agent_decisions(bt_run_id: str, history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in history:
        if row.get("event") != "cycle_start":
            continue
        params = row.get("params")
        if not isinstance(params, dict):
            continue
        meta = dict(params.get("meta") or {}) if isinstance(params.get("meta"), dict) else {}
        raw_prop = meta.pop("raw_proposal", None)
        out.append(
            {
                "bt_run_id": bt_run_id,
                "bar_index": row.get("i"),
                "bar_time_utc": row.get("time"),
                "spread_bps": params.get("spread_bps"),
                "order_size_quote": params.get("order_size_quote"),
                "max_inventory_base": params.get("max_inventory_base"),
                "meta": meta,
                "raw_proposal": raw_prop,
            }
        )
    return out


def build_agent_decisions_jsonl(decisions: list[dict[str, Any]]) -> str:
    if not decisions:
        return ""
    lines = [json.dumps(d, ensure_ascii=False, default=str) for d in decisions]
    return "\n".join(lines) + "\n"


def compute_bt_summary(
    history: list[dict[str, Any]],
    *,
    bars_in_df: int,
    steps: int,
    motor_cycles: int,
    initial_quote: float,
    final_equity: float,
    fetch_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    apt_n = sum(1 for h in history if h.get("filter_apt"))
    paper_ok_n = sum(1 for h in history if (h.get("paper_step") or {}).get("ok"))
    paper_fail = Counter()
    for h in history:
        ps = h.get("paper_step") or {}
        if ps.get("ok") is False:
            r = ps.get("reason")
            paper_fail[str(r) if r is not None and r != "" else "(sem motivo)"] += 1
    paper_skip_n = sum(1 for h in history if (h.get("paper_step") or {}).get("ok") is False)
    net_sum = 0.0
    for h in history:
        ps = h.get("paper_step") or {}
        if ps.get("ok"):
            net_sum += float(ps.get("net_quote") or 0.0)
    cycle_starts = sum(1 for h in history if h.get("event") == "cycle_start")
    cycle_ends = sum(1 for h in history if h.get("event") == "cycle_end")
    end_reasons = Counter()
    for h in history:
        if h.get("event") == "cycle_end":
            er = h.get("cycle_end_reason")
            end_reasons[str(er if er is not None else "(sem motivo)")] += 1
    steps_with_paper = sum(1 for h in history if isinstance(h.get("paper_step"), dict))
    out: dict[str, Any] = {
        "bars_in_replay_df": bars_in_df,
        "replay_steps": steps,
        "motor_cycle_counter_final": motor_cycles,
        "steps_filter_apt": apt_n,
        "pct_steps_filter_apt": round(100.0 * apt_n / steps, 4) if steps else 0.0,
        "paper_roundtrip_ok": paper_ok_n,
        "paper_roundtrip_skipped_or_failed": paper_skip_n,
        "paper_failure_reason_counts": dict(paper_fail),
        "cycle_end_reason_counts": dict(end_reasons),
        "steps_with_paper_step": steps_with_paper,
        "sum_net_quote_on_ok_roundtrips": round(net_sum, 8),
        "cycle_start_events": cycle_starts,
        "cycle_end_events": cycle_ends,
        "initial_quote": round(initial_quote, 6),
        "final_equity_quote": round(final_equity, 6),
        "pnl_quote_vs_start": round(final_equity - initial_quote, 8),
    }
    if fetch_meta:
        out["klines_fetch"] = dict(fetch_meta)
    return out


def run_backtest(
    symbol: str,
    client: BinancePublic | None = None,
    cfg: dict[str, Any] | None = None,
    bars: int | None = None,
    step_every: int = 1,
    *,
    kline_interval: str | None = None,
    start_time_ms: int | None = None,
    end_time_ms: int | None = None,
    stress_preset: str | None = None,
    force_agent_heuristic: bool = False,
    write_audit_files: bool = True,
    filter_logger: logging.Logger | None = None,
    agent_logger: logging.Logger | None = None,
) -> dict[str, Any]:
    """
    Replay histórico (klines) com livro sintético.

    `kline_interval` — se None, usa `filters.kline_interval` do config.

    Modo **últimas N velas** (`start_time_ms` is None): `bars` velas até `end_time_ms` (ou agora);
    paginação automática se N > 1000.

    Modo **intervalo** (`start_time_ms` definido): todas as velas com open em
    [start_time_ms, end_time_ms] (fim = agora se `end_time_ms` is None), até `backtest.max_bars`.

    `force_agent_heuristic`: força `agent.provider = heuristic` só nesta corrida (sem Gemini).
    """
    cfg = copy.deepcopy(cfg or load_config())
    bcfg = cfg["backtest"]
    _apply_stress_preset(bcfg, cfg, stress_preset)

    max_soft = int(bcfg.get("max_bars", 20_000))
    default_bars = int(bcfg.get("default_bars", 500))

    if force_agent_heuristic:
        ag = dict(cfg.get("agent") or {})
        ag["provider"] = "heuristic"
        cfg["agent"] = ag

    bt_run_id = uuid.uuid4().hex[:12]

    _e = dict(cfg.get("engine") or {})
    fee_bps = float(bcfg["fee_bps"])
    cushion = float(_e.get("paper_spread_cushion_bps", 2.0))
    slip_bt = bcfg.get("slippage_bps")
    if slip_bt is not None:
        _e["paper_slippage_bps"] = float(slip_bt)
    slip_e = float(_e.get("paper_slippage_bps", 0.0))
    raw_spread = bcfg.get("synthetic_book_spread_bps")
    if raw_spread is None:
        auto = 2.0 * fee_bps + cushion + 2.0 * slip_e + 3.0
        synth_spread_bps = max(4.0, auto)
    else:
        synth_spread_bps = float(raw_spread)
    if bcfg.get("paper_skip_unprofitable_book_roundtrip") is not None:
        _e["paper_skip_unprofitable_book_roundtrip"] = bool(
            bcfg["paper_skip_unprofitable_book_roundtrip"]
        )
    cfg["engine"] = _e
    cfg["_agent_data_source"] = "backtest_synthetic_book"

    client = client or BinancePublic()
    fcfg = cfg["filters"]
    interval = (kline_interval or fcfg.get("kline_interval") or "5m").strip()
    pause_pg = float(bcfg.get("klines_pagination_pause_sec", 0.0))

    fetch_meta: dict[str, Any] = {}
    n_bars_requested: int | None = None

    if start_time_ms is not None:
        end_fetch = int(end_time_ms) if end_time_ms is not None else int(time.time() * 1000)
        if int(start_time_ms) > end_fetch:
            return {
                "ok": False,
                "error": "start_after_end",
                "detail": "início da janela > fim",
                "bt_run_id": bt_run_id,
            }
        raw_kl, fetch_meta = client.klines_fetch_range(
            symbol,
            interval,
            int(start_time_ms),
            end_fetch,
            max_rows=max_soft,
            pause_sec=pause_pg,
        )
        if fetch_meta.get("error") == "start_after_end":
            return {"ok": False, "error": "start_after_end", "bt_run_id": bt_run_id}
    else:
        n_bars = int(bars if bars is not None else default_bars)
        if n_bars < 30:
            return {
                "ok": False,
                "error": "bars_below_minimum",
                "detail": "mínimo 30 barras",
                "bt_run_id": bt_run_id,
            }
        if n_bars > BT_MAX_BARS_HARD:
            n_bars = BT_MAX_BARS_HARD
        if n_bars > max_soft:
            n_bars = max_soft
        raw_kl = client.klines_fetch_last_n(
            symbol,
            interval,
            n_bars,
            end_time_ms=end_time_ms,
            pause_sec=pause_pg,
        )
        fetch_meta = {"mode": "last_n", "bars_requested": n_bars}
        n_bars_requested = n_bars

    df = client.klines_to_df(raw_kl)
    if len(df) < 30:
        return {"ok": False, "error": "not_enough_bars", "bt_run_id": bt_run_id}

    base, quote_a = split_pair(symbol)
    initial = float(bcfg["initial_quote"])

    wallet = VirtualWallet(
        symbol=symbol.upper(),
        quote_asset=quote_a,
        base_asset=base,
        quote_balance=initial,
        base_balance=0.0,
        fee_bps=fee_bps,
    )
    eng = MarketMakingEngine(
        symbol,
        wallet,
        client=client,
        cfg=cfg,
        filter_logger=filter_logger,
        agent_logger=agent_logger,
    )

    history: list[dict[str, Any]] = []
    min_i = max(30, int(fcfg["kline_limit"]) // 2)

    min_l = float(fcfg["min_liquidity_quote_usd"])
    max_l = float(fcfg["max_liquidity_quote_usd"])
    floor = float(bcfg.get("min_synthetic_liq", 8000.0))
    step_e = max(1, int(step_every))

    for i in range(min_i, len(df), step_e):
        window = df.iloc[: i + 1].copy()
        mid = float(window["close"].iloc[-1])
        vol_quote = float(window["close"].iloc[-1] * window["volume"].iloc[-1])
        raw = max(vol_quote * 5.0, floor)
        liq_proxy = min(max(raw, min_l * 1.05), max_l * 0.95)
        depth = synthetic_depth(mid, liq_proxy, synth_spread_bps)
        t24 = synthetic_ticker_24h(window.tail(288))

        row = eng.tick_with_data(
            window,
            depth,
            t24,
            silent_filters=True,
            silent_human=True,
        )
        row["i"] = i
        row["time"] = str(window["open_time"].iloc[-1])
        history.append(row)

    last_mid = float(df["close"].iloc[-1])
    flog = filter_logger or logging.getLogger("filter")
    _win_note = ""
    if start_time_ms is not None:
        _win_note += (
            f" | start_utc={datetime.fromtimestamp(start_time_ms / 1000.0, tz=timezone.utc).isoformat()}"
        )
    if end_time_ms is not None:
        _win_note += (
            f" | end_utc={datetime.fromtimestamp(end_time_ms / 1000.0, tz=timezone.utc).isoformat()}"
        )
    elif start_time_ms is not None and len(df):
        _win_note += f" | end_utc≈{df['open_time'].iloc[-1]} (última vela do DF)"
    flog.info(
        "=== Backtest concluído: %s ===\n"
        "bt_run_id=%s | intervalo=%s | barras_df=%s | passos=%s | ciclos=%s%s\n"
        "%s\n"
        "(Nota: esta carteira é só do backtest; a carteira da UI ‘ao vivo’ é outra instância.)\n"
        "===",
        symbol.upper(),
        bt_run_id,
        interval,
        len(df),
        len(history),
        eng.state.cycle_id,
        _win_note,
        wallet.explain(last_mid),
    )

    audit_csv = build_bt_audit_csv(bt_run_id, history)
    agent_decisions = extract_agent_decisions(bt_run_id, history)
    agent_jsonl = build_agent_decisions_jsonl(agent_decisions)
    final_eq = float(wallet.equity_quote(last_mid))
    summary = compute_bt_summary(
        history,
        bars_in_df=len(df),
        steps=len(history),
        motor_cycles=int(eng.state.cycle_id),
        initial_quote=initial,
        final_equity=final_eq,
        fetch_meta=fetch_meta,
    )

    _pag = (
        "klines_fetch_range (start→end, até 1000 velas por GET)"
        if start_time_ms is not None
        else "klines_fetch_last_n (até 1000 velas por GET)"
    )
    replay_spec = {
        "bt_run_id": bt_run_id,
        "symbol": symbol.upper(),
        "kline_interval": interval,
        "fetch_mode": "range" if start_time_ms is not None else "last_n",
        "bars_requested": n_bars_requested,
        "bars_fetched": len(df),
        "step_every": step_e,
        "start_time_ms": start_time_ms,
        "start_time_utc_iso": (
            datetime.fromtimestamp(int(start_time_ms) / 1000.0, tz=timezone.utc).isoformat()
            if start_time_ms is not None
            else None
        ),
        "end_time_ms": end_time_ms,
        "end_time_utc_iso": (
            datetime.fromtimestamp(end_time_ms / 1000.0, tz=timezone.utc).isoformat()
            if end_time_ms is not None
            else None
        ),
        "stress_preset": stress_preset or "",
        "force_agent_heuristic": bool(force_agent_heuristic),
        "pagination": _pag,
        "klines_fetch_meta": fetch_meta,
    }

    artifact_paths: dict[str, str] = {}
    if write_audit_files and audit_csv:
        try:
            BT_AUDIT_DIR.mkdir(parents=True, exist_ok=True)
            ap = BT_AUDIT_DIR / f"{bt_run_id}_audit.csv"
            ap.write_text(audit_csv, encoding="utf-8")
            artifact_paths["audit_csv_path"] = str(ap)
            aj = BT_AUDIT_DIR / f"{bt_run_id}_agent.jsonl"
            aj.write_text(agent_jsonl or "", encoding="utf-8")
            artifact_paths["agent_jsonl_path"] = str(aj)
        except OSError as e:
            artifact_paths["write_error"] = str(e)

    return {
        "ok": True,
        "bt_run_id": bt_run_id,
        "symbol": symbol.upper(),
        "bars": len(df),
        "steps": len(history),
        "motor_cycle_counter": eng.state.cycle_id,
        "replay_spec": replay_spec,
        "summary": summary,
        "replay_book": {
            "synthetic_book_spread_bps": synth_spread_bps,
            "synthetic_book_spread_auto": raw_spread is None,
            "paper_slippage_bps_applied": slip_e,
            "paper_skip_unprofitable_book_roundtrip": bool(
                _e.get("paper_skip_unprofitable_book_roundtrip", True)
            ),
        },
        "closing_explanation": wallet.explain(last_mid),
        "final_wallet": wallet.summary(last_mid),
        "history_tail": history[-5:],
        "audit_csv": audit_csv,
        "agent_decisions_jsonl": agent_jsonl,
        "agent_decisions_count": len(agent_decisions),
        "artifact_paths": artifact_paths,
        "nota_determinismo": (
            "Mesmo par, intervalo de vela, janela (últimas N ou intervalo UTC fixo), presets e parâmetros produzem "
            "praticamente o mesmo resultado. Pequenas diferenças só aparecem se a API devolver klines levemente diferentes."
        ),
    }
