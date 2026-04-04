from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from microtrans.binance_public import BinancePublic
from microtrans.binance_signed import BinanceSigned
from microtrans.config_loader import load_config
from microtrans.symbols import split_pair
from microtrans.testnet_executor import run_executor_iter
from microtrans.testnet_soak_report import summarize_executor_jsonl


def _parse_structured_logs(lines: list[str]) -> pd.DataFrame:
    rows: list[dict[str, str]] = []
    for ln in lines:
        parts = [p.strip() for p in str(ln).split("|", 3)]
        if len(parts) == 4:
            rows.append(
                {
                    "nivel": parts[0],
                    "contexto": parts[1],
                    "event_id": parts[2],
                    "mensagem": parts[3],
                }
            )
    return pd.DataFrame(rows)


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _read_jsonl_tail(path: Path, limit: int = 30) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            lines = f.readlines()
        for ln in lines[-limit:]:
            try:
                out.append(json.loads(ln))
            except Exception:
                continue
    except Exception:
        return []
    return out


def _extract_order_ids_from_executor_jsonl(path: Path) -> set[int]:
    ids: set[int] = set()
    if not path.exists():
        return ids
    try:
        with path.open("r", encoding="utf-8") as f:
            for ln in f:
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


def _fetch_my_trades_paginated(
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


def _compute_robot_pnl_from_real_fills(
    *,
    st: Any,
    symbol: str,
    jsonl_path: Path,
) -> dict[str, Any]:
    cfg = load_config()
    ex = dict(cfg.get("execution") or {})
    base_url = str(ex.get("testnet_base_url") or "https://testnet.binance.vision")
    pub = BinancePublic(base_url=base_url)
    signed = BinanceSigned.from_env(require_testnet=True)
    sym = str(symbol or "BTCUSDT").strip().upper()
    base_a, quote_a = split_pair(sym)
    order_ids = _extract_order_ids_from_executor_jsonl(jsonl_path)
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
        }
    trs, pages = _fetch_my_trades_paginated(signed=signed, symbol=sym, limit=1000)
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


def _maybe_refresh_robot_pnl(
    st: Any,
    *,
    symbol: str,
    jsonl_path: Path,
    min_sec: float = 12.0,
) -> dict[str, Any]:
    now = time.time()
    cache_key = "_tn_robot_pnl_cache"
    last_ts = float(st.session_state.get("_tn_robot_pnl_last_ts") or 0.0)
    same_src = st.session_state.get("_tn_robot_pnl_src") == (str(symbol).upper(), str(jsonl_path))
    if same_src and (now - last_ts) < max(5.0, float(min_sec)):
        return dict(st.session_state.get(cache_key) or {})
    try:
        rep = _compute_robot_pnl_from_real_fills(st=st, symbol=symbol, jsonl_path=jsonl_path)
        st.session_state[cache_key] = rep
        st.session_state["_tn_robot_pnl_last_ts"] = now
        st.session_state["_tn_robot_pnl_src"] = (str(symbol).upper(), str(jsonl_path))
        st.session_state.pop("_tn_robot_pnl_error", None)
        return rep
    except Exception as e:
        st.session_state["_tn_robot_pnl_error"] = str(e)
        return dict(st.session_state.get(cache_key) or {})


def _capture_testnet_equity_snapshot_ui(st: Any, *, symbol: str) -> dict[str, Any]:
    cfg = load_config()
    ex = dict(cfg.get("execution") or {})
    base_url = str(ex.get("testnet_base_url") or "https://testnet.binance.vision")
    pub = BinancePublic(base_url=base_url)
    signed = BinanceSigned.from_env(require_testnet=True)
    acc = signed.account()
    bals = acc.get("balances") or []
    sym = str(symbol or "BTCUSDT").strip().upper()
    base_a, quote_a = split_pair(sym)
    by_asset: dict[str, tuple[float, float]] = {}
    for b in bals:
        a = str(b.get("asset") or "").upper()
        if not a:
            continue
        by_asset[a] = (float(b.get("free") or 0.0), float(b.get("locked") or 0.0))
    qf, ql = by_asset.get(quote_a, (0.0, 0.0))
    bf, bl = by_asset.get(base_a, (0.0, 0.0))
    q_total = qf + ql
    b_total = bf + bl
    tk = pub.ticker_24h(sym)
    mark = float(tk.get("lastPrice") or 0.0)
    eq = q_total + (b_total * mark if mark > 0 else 0.0)
    bkey = f"{sym}_eq0"
    base_map = st.session_state.setdefault("testnet_equity_baseline_by_symbol", {})
    if bkey not in base_map:
        base_map[bkey] = eq
    pnl = eq - float(base_map.get(bkey) or eq)
    row = {
        "hora_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "symbol": sym,
        "quote_asset": quote_a,
        "base_asset": base_a,
        "quote_total": q_total,
        "base_total": b_total,
        "mark_price": mark,
        "equity_quote": eq,
        "pnl_vs_start": pnl,
    }
    st.session_state["testnet_equity_rows"] = (st.session_state.get("testnet_equity_rows") or []) + [row]
    st.session_state["testnet_equity_rows"] = st.session_state["testnet_equity_rows"][-1500:]
    return row


def _maybe_capture_testnet_equity_snapshot_ui(st: Any, *, symbol: str, min_sec: float) -> None:
    now = time.time()
    last_ts = float(st.session_state.get("_tn_eq_last_poll_ts") or 0.0)
    if now - last_ts < max(5.0, float(min_sec)):
        return
    try:
        _capture_testnet_equity_snapshot_ui(st, symbol=symbol)
        st.session_state["_tn_eq_last_poll_ts"] = now
        st.session_state.pop("_tn_eq_last_poll_error", None)
    except Exception as e:
        st.session_state["_tn_eq_last_poll_error"] = str(e)


def _home_brand_image_path() -> Path | None:
    root = Path(__file__).resolve().parents[3]
    candidates = [
        root / "indentidade_visual" / "Gemini_Generated_Image_r9hxdwr9hxdwr9hx.png",
        root / "indentidade_visual" / "Gemini_Generated_Image_ntdn6xntdn6xntdn.png",
        root / "indentidade_visual" / "WhatsApp Image 2026-04-02 at 17.36.26.jpeg",
    ]
    for p in candidates:
        if p.is_file():
            return p
    return None


def _home_market_watch(st: Any, min_sec: float = 30.0) -> dict[str, dict[str, float]]:
    now = time.time()
    last_ts = float(st.session_state.get("_home_watch_last_ts") or 0.0)
    cached = st.session_state.get("_home_watch_cache")
    if isinstance(cached, dict) and (now - last_ts) < max(8.0, float(min_sec)):
        return dict(cached)
    out: dict[str, dict[str, float]] = {}
    try:
        pub = BinancePublic()
        for sym in ("BTCUSDT", "SOLUSDT"):
            tk = pub.ticker_24h(sym)
            out[sym] = {
                "last": float(tk.get("lastPrice") or 0.0),
                "chg_pct": float(tk.get("priceChangePercent") or 0.0),
                "vol_quote": float(tk.get("quoteVolume") or 0.0),
            }
    except Exception:
        out = dict(cached or {})
    st.session_state["_home_watch_cache"] = out
    st.session_state["_home_watch_last_ts"] = now
    return out


def _safe_test_name(s: str) -> str:
    t = re.sub(r"[^A-Za-z0-9_-]+", "_", str(s or "").strip())
    t = t.strip("._-")
    return (t[:80] or "").lower()


def _project_root_from_ui_module() -> Path:
    # .../src/microtrans/ui/pages.py -> project root
    return Path(__file__).resolve().parents[3]


def _soak_pid_file(root: Path | None = None) -> Path:
    r = root or _project_root_from_ui_module()
    return r / "runtime" / "soak_executor.pid"


def _read_running_soak_pid(root: Path | None = None) -> int | None:
    pf = _soak_pid_file(root)
    if not pf.exists():
        return None
    try:
        pid = int(pf.read_text(encoding="utf-8").strip())
    except Exception:
        try:
            pf.unlink()
        except Exception:
            pass
        return None
    try:
        os.kill(pid, 0)
    except Exception:
        try:
            pf.unlink()
        except Exception:
            pass
        return None
    if not _pid_looks_like_executor_loop(pid):
        # Evita agir em PID reaproveitado por outro processo.
        try:
            pf.unlink()
        except Exception:
            pass
        return None
    return pid


def _pid_looks_like_executor_loop(pid: int) -> bool:
    p = int(pid)
    if p <= 0:
        return False
    if os.name == "nt":
        try:
            cmd = subprocess.check_output(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    f"(Get-CimInstance Win32_Process -Filter \"ProcessId = {p}\").CommandLine",
                ],
                text=True,
                stderr=subprocess.DEVNULL,
            )
            s = str(cmd or "").lower()
            return ("microtrans.binance_testnet_cli" in s) and ("executor-loop" in s)
        except Exception:
            return False
    proc_cmdline = Path(f"/proc/{p}/cmdline")
    if proc_cmdline.exists():
        try:
            raw = proc_cmdline.read_bytes().decode("utf-8", errors="ignore").replace("\x00", " ").lower()
            return ("microtrans.binance_testnet_cli" in raw) and ("executor-loop" in raw)
        except Exception:
            return False
    return False


def _terminate_pid(pid: int) -> tuple[bool, str]:
    p = int(pid)
    if p <= 0:
        return False, "PID inválido."
    if os.name == "nt":
        try:
            rc = subprocess.run(
                ["taskkill", "/PID", str(p), "/T", "/F"],
                capture_output=True,
                text=True,
                check=False,
            )
            if rc.returncode == 0:
                return True, f"Soak parado (pid={p})."
            detail = (rc.stderr or rc.stdout or "").strip()
            return False, f"Falha ao parar soak por PID ({p}): {detail or 'taskkill retornou erro'}"
        except Exception as e:
            return False, f"Falha ao parar soak por PID ({p}): {e}"
    try:
        os.kill(p, 15)
        time.sleep(2.0)
        try:
            os.kill(p, 0)
            os.kill(p, 9)
        except Exception:
            pass
        return True, f"Soak parado (pid={p})."
    except Exception as e:
        return False, f"Falha ao parar soak por PID ({p}): {e}"


def _write_soak_pid(pid: int, root: Path | None = None) -> None:
    pf = _soak_pid_file(root)
    pf.parent.mkdir(parents=True, exist_ok=True)
    pf.write_text(str(int(pid)), encoding="utf-8")


def _stop_soak_process(st: Any) -> tuple[bool, str]:
    stopped = False
    msg = "Nenhum soak ativo encontrado."
    proc = st.session_state.get("_ui_tn_soak_proc")
    if proc is not None:
        try:
            proc.terminate()
            try:
                proc.wait(timeout=2.0)
            except Exception:
                pass
            stopped = True
            msg = "Pedido de parada enviado ao soak."
        except Exception as e:
            msg = f"Falha ao parar processo da sessão: {e}"

    root = _project_root_from_ui_module()
    pid = _read_running_soak_pid(root)
    if pid is not None:
        ok_pid, pid_msg = _terminate_pid(pid)
        if ok_pid:
            stopped = True
            msg = pid_msg
        else:
            msg = pid_msg
        try:
            _soak_pid_file(root).unlink()
        except Exception:
            pass

    st.session_state.ui_tn_soak_running = False
    st.session_state._ui_tn_soak_proc = None
    return stopped, msg


def _human_operator(agent_src: str) -> str:
    s = str(agent_src or "").lower()
    if "gemini" in s:
        return "IA (Gemini)"
    if "heuristic" in s:
        return "Robô heurístico"
    return "Robô"


def _start_soak_process(
    *,
    symbol: str,
    interval_sec: float,
    max_cycles: int,
    force_heuristic: bool,
    agent_provider: str | None,
    relax_liquidity: bool,
    max_notional_quote_per_order: float | None,
    jsonl_out: str,
    run_label: str | None = None,
) -> subprocess.Popen[str]:
    root = _project_root_from_ui_module()
    cmd = [
        sys.executable,
        "-m",
        "microtrans.binance_testnet_cli",
        "executor-loop",
        "--symbol",
        symbol,
        "--interval-sec",
        str(interval_sec),
        "--max-cycles",
        str(max_cycles),
        "--jsonl-out",
        jsonl_out,
    ]
    if force_heuristic:
        cmd.append("--force-heuristic")
    if agent_provider:
        cmd.extend(["--agent-provider", str(agent_provider).strip().lower()])
    if relax_liquidity:
        cmd.append("--relax-liquidity")
    else:
        cmd.append("--no-relax-liquidity")
    if max_notional_quote_per_order is not None:
        cmd.extend(["--max-notional-quote-per-order", str(float(max_notional_quote_per_order))])
    if run_label and str(run_label).strip():
        cmd.extend(["--run-label", str(run_label).strip()])
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root / "src")
    p = subprocess.Popen(
        cmd,
        cwd=str(root),
        env=env,
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _write_soak_pid(int(p.pid), root)
    return p


def _poll_soak_process(st: Any) -> None:
    proc = st.session_state.get("_ui_tn_soak_proc")
    if proc is None:
        st.session_state.ui_tn_soak_running = _read_running_soak_pid() is not None
        return
    code = proc.poll()
    if code is None:
        st.session_state.ui_tn_soak_running = True
        return
    st.session_state.ui_tn_soak_running = False
    st.session_state.ui_tn_soak_exit_code = int(code)
    st.session_state._ui_tn_soak_proc = None


def _run_testnet_soak_ui(
    st: Any,
    *,
    symbol: str,
    interval_sec: float,
    max_cycles: int,
    force_heuristic: bool,
    jsonl_out: str,
) -> dict[str, Any]:
    cfg = load_config()
    ex = dict(cfg.get("execution") or {})
    base = str(ex.get("testnet_base_url") or "https://testnet.binance.vision")
    pub = BinancePublic(base_url=base)
    signed = BinanceSigned.from_env(require_testnet=True)
    out_path = Path(jsonl_out).expanduser()

    started_at = time.time()
    stats = {
        "cycles": 0,
        "ok_rows": 0,
        "error_rows": 0,
        "quoted_cycles": 0,
        "quoted_placed_cycles": 0,
        "quoted_blocked_cycles": 0,
        "placed_orders_total": 0,
        "cancelled_orders_total": 0,
        "blocked_kill_switch_cycles": 0,
    }
    status_counts: dict[str, int] = {}
    progress = st.progress(0.0)
    info_box = st.empty()
    tail_box = st.empty()
    tail_rows: list[dict[str, Any]] = []

    for step in run_executor_iter(
        symbol=symbol,
        pub=pub,
        signed=signed,
        interval_sec=interval_sec,
        max_cycles=max_cycles,
        cfg=cfg,
        force_heuristic=force_heuristic,
    ):
        row = dict(step)
        row["ts_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        _append_jsonl(out_path, row)

        stats["cycles"] += 1
        if bool(step.get("ok")):
            stats["ok_rows"] += 1
        else:
            stats["error_rows"] += 1
        stt = str(step.get("status") or "unknown")
        status_counts[stt] = status_counts.get(stt, 0) + 1
        if stt.startswith("quoted"):
            stats["quoted_cycles"] += 1
            if stt == "quoted_placed" or stt == "quoted":
                stats["quoted_placed_cycles"] += 1
            elif stt == "quoted_blocked_min_notional_balance":
                stats["quoted_blocked_cycles"] += 1
        if stt == "blocked_kill_switch":
            stats["blocked_kill_switch_cycles"] += 1
        stats["placed_orders_total"] += int(step.get("placed_count") or 0)
        stats["cancelled_orders_total"] += int(step.get("cancelled_count") or 0)

        elapsed = int(time.time() - started_at)
        eta = 0
        if max_cycles > 0:
            progress.progress(min(1.0, stats["cycles"] / max(1, max_cycles)))
            rem = max(0, max_cycles - stats["cycles"])
            eta = int(rem * max(1.0, interval_sec))
        info_box.markdown(
            f"**Soak em execução** · ciclos: `{stats['cycles']}` · elapsed: `{elapsed}s` · ETA: `{eta}s`"
        )

        tail_rows.append(
            {
                "ciclo": row.get("loop_cycle"),
                "status": row.get("status"),
                "ok": row.get("ok"),
                "placed": row.get("placed_count"),
                "cancelled": row.get("cancelled_count"),
                "ts_utc": row.get("ts_utc"),
            }
        )
        tail_rows = tail_rows[-15:]
        tail_box.dataframe(pd.DataFrame(tail_rows[::-1]), use_container_width=True, hide_index=True)

    return {
        "ok": stats["error_rows"] == 0,
        "symbol": symbol.upper(),
        "interval_sec": interval_sec,
        "max_cycles": max_cycles,
        "force_heuristic": force_heuristic,
        "jsonl_out": str(out_path),
        **stats,
        "status_counts": status_counts,
    }


def render_selected_page(
    st: Any,
    *,
    page: str,
    last: dict[str, Any],
    cfg: dict[str, Any],
    eng_state: dict[str, Any],
) -> None:
    if page in {"Tela Inicial", "Painel"}:
        st.markdown("#### Visão executiva")
        hc1, hc2 = st.columns([1.35, 1.0], gap="medium")
        with hc1:
            st.markdown(
                """
                <div class="mt-card soft">
                  <div class="mt-panel-title">MK BOT - MARKET MAKING</div>
                  <div style="color:#A0A0A5;font-size:0.9rem;">
                    Operação em papel + backtest + testnet, com foco em execução auditável e rotina profissional.
                  </div>
                </div>
                """,
                unsafe_allow_html=True,
            )
            watch = _home_market_watch(st, min_sec=30.0)
            wm1, wm2 = st.columns(2)
            btc = watch.get("BTCUSDT") or {}
            sol = watch.get("SOLUSDT") or {}
            wm1.metric("BTCUSDT", f"{float(btc.get('last') or 0):,.2f}", f"{float(btc.get('chg_pct') or 0):+.2f}%")
            wm2.metric("SOLUSDT", f"{float(sol.get('last') or 0):,.4f}", f"{float(sol.get('chg_pct') or 0):+.2f}%")
            nv1, nv2, nv3, nv4 = st.columns(4)
            with nv1:
                if st.button("🧪 Backtest", key="ui_btn_home_nav_backtest", use_container_width=True):
                    st.session_state.ui_page = "Backtest"
                    st.rerun()
            with nv2:
                if st.button("⚙️ Vigília e OP", key="ui_btn_home_nav_vigil", use_container_width=True):
                    st.session_state.ui_page = "Vigília e OP"
                    st.rerun()
            with nv3:
                if st.button("🛰️ Testnet", key="ui_btn_home_nav_testnet", use_container_width=True):
                    st.session_state.ui_page = "Testnet"
                    st.rerun()
            with nv4:
                if st.button("🖥️ Logs", key="ui_btn_home_nav_logs", use_container_width=True):
                    st.session_state.ui_page = "Logs"
                    st.rerun()
        with hc2:
            img = _home_brand_image_path()
            if img is not None:
                st.image(str(img), use_container_width=True)

        if not last:
            st.info("Ainda sem dados. Use **Rodar um tick agora** para iniciar o painel.")
        else:
            apt = last.get("filter_apt")
            st.markdown('<div class="mt-panel-title">Resumo operacional</div>', unsafe_allow_html=True)
            if apt:
                st.success("Mercado **apto** — filtros (com as regras atuais) permitem estratégia.")
            else:
                st.error("Mercado **não apto** — veja critérios abaixo ou ative relaxação de liquidez na barra lateral.")

            m = last.get("metrics") or {}
            st.markdown('<div class="mt-kpi-grid">', unsafe_allow_html=True)
            g1, g2, g3, g4 = st.columns(4)
            g1.metric("Preço (mid)", f"{float(last.get('mid') or 0):,.2f}")
            g2.metric("Spread (bps)", f"{float(last.get('spread_bps') or 0):.2f}")
            g3.metric("ADX", f"{float(m.get('adx', 0)):.2f}")
            g4.metric("Range/ATR", f"{float(m.get('range_atr_ratio', 0)):.2f}")
            st.markdown("</div>", unsafe_allow_html=True)

            cx_chart, cx_list = st.columns([2.0, 1.0], gap="small")
            with cx_chart:
                with st.container(border=True):
                    st.markdown('<div class="mt-panel-title">Patrimônio estimado (sessão)</div>', unsafe_allow_html=True)
                    ar = st.session_state.get("audit_rows") or []
                    if ar:
                        df_eq = pd.DataFrame(ar)
                        ser = df_eq["carteira_patrimonio"].dropna() if "carteira_patrimonio" in df_eq.columns else pd.Series([])
                        if not ser.empty:
                            st.line_chart(ser.reset_index(drop=True), height=230, use_container_width=True)
                        else:
                            st.caption("Sem patrimônio suficiente para gráfico nesta sessão.")
                    else:
                        st.caption("Sem histórico de ticks para gráfico ainda.")
            with cx_list:
                with st.container(border=True):
                    st.markdown('<div class="mt-panel-title">Insights rápidos</div>', unsafe_allow_html=True)
                    paper = last.get("paper_step") or {}
                    st.markdown(
                        "\n".join(
                            [
                                f"- Status do ciclo: **{last.get('event') or 'sem evento'}**",
                                f"- Fonte do agente: **{((eng_state.get('params') or {}).get('meta') or {}).get('source', '—')}**",
                                f"- Modo papel: **{paper.get('paper_pricing') or '—'}**",
                                f"- Liquidez relaxada: **{'sim' if st.session_state.get('relax_liquidity') else 'não'}**",
                            ]
                        )
                    )
                    if paper:
                        st.caption(f"Última observação do papel: {paper.get('reason') or paper.get('slip_note') or 'ok'}")

            rows = last.get("filter_diagnostic") or []
            with st.container(border=True):
                st.markdown('<div class="mt-panel-title">Critérios de filtro</div>', unsafe_allow_html=True)
                if rows:
                    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
                else:
                    st.caption("Sem tabela de critérios neste momento.")

            st.markdown("#### Diagnóstico e detalhes técnicos")
            st.text(last.get("filter_human") or "")
            _engx = cfg.get("engine") or {}
            st.caption(
                f"Papel (barra lateral + YAML): `paper_pricing={_engx.get('paper_pricing', 'book_top')}`, "
                f"`paper_slippage_bps={_engx.get('paper_slippage_bps', 0)}`, "
                f"`paper_fill_probability={_engx.get('paper_fill_probability', 1.0)}` · "
                f"SL ciclo {_engx.get('exit_drawdown_pct', 3)}% · "
                f"TP {_engx.get('exit_take_profit_pct', 0) or 0}% / { _engx.get('exit_take_profit_quote', 0) or 0} quote"
            )
            st.json(
                {
                    "evento": last.get("event"),
                    "motor": eng_state,
                    "papel": last.get("paper_step"),
                }
            )
            prm = (eng_state.get("params") or {}) if eng_state else {}
            if prm:
                with st.expander("Contrato do agente (v1) — parâmetros validados"):
                    st.caption(
                        "Campos obrigatórios no payload: spread_bps, order_size_quote, max_inventory_base. "
                        "Limites em `config/default.yaml` → `agent.bounds`."
                    )
                    st.json(prm)

        st.markdown("---")
        st.markdown("#### Resumo das carteiras")
        csum1, csum2, csum3 = st.columns(3)
        # Carteira de vigília/papel
        w = st.session_state.get("wallet")
        mid = float(last.get("mid") or 0.0)
        if w is not None and mid > 0:
            sw = w.summary(mid)
            csum1.metric("Carteira Vigília (Papel)", f"{float(sw.get('patrimonio_quote', 0.0)):,.4f}")
            csum1.caption(f"PnL sessão: {float(sw.get('pnl_vs_inicio_quote', 0.0)):+,.4f}")
        else:
            csum1.metric("Carteira Vigília (Papel)", "—")
            csum1.caption("Ainda sem referência de preço.")
        # Carteira testnet
        tn_rows = st.session_state.get("testnet_equity_rows") or []
        if tn_rows:
            dft = pd.DataFrame(tn_rows)
            if "symbol" in dft.columns:
                dft = dft[dft["symbol"] == str(st.session_state.get("symbol") or "BTCUSDT").upper()]
            if not dft.empty:
                last_eq = float(dft["equity_quote"].iloc[-1])
                last_pnl = float(dft["pnl_vs_start"].iloc[-1])
                csum2.metric("Carteira Testnet", f"{last_eq:,.4f}")
                csum2.caption(f"PnL mark-to-market: {last_pnl:+,.4f}")
            else:
                csum2.metric("Carteira Testnet", "—")
                csum2.caption("Sem snapshot no símbolo atual.")
        else:
            csum2.metric("Carteira Testnet", "—")
            csum2.caption("Sem snapshots de testnet.")
        # Carteira backtest (última corrida)
        bt = st.session_state.get("last_bt") or {}
        fw = bt.get("final_wallet") or {}
        if fw:
            csum3.metric("Carteira Backtest", f"{float(fw.get('patrimonio_quote', 0.0)):,.4f}")
            csum3.caption(f"PnL replay: {float((bt.get('summary') or {}).get('pnl_quote_vs_start', 0.0)):+,.4f}")
        else:
            csum3.metric("Carteira Backtest", "—")
            csum3.caption("Sem replay recente.")

    elif page == "Vigília e OP":
        st.markdown("#### Configuração da operação manual e vigília")
        o1, o2 = st.columns([1.15, 1.0], gap="medium")
        with o1:
            with st.container(border=True):
                st.markdown('<div class="mt-panel-title">Setup de execução (papel)</div>', unsafe_allow_html=True)
                st.text_input(
                    "Ativo (par)",
                    value=str(st.session_state.get("symbol") or "BTCUSDT"),
                    key="symbol",
                    help="Ex.: BTCUSDT. Base para tick, vigília e simulação.",
                )
                st.checkbox(
                    "Relaxar filtro de liquidez",
                    value=bool(st.session_state.get("relax_liquidity", False)),
                    key="relax_liquidity",
                )
                ap_ix = 1 if str(st.session_state.get("ui_agent_provider") or "heuristic") == "gemini" else 0
                st.selectbox(
                    "Provedor do agente",
                    options=["heuristic", "gemini"],
                    index=ap_ix,
                    format_func=lambda x: {"heuristic": "Heurística local", "gemini": "Google Gemini"}.get(x, x),
                    key="ui_agent_provider",
                )
                pp_ix = 1 if str(st.session_state.get("ui_paper_pricing") or "book_top") == "book_top" else 0
                st.selectbox(
                    "Modo de preço no papel",
                    options=["synthetic_mid", "book_top"],
                    index=pp_ix,
                    format_func=lambda x: {"synthetic_mid": "Livro simulado", "book_top": "Topo do livro"}.get(x, x),
                    key="ui_paper_pricing",
                )
                st.number_input("Deslizamento (bps)", min_value=0.0, max_value=200.0, step=0.5, key="ui_paper_slip")
                st.slider("Probabilidade de fill", min_value=0.0, max_value=1.0, step=0.05, key="ui_paper_fill")
                st.number_input("Stop do ciclo (%)", min_value=0.1, max_value=90.0, step=0.25, key="ui_exit_dd")
                st.number_input("Take profit do ciclo (%)", min_value=0.0, max_value=500.0, step=0.1, key="ui_tp_pct")
                st.number_input("Take profit do ciclo (quote)", min_value=0.0, max_value=1e12, step=1.0, key="ui_tp_quote")

        with o2:
            with st.container(border=True):
                st.markdown('<div class="mt-panel-title">Resumo da vigília</div>', unsafe_allow_html=True)
                st.markdown(
                    "\n".join(
                        [
                            f"- Ativo: **{str(st.session_state.get('symbol') or 'BTCUSDT').upper()}**",
                            f"- Intervalo entre ciclos: **{int(st.session_state.get('ui_auto_step', 15))}s**",
                            f"- Duração: **{int(st.session_state.get('ui_auto_dur', 120))}s**",
                            f"- Agente: **{str(st.session_state.get('ui_agent_provider') or 'heuristic')}**",
                            f"- Liquidez relaxada: **{'sim' if st.session_state.get('relax_liquidity') else 'não'}**",
                            f"- Estado: **{'ativo' if st.session_state.get('vigil_active') else 'parado'}**",
                        ]
                    )
                )
                st.number_input("Duração da vigília (seg)", min_value=10, max_value=7200, step=10, key="ui_auto_dur")
                st.number_input("Intervalo entre ciclos (seg)", min_value=2, max_value=600, step=1, key="ui_auto_step")

        with st.container(border=True):
            st.markdown('<div class="mt-panel-title">Ações rápidas</div>', unsafe_allow_html=True)
            a1, a2, a3, a4 = st.columns(4)
            with a1:
                if st.button("Ping API Binance", key="ui_btn_ping_page", use_container_width=True):
                    st.session_state.ui_ping_pending = True
                    st.rerun()
            with a2:
                if st.button("Rodar um tick agora", key="ui_btn_tick_page", type="primary", use_container_width=True):
                    st.session_state.ui_tick_pending = True
                    st.rerun()
            with a3:
                if st.button("Iniciar vigília", key="ui_btn_vigil_page", use_container_width=True):
                    st.session_state.ui_run_auto_pending = True
                    st.rerun()
            with a4:
                if st.button("Resetar carteira UI", key="ui_btn_reset_wallet_page", use_container_width=True):
                    st.session_state.ui_reset_wallet_pending = True
                    st.rerun()
            if st.session_state.get("vigil_active"):
                if st.button("Parar vigília", key="ui_btn_vigil_stop_page"):
                    st.session_state.vigil_active = False
                    st.success("Vigília parada.")
                    st.rerun()
            st.caption("Regra operacional: logs detalhados ficam na página Logs.")

        st.markdown("#### Resultado da operação/vigília")
        w = st.session_state.get("wallet")
        mid = float(last.get("mid") or 0.0)
        with st.container(border=True):
            if w is not None and mid > 0:
                sm = w.summary(mid)
                k1, k2, k3, k4 = st.columns(4)
                k1.metric("Patrimônio", f"{float(sm.get('patrimonio_quote', 0.0)):,.4f}")
                k2.metric("PnL sessão", f"{float(sm.get('pnl_vs_inicio_quote', 0.0)):+,.4f}")
                k3.metric("Operações", str(sm.get("n_operacoes", 0)))
                k4.metric("Taxas", f"{float(sm.get('taxas_pagas_total_quote', 0.0)):,.4f}")
            ar = st.session_state.get("audit_rows") or []
            if ar:
                dfa = pd.DataFrame(ar)
                if "carteira_patrimonio" in dfa.columns and not dfa["carteira_patrimonio"].dropna().empty:
                    st.line_chart(dfa["carteira_patrimonio"].dropna().reset_index(drop=True), height=220, use_container_width=True)
                cols = [c for c in ["hora_utc", "evento", "sim_volta_ok", "sim_resultado_quote", "carteira_patrimonio"] if c in dfa.columns]
                if cols:
                    st.dataframe(dfa[cols].iloc[::-1].head(30), use_container_width=True, hide_index=True)
            else:
                st.info("Sem histórico de operação nesta sessão. Rode tick ou vigília.")

    elif page == "Mercado":
        c_book, c_24 = st.columns([1.25, 1.0], gap="small")
        with c_book:
            with st.container(border=True):
                st.markdown('<div class="mt-panel-title">Livro de ordens (snapshot)</div>', unsafe_allow_html=True)
                if not last.get("book_preview"):
                    st.caption("Faça um tick para carregar o book.")
                else:
                    bp = last["book_preview"]
                    bc, ac = st.columns(2)
                    with bc:
                        st.caption("Bids (compra)")
                        st.dataframe(
                            pd.DataFrame(bp.get("bids") or [], columns=["Preço", "Qtd base"]),
                            use_container_width=True,
                            hide_index=True,
                        )
                    with ac:
                        st.caption("Asks (venda)")
                        st.dataframe(
                            pd.DataFrame(bp.get("asks") or [], columns=["Preço", "Qtd base"]),
                            use_container_width=True,
                            hide_index=True,
                        )
        with c_24:
            with st.container(border=True):
                st.markdown('<div class="mt-panel-title">Resumo 24h (API)</div>', unsafe_allow_html=True)
                m24 = last.get("market_24h") or {}
                if not m24:
                    st.caption("Sem snapshot 24h ainda.")
                else:
                    k1, k2 = st.columns(2)
                    k1.metric("Variação 24h", f"{float(m24.get('variacao_pct_24h', 0.0)):+.2f}%")
                    k2.metric("Volume quote", f"{float(m24.get('volume_quote_24h', 0.0)):,.0f}")
                    st.caption(
                        f"Máxima: {m24.get('high_24h', '—')} | Mínima: {m24.get('low_24h', '—')} | Último: {m24.get('ultimo', '—')}"
                    )

    elif page == "Carteira":
        w = st.session_state.wallet
        wmid = float(last.get("mid") or 0) if last else 0.0
        sm = w.summary(wmid if wmid > 0 else 1.0)
        st.markdown("#### Carteira simulada (modo papel)")
        st.markdown('<div class="mt-kpi-grid">', unsafe_allow_html=True)
        k1, k2, k3, k4 = st.columns(4)
        k1.metric(f"Saldo {sm.get('ativo_quote')}", f"{sm.get('quote_saldo', 0):,.4f}")
        k2.metric(f"Saldo {sm.get('ativo_base')}", f"{sm.get('base_saldo', 0):.8f}")
        k3.metric("Patrimônio", f"{sm.get('patrimonio_quote', 0):,.4f}")
        k4.metric("PnL vs início", f"{sm.get('pnl_vs_inicio_quote', 0):+,.4f}")
        k5, k6, k7 = st.columns(3)
        k5.metric("Taxas", f"{sm.get('taxas_pagas_total_quote', 0):,.4f}")
        k6.metric("Volume tradeado (quote)", f"{sm.get('volume_quote_negociado', 0):,.2f}")
        k7.metric("Taxa média / op.", f"{sm.get('taxa_media_por_operacao', 0):.6f}")
        st.markdown("</div>", unsafe_allow_html=True)
        st.caption(w.explain(wmid if wmid > 0 else 1.0))
        trades = w.recent_trades(40)
        with st.container(border=True):
            st.markdown('<div class="mt-panel-title">Operações recentes</div>', unsafe_allow_html=True)
            if trades:
                st.dataframe(pd.DataFrame(trades), use_container_width=True, hide_index=True)
            else:
                st.caption("Sem operações na sessão atual.")
            csv_data = w.trades_to_csv()
            st.download_button(
                "Exportar CSV",
                data=csv_data,
                file_name=f"trades_{st.session_state.symbol}.csv",
                mime="text/csv",
            )

    elif page == "Backtest":
        st.markdown("#### Backtest (Replay P3b)")
        st.caption("Fluxo de laboratório: gera artefatos próprios (CSV/JSONL) separados da auditoria ao vivo.")
        with st.container(border=True):
            st.markdown('<div class="mt-panel-title">Parâmetros do backtest</div>', unsafe_allow_html=True)
            c1, c2, c3 = st.columns(3)
            bty = cfg.get("backtest") or {}
            fc = dict(cfg.get("filters") or {})
            iv_list = list(fc.get("kline_supported_intervals", [])) or ["1m", "3m", "5m", "15m", "30m", "1h", "4h", "1d"]
            iv_def = str(st.session_state.get("ui_bt_interval") or fc.get("kline_interval", "5m"))
            if iv_def not in iv_list:
                iv_list = sorted(set(iv_list + [iv_def]))
            with c1:
                st.text_input("Ativo (vazio = painel)", value=str(st.session_state.get("ui_bt_symbol") or ""), key="ui_bt_symbol")
                st.selectbox("Intervalo", options=iv_list, index=iv_list.index(iv_def), key="ui_bt_interval")
                st.selectbox(
                    "Provedor do agente",
                    options=["heuristic", "gemini"],
                    index=1 if str(st.session_state.get("ui_agent_provider") or "heuristic") == "gemini" else 0,
                    format_func=lambda x: {"heuristic": "Heurística local", "gemini": "Google Gemini"}.get(x, x),
                    key="ui_agent_provider",
                )
            with c2:
                st.radio(
                    "Modo dos dados",
                    options=["last_n", "range"],
                    index=0 if str(st.session_state.get("ui_bt_mode") or "last_n") == "last_n" else 1,
                    format_func=lambda x: {"last_n": "Últimas N velas", "range": "Intervalo data/hora UTC"}.get(x, x),
                    key="ui_bt_mode",
                )
                st.number_input(
                    "Barras (N)",
                    min_value=30,
                    max_value=int(bty.get("max_bars", 20000)),
                    value=int(st.session_state.get("ui_bt_bars") or bty.get("default_bars", 500)),
                    step=10,
                    key="ui_bt_bars",
                )
                st.number_input(
                    "Passo do replay",
                    min_value=1,
                    max_value=500,
                    value=int(st.session_state.get("ui_bt_step") or 1),
                    key="ui_bt_step",
                )
                st.checkbox(
                    "Fixar instante final da janela (UTC)",
                    key="ui_bt_fix_end",
                    help=(
                        "Desmarcado: usa a última vela disponível no momento do clique. "
                        "Marcado: usa data/hora fixa para replay reprodutível."
                    ),
                )
            with c3:
                preset_opts = ["(nenhum)"] + list((bty.get("stress_presets") or {}).keys())
                preset_def = str(st.session_state.get("ui_bt_preset") or "(nenhum)")
                if preset_def not in preset_opts:
                    preset_opts = preset_opts + [preset_def]
                st.selectbox("Preset de stress", options=preset_opts, index=preset_opts.index(preset_def), key="ui_bt_preset")
                st.checkbox("Forçar heurística nesta corrida", value=bool(st.session_state.get("ui_bt_force_heuristic")), key="ui_bt_force_heuristic")
                st.selectbox(
                    "Modo de preço no papel",
                    options=["synthetic_mid", "book_top"],
                    index=1 if str(st.session_state.get("ui_paper_pricing") or "book_top") == "book_top" else 0,
                    format_func=lambda x: {"synthetic_mid": "Livro simulado", "book_top": "Topo do livro"}.get(x, x),
                    key="ui_paper_pricing",
                )
            st.markdown("---")
            mode_bt = str(st.session_state.get("ui_bt_mode") or "last_n")
            if mode_bt == "range":
                now_u = datetime.now(timezone.utc)
                if "ui_bt_r_start_d" not in st.session_state:
                    st.session_state.ui_bt_r_start_d = (now_u.date())
                if "ui_bt_r_start_t" not in st.session_state:
                    st.session_state.ui_bt_r_start_t = datetime.min.time().replace(hour=0, minute=0, second=0)
                if "ui_bt_r_end_d" not in st.session_state:
                    st.session_state.ui_bt_r_end_d = now_u.date()
                if "ui_bt_r_end_t" not in st.session_state:
                    st.session_state.ui_bt_r_end_t = datetime.min.time().replace(hour=now_u.hour, minute=now_u.minute, second=0)
                r1, r2, r3, r4 = st.columns(4)
                with r1:
                    st.date_input("Início (data UTC)", key="ui_bt_r_start_d")
                with r2:
                    st.time_input("Início (hora UTC)", key="ui_bt_r_start_t")
                with r3:
                    st.date_input("Fim (data UTC)", key="ui_bt_r_end_d")
                with r4:
                    st.time_input("Fim (hora UTC)", key="ui_bt_r_end_t")
                st.caption(
                    f"Teto de segurança: **{int(bty.get('max_bars', 20000))}** velas por corrida."
                )
            elif bool(st.session_state.get("ui_bt_fix_end")):
                now_u = datetime.now(timezone.utc)
                if "ui_bt_end_d" not in st.session_state:
                    st.session_state.ui_bt_end_d = now_u.date()
                if "ui_bt_end_t" not in st.session_state:
                    st.session_state.ui_bt_end_t = datetime.min.time().replace(hour=now_u.hour, minute=now_u.minute, second=0)
                e1, e2 = st.columns(2)
                with e1:
                    st.date_input("Fim da janela (data UTC)", key="ui_bt_end_d")
                with e2:
                    st.time_input("Fim da janela (hora UTC)", key="ui_bt_end_t")
        if st.button("Rodar backtest com a configuração atual", key="ui_btn_bt_page", type="primary"):
            st.session_state.ui_bt_run_pending = True
            st.rerun()
        bt = st.session_state.get("last_bt") or {}
        if not bt:
            st.info("Sem corrida nesta sessão. Configure os parâmetros na sidebar e clique em **Rodar backtest**.")
        else:
            with st.container(border=True):
                st.markdown('<div class="mt-panel-title">Resumo da corrida</div>', unsafe_allow_html=True)
                st.markdown(bt.get("closing_explanation", ""))
                st.info(bt.get("nota_determinismo", ""))
            rs = bt.get("replay_spec") or {}
            _fim_lbl = rs.get("end_time_utc_iso") or (
                "agora (no clique)" if rs.get("fetch_mode") == "last_n" else "—"
            )
            st.caption(
                f"`bt_run_id` **{bt.get('bt_run_id', '—')}** · modo **{rs.get('fetch_mode', '—')}** · "
                f"{rs.get('symbol', '—')} · vela **{rs.get('kline_interval', '—')}** · "
                f"N pedido **{rs.get('bars_requested', '—')}** · barras_df **{rs.get('bars_fetched', '—')}** · "
                f"UTC início **{rs.get('start_time_utc_iso') or '—'}** · fim **{_fim_lbl}** · "
                f"heurística_forçada **{rs.get('force_agent_heuristic', False)}** · preset **{rs.get('stress_preset') or '—'}**"
            )
            summ = bt.get("summary") or {}
            if summ:
                st.markdown('<div class="mt-kpi-grid">', unsafe_allow_html=True)
                s1, s2, s3, s4, s5 = st.columns(5)
                s1.metric("Passos replay", str(summ.get("replay_steps", "—")))
                s2.metric("% ticks apto", f"{summ.get('pct_steps_filter_apt', 0):.2f}%")
                s3.metric("Voltas papel OK", str(summ.get("paper_roundtrip_ok", "—")))
                s4.metric("Aberturas ciclo", str(summ.get("cycle_start_events", "—")))
                s5.metric("PnL vs início (quote)", f"{summ.get('pnl_quote_vs_start', 0):+.6f}")
                st.markdown("</div>", unsafe_allow_html=True)
                with st.expander("Diagnóstico: papel e ciclos (sem abrir o CSV)", expanded=False):
                    st.markdown("**Falhas / skips de ida+volta** (`paper_ok=false`) por motivo:")
                    st.json(summ.get("paper_failure_reason_counts") or {})
                    st.markdown("**Eventos `cycle_end`** por motivo:")
                    st.json(summ.get("cycle_end_reason_counts") or {})
                    if summ.get("klines_fetch"):
                        st.markdown("**Metadados do carregamento de velas:**")
                        st.json(summ.get("klines_fetch"))
            fw = bt.get("final_wallet") or {}
            st.markdown('<div class="mt-kpi-grid">', unsafe_allow_html=True)
            b1, b2, b3, b4 = st.columns(4)
            b1.metric("Passos", str(bt.get("steps", "—")))
            b2.metric("Ciclos motor", str(bt.get("motor_cycle_counter", "—")))
            b3.metric("Operações", str(fw.get("n_operacoes", "—")))
            b4.metric("Patrimônio final", f"{fw.get('patrimonio_quote', 0):,.4f}")
            st.markdown("</div>", unsafe_allow_html=True)
            ap = bt.get("artifact_paths") or {}
            if ap.get("audit_csv_path"):
                st.caption(f"Ficheiros gravados: `{ap.get('audit_csv_path')}` · `{ap.get('agent_jsonl_path', '')}`")
            _bt_skip_json = {"history_tail", "audit_csv", "agent_decisions_jsonl"}
            st.json({k: v for k, v in bt.items() if k not in _bt_skip_json})
            _ac = bt.get("audit_csv") or ""
            _aj = bt.get("agent_decisions_jsonl") or ""
            d1, d2 = st.columns(2)
            with d1:
                st.download_button(
                    "Descarregar auditoria do backtest (CSV)",
                    data=_ac.encode("utf-8-sig"),
                    file_name=f"bt_{bt.get('bt_run_id', 'run')}_audit.csv",
                    mime="text/csv",
                    key="dl_bt_audit",
                    disabled=not _ac,
                )
            with d2:
                st.download_button(
                    "Descarregar decisões do agente (JSONL)",
                    data=_aj.encode("utf-8"),
                    file_name=f"bt_{bt.get('bt_run_id', 'run')}_agent.jsonl",
                    mime="text/plain",
                    key="dl_bt_agent",
                    disabled=not _aj,
                )

    elif page == "Testnet":
        st.markdown("#### Spot Testnet (P4t)")
        st.caption(
            "Validação de execução em saldo fictício da Binance. Não afeta conta live."
        )
        sym0 = str(st.session_state.get("ui_tn_soak_symbol") or st.session_state.get("symbol") or "BTCUSDT").upper()
        jsonl0 = Path(
            str(
                st.session_state.get("ui_tn_soak_active_jsonl")
                or st.session_state.get("ui_tn_soak_jsonl")
                or "auditoria/testnet/soak_ui_btcusdt.jsonl"
            )
        ).expanduser()
        tn_rows = st.session_state.get("testnet_equity_rows") or []
        dft = pd.DataFrame(tn_rows) if tn_rows else pd.DataFrame()
        if not dft.empty and "symbol" in dft.columns:
            dft = dft[dft["symbol"] == sym0]
        st.markdown('<div class="mt-panel-title">Carteira Testnet integrada ao painel</div>', unsafe_allow_html=True)
        if not dft.empty:
            last_eq = float(dft["equity_quote"].iloc[-1])
            pnl_eq = float(dft["pnl_vs_start"].iloc[-1])
            quote_lbl = str(dft["quote_asset"].iloc[-1])
            mark_ref = float(dft["mark_price"].iloc[-1]) if "mark_price" in dft.columns else 0.0
            rob = _maybe_refresh_robot_pnl(
                st,
                symbol=sym0,
                jsonl_path=jsonl0,
                min_sec=12.0,
            )
            pnl_robot = float(rob.get("pnl_robot_total_quote") or 0.0)
            pnl_robot_cls = "mt-wallet-pnl-up" if pnl_robot >= 0 else "mt-wallet-pnl-down"
            w = st.session_state.get("wallet")
            mid = float(last.get("mid") or 0.0)
            ref_px = mid if mid > 0 else mark_ref
            paper_eq = float(w.equity_quote(ref_px)) if (w is not None and ref_px > 0) else None
            diff_txt = f"{(last_eq - paper_eq):+,.4f} {quote_lbl}" if paper_eq is not None else "—"
            pnl_cls = "mt-wallet-pnl-up" if pnl_eq >= 0 else "mt-wallet-pnl-down"
            diff_cls = "mt-wallet-pnl-up" if (paper_eq is not None and (last_eq - paper_eq) >= 0) else "mt-wallet-pnl-down"
            paper_box = ""
            if w is not None and ref_px > 0:
                smp = w.summary(ref_px)
                pnlp = float(smp.get("pnl_vs_inicio_quote", 0.0))
                pnlp_cls = "mt-wallet-pnl-up" if pnlp >= 0 else "mt-wallet-pnl-down"
                paper_box = f"""
                <div class="mt-wallet-box">
                  <div class="mt-wallet-title">Saldo Estimado (Papel)</div>
                  <div class="mt-wallet-row"><span class="mt-wallet-muted">Patrimônio</span><span>{float(smp.get("patrimonio_quote", 0.0)):,.4f} {smp.get("ativo_quote","USDT")}</span></div>
                  <div class="mt-wallet-row"><span class="mt-wallet-muted">PnL sessão</span><span class="{pnlp_cls}">{pnlp:+,.4f}</span></div>
                  <div class="mt-wallet-row"><span class="mt-wallet-muted">Referência preço</span><span>{ref_px:,.2f}</span></div>
                </div>
                """
            testnet_box = f"""
            <div class="mt-wallet-box">
              <div class="mt-wallet-title">Saldo Testnet (Corretora)</div>
              <div class="mt-wallet-row"><span class="mt-wallet-muted">Patrimônio</span><span>{last_eq:,.4f} {quote_lbl}</span></div>
              <div class="mt-wallet-row"><span class="mt-wallet-muted">PnL mark-to-market</span><span class="{pnl_cls}">{pnl_eq:+,.4f} {quote_lbl}</span></div>
              <div class="mt-wallet-row"><span class="mt-wallet-muted">PnL real do robô (fills)</span><span class="{pnl_robot_cls}">{pnl_robot:+,.4f} {quote_lbl}</span></div>
              <div class="mt-wallet-row"><span class="mt-wallet-muted">Diferença vs papel</span><span class="{diff_cls}">{diff_txt}</span></div>
            </div>
            """
            bx1, bx2 = st.columns(2, gap="medium")
            with bx1:
                st.markdown(testnet_box, unsafe_allow_html=True)
            with bx2:
                if paper_box:
                    st.markdown(paper_box, unsafe_allow_html=True)
                else:
                    st.caption("Saldo Estimado (papel) ainda sem referência de preço.")
            rb1, rb2 = st.columns([1.2, 1.0])
            with rb1:
                if st.button("Resetar referência de lucro testnet", key="ui_btn_tn_reset_pnl_ref"):
                    # Recomeça série e baseline para evitar mistura de sessões anteriores.
                    rows_all = st.session_state.get("testnet_equity_rows") or []
                    st.session_state.testnet_equity_rows = [
                        r for r in rows_all if str(r.get("symbol") or "").upper() != sym0
                    ]
                    bmap = st.session_state.get("testnet_equity_baseline_by_symbol") or {}
                    bmap.pop(f"{sym0}_eq0", None)
                    st.session_state.pop("_tn_robot_pnl_cache", None)
                    st.session_state.pop("_tn_robot_pnl_last_ts", None)
                    st.session_state.pop("_tn_robot_pnl_src", None)
                    st.session_state.pop("_tn_robot_pnl_error", None)
                    try:
                        _capture_testnet_equity_snapshot_ui(st, symbol=sym0)
                        st.success("Referência de lucro resetada. Novo ponto inicial criado.")
                    except Exception as e:
                        st.warning(f"Referência resetada, mas não consegui capturar snapshot agora: {e}")
                    st.rerun()
            with rb2:
                st.caption(
                    "Use este reset ao iniciar nova simulação. "
                    "Assim o PnL não acumula operações antigas."
                )
                if st.session_state.get("_tn_robot_pnl_error"):
                    st.caption(f"Leitura de fills indisponível agora: {st.session_state.get('_tn_robot_pnl_error')}")
            st.line_chart(
                dft[["equity_quote"]].reset_index(drop=True),
                height=170,
                use_container_width=True,
            )
            st.caption(
                "Curva de patrimônio testnet (marcação a mercado em USDT), comparada ao saldo estimado do papel."
            )
        else:
            st.info("Sem snapshots de patrimônio testnet ainda. Use 'Consultar conta testnet' para iniciar a série.")
        if "ui_tn_soak_running" not in st.session_state:
            st.session_state.ui_tn_soak_running = False
        if "ui_tn_soak_exit_code" not in st.session_state:
            st.session_state.ui_tn_soak_exit_code = None
        if "ui_tn_soak_active_jsonl" not in st.session_state:
            st.session_state.ui_tn_soak_active_jsonl = str(
                st.session_state.get("ui_tn_soak_jsonl") or "auditoria/testnet/soak_ui_btcusdt.jsonl"
            )
        _poll_soak_process(st)
        tab_ops, tab_soak, tab_report = st.tabs(["Menu rápido", "Soak 24/7", "Relatório"])
        with tab_ops:
            t1, t2 = st.columns(2)
            with t1:
                if st.button("Consultar conta testnet", key="ui_btn_tn_account_page"):
                    st.session_state.ui_tn_account_pending = True
                    st.rerun()
            with t2:
                if st.button("Rodar order-smoke", key="ui_btn_tn_smoke_page", type="primary"):
                    st.session_state.ui_tn_smoke_pending = True
                    st.rerun()
            st.caption("Os botões acima executam o mesmo fluxo do bloco lateral, mantendo um ponto único de execução.")
            acc = st.session_state.get("last_testnet_account") or {}
            smk = st.session_state.get("last_testnet_smoke") or {}
            if acc:
                st.markdown('<div class="mt-panel-title">Última consulta de conta</div>', unsafe_allow_html=True)
                c1, c2, c3 = st.columns(3)
                c1.metric("canTrade", str(acc.get("canTrade")))
                c2.metric("Saldos (total)", str(acc.get("balances_total", "—")))
                c3.metric("Maker/Taker", f"{acc.get('makerCommission', '—')} / {acc.get('takerCommission', '—')}")
                with st.expander("Ver JSON completo da conta", expanded=False):
                    st.json(acc)
            else:
                st.caption("Ainda sem consulta de conta nesta sessão.")
            if smk:
                st.markdown('<div class="mt-panel-title">Último order-smoke</div>', unsafe_allow_html=True)
                with st.expander("Ver JSON do order-smoke", expanded=False):
                    st.json(smk)
            else:
                st.caption("Ainda sem order-smoke nesta sessão.")
            if st.button("Atualizar patrimônio testnet agora", key="ui_btn_tn_refresh_equity"):
                try:
                    sym_eq = str(st.session_state.get("ui_tn_symbol") or st.session_state.get("symbol") or "BTCUSDT")
                    _capture_testnet_equity_snapshot_ui(st, symbol=sym_eq)
                    st.success("Patrimônio testnet atualizado.")
                except Exception as e:
                    st.error(f"Falha ao atualizar patrimônio testnet: {e}")

        with tab_soak:
            st.caption("Soak com start/stop na UI e trilha JSONL para relatório.")
            with st.container(border=True):
                st.markdown('<div class="mt-panel-title">Setup do soak</div>', unsafe_allow_html=True)
                c1, c2 = st.columns(2)
                with c1:
                    ui_symbol = st.text_input("Símbolo do soak", value="BTCUSDT", key="ui_tn_soak_symbol")
                    ui_interval = st.number_input(
                        "Intervalo entre ciclos (seg)",
                        min_value=2.0,
                        max_value=600.0,
                        value=15.0,
                        step=1.0,
                        key="ui_tn_soak_interval",
                    )
                with c2:
                    ui_cycles = st.number_input(
                        "Total de ciclos (ex.: 480 ~ 2h em 15s)",
                        min_value=1,
                        max_value=200000,
                        value=480,
                        step=1,
                        key="ui_tn_soak_cycles",
                    )
                    ui_force_h = st.checkbox(
                        "Forçar heurística (sobrescreve provedor para sem custo Gemini)",
                        value=True,
                        key="ui_tn_soak_force_heuristic",
                    )
                    _ap_def = str(st.session_state.get("ui_agent_provider") or "heuristic").strip().lower()
                    _ap_ix = 1 if _ap_def == "gemini" else 0
                    ui_tn_agent_provider = st.selectbox(
                        "Provedor do agente (Testnet)",
                        options=["heuristic", "gemini"],
                        index=_ap_ix,
                        format_func=lambda x: {
                            "heuristic": "Heurística local",
                            "gemini": "Google Gemini",
                        }.get(x, x),
                        key="ui_tn_agent_provider",
                    )
                    ui_relax_liq = st.checkbox(
                        "Relaxar filtro de liquidez (recomendado para validar execução)",
                        value=True,
                        key="ui_tn_soak_relax_liquidity",
                    )
            with st.container(border=True):
                st.markdown('<div class="mt-panel-title">Gestão de risco da sessão</div>', unsafe_allow_html=True)
                r1, r2, r3, r4 = st.columns(4)
            with r1:
                ui_bank_quote = st.number_input(
                    "Banca da sessão (quote)",
                    min_value=10.0,
                    max_value=1_000_000_000.0,
                    value=1000.0,
                    step=10.0,
                    key="ui_tn_risk_bank_quote",
                )
            with r2:
                ui_risk_pct = st.number_input(
                    "Risco máx por ordem (%)",
                    min_value=0.1,
                    max_value=100.0,
                    value=5.0,
                    step=0.1,
                    key="ui_tn_risk_per_order_pct",
                )
            with r3:
                ui_target_quote = st.number_input(
                    "Meta de lucro sessão (quote)",
                    min_value=0.0,
                    max_value=1_000_000_000.0,
                    value=20.0,
                    step=1.0,
                    key="ui_tn_risk_target_quote",
                )
            with r4:
                ui_stop_quote = st.number_input(
                    "Stop loss sessão (quote)",
                    min_value=0.0,
                    max_value=1_000_000_000.0,
                    value=20.0,
                    step=1.0,
                    key="ui_tn_risk_stop_quote",
                )
            ui_max_notional_user = max(1.0, float(ui_bank_quote) * float(ui_risk_pct) / 100.0)
            st.caption(
                f"Teto aplicado por ordem nesta sessão: **{ui_max_notional_user:.2f} quote** "
                "(banca × risco por ordem). Meta/stop monitorados pelo PnL real de fills."
            )
            ui_jsonl = st.text_input(
                "Arquivo JSONL do soak",
                value="auditoria/testnet/soak_ui_btcusdt.jsonl",
                key="ui_tn_soak_jsonl",
            )
            ui_test_name = st.text_input(
                "Nome do teste (para relatório e anexos Telegram)",
                value=str(st.session_state.get("ui_tn_test_name") or ""),
                key="ui_tn_test_name",
                placeholder="ex.: TESTE_04_1H",
            )
            ui_auto_name_jsonl = st.checkbox(
                "Gerar nome automático de arquivo com o nome do teste",
                value=True,
                key="ui_tn_auto_name_jsonl",
            )
            effective_jsonl_preview = str(ui_jsonl)
            if bool(ui_auto_name_jsonl) and str(ui_test_name).strip():
                stamp_preview = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                tname_preview = _safe_test_name(ui_test_name)
                effective_jsonl_preview = f"auditoria/testnet/{tname_preview}_{stamp_preview}.jsonl"
            st.caption(f"Arquivo desta execução: `{effective_jsonl_preview}`")
            ui_reset_jsonl_before_run = st.checkbox(
                "Limpar JSONL antes de iniciar novo soak (recomendado)",
                value=True,
                key="ui_tn_soak_reset_jsonl_before_run",
            )
            with st.expander("Higiene de logs do TestnetView", expanded=False):
                clear_with_file = st.checkbox(
                    "Também apagar o arquivo JSONL atual",
                    value=True,
                    key="ui_tn_clear_with_file",
                )
                if st.button("Limpar logs e estado desta página", key="ui_btn_tn_clear_view"):
                    if st.session_state.get("ui_tn_soak_running"):
                        st.warning("Pare o soak antes de limpar os logs do TestnetView.")
                    else:
                        st.session_state.last_testnet_soak_summary = {}
                        st.session_state.last_testnet_soak_report = {}
                        st.session_state.ui_tn_soak_exit_code = None
                        st.session_state._tn_eq_last_poll_ts = 0.0
                        st.session_state.pop("_tn_eq_last_poll_error", None)
                        st.session_state.pop("_tn_robot_pnl_cache", None)
                        st.session_state.pop("_tn_robot_pnl_last_ts", None)
                        st.session_state.pop("_tn_robot_pnl_src", None)
                        st.session_state.pop("_tn_robot_pnl_error", None)
                        st.session_state.last_testnet_account = {}
                        st.session_state.last_testnet_smoke = {}
                        sym_clear = str(st.session_state.get("ui_tn_soak_symbol") or "BTCUSDT").strip().upper()
                        rows0 = st.session_state.get("testnet_equity_rows") or []
                        st.session_state.testnet_equity_rows = [
                            r for r in rows0 if str(r.get("symbol") or "").upper() != sym_clear
                        ]
                        bkey = f"{sym_clear}_eq0"
                        bmap = st.session_state.get("testnet_equity_baseline_by_symbol") or {}
                        if bkey in bmap:
                            bmap.pop(bkey, None)
                        if clear_with_file:
                            p = Path(
                                str(
                                    st.session_state.get("ui_tn_soak_active_jsonl")
                                    or st.session_state.get("ui_tn_soak_jsonl")
                                    or ""
                                )
                            ).expanduser()
                            if p.exists():
                                try:
                                    p.unlink()
                                except Exception as e:
                                    st.error(f"Não foi possível apagar o JSONL: {e}")
                                    st.stop()
                        st.success("Logs/estado do TestnetView limpos.")
                        st.rerun()
            b1, b2, b3 = st.columns([1.1, 1.1, 1.2])
            with b1:
                if st.button(
                    "Iniciar soak agora",
                    type="primary",
                    key="ui_btn_tn_soak_run",
                    disabled=bool(st.session_state.get("ui_tn_soak_running")),
                ):
                    try:
                        if _read_running_soak_pid() is not None:
                            st.warning("Já existe um soak rodando. Pare o atual antes de iniciar outro.")
                            st.stop()
                        effective_jsonl = str(ui_jsonl)
                        if bool(ui_auto_name_jsonl) and str(ui_test_name).strip():
                            stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                            tname = _safe_test_name(ui_test_name)
                            effective_jsonl = f"auditoria/testnet/{tname}_{stamp}.jsonl"
                        if bool(ui_reset_jsonl_before_run):
                            p0 = Path(str(effective_jsonl)).expanduser()
                            if p0.exists():
                                p0.unlink()
                        st.session_state.ui_tn_soak_active_jsonl = str(effective_jsonl)
                        proc = _start_soak_process(
                            symbol=str(ui_symbol).strip().upper(),
                            interval_sec=float(ui_interval),
                            max_cycles=int(ui_cycles),
                            force_heuristic=bool(ui_force_h),
                            agent_provider=str(ui_tn_agent_provider).strip().lower(),
                            relax_liquidity=bool(ui_relax_liq),
                            max_notional_quote_per_order=float(ui_max_notional_user),
                            jsonl_out=str(effective_jsonl),
                            run_label=str(ui_test_name).strip() or None,
                        )
                        st.session_state._ui_tn_soak_proc = proc
                        st.session_state.ui_tn_soak_running = True
                        st.session_state.ui_tn_soak_exit_code = None
                        st.success(f"Soak iniciado em background. Arquivo: {effective_jsonl}")
                    except Exception as e:
                        st.error(f"Falha ao iniciar soak: {e}")
            with b2:
                if st.button(
                    "Parar teste",
                    key="ui_btn_tn_soak_stop",
                    disabled=not (
                        bool(st.session_state.get("ui_tn_soak_running"))
                        or (_read_running_soak_pid() is not None)
                    ),
                ):
                    ok, msg = _stop_soak_process(st)
                    if ok:
                        st.warning(msg)
                    else:
                        st.error(msg)
            with b3:
                st.caption(
                    f"Status atual: {'rodando' if st.session_state.get('ui_tn_soak_running') else 'parado'}"
                )

            # Monitoramento visual contínuo a partir do JSONL.
            out_path = Path(
                str(st.session_state.get("ui_tn_soak_active_jsonl") or ui_jsonl or "auditoria/testnet/soak_ui_btcusdt.jsonl")
            ).expanduser()
            st.caption(f"Arquivo monitorado agora: `{out_path}`")
            if st.session_state.get("ui_tn_soak_running"):
                _maybe_capture_testnet_equity_snapshot_ui(
                    st,
                    symbol=str(ui_symbol).strip().upper(),
                    min_sec=max(8.0, float(ui_interval)),
                )
            if out_path.exists():
                rep_live = summarize_executor_jsonl(out_path)
                if rep_live.get("ok"):
                    cyc = int(rep_live.get("rows", 0))
                    den = max(1, int(ui_cycles))
                    st.progress(min(1.0, cyc / den))
                    rob_live = _maybe_refresh_robot_pnl(
                        st,
                        symbol=str(ui_symbol).strip().upper(),
                        jsonl_path=out_path,
                        min_sec=max(8.0, float(ui_interval)),
                    )
                    pnl_live = float(rob_live.get("pnl_robot_total_quote") or 0.0)
                    st.markdown(
                        f"**Soak em execução** · ciclos: `{cyc}` · ordens postadas: `{rep_live.get('placed_orders_total', 0)}` · "
                        f"erros: `{rep_live.get('error_rows', 0)}` · PnL real do robô: `{pnl_live:+.4f}`"
                    )
                    # Gestão de risco de sessão (meta/stop) com base no PnL real de fills.
                    if st.session_state.get("ui_tn_soak_running"):
                        hit_target = float(ui_target_quote) > 0 and pnl_live >= float(ui_target_quote)
                        hit_stop = float(ui_stop_quote) > 0 and pnl_live <= -float(ui_stop_quote)
                        if hit_target or hit_stop:
                            proc = st.session_state.get("_ui_tn_soak_proc")
                            if proc is not None:
                                try:
                                    proc.terminate()
                                    if hit_target:
                                        st.success(
                                            f"Meta de lucro atingida ({pnl_live:+.4f}). Soak interrompido automaticamente."
                                        )
                                    else:
                                        st.warning(
                                            f"Stop loss atingido ({pnl_live:+.4f}). Soak interrompido automaticamente."
                                        )
                                except Exception as e:
                                    st.error(f"Falha ao aplicar stop automático: {e}")
                tail = _read_jsonl_tail(out_path, limit=20)
                if tail:
                    human_rows: list[dict[str, Any]] = []
                    cfg0 = load_config()
                    fee_bps = float((cfg0.get("backtest") or {}).get("fee_bps", 10.0))
                    for rr in tail[::-1]:
                        cand = rr.get("candidate") or {}
                        buy = (cand.get("buy") or {}).get("price")
                        sell = (cand.get("sell") or {}).get("price")
                        buy_f = float(buy) if buy not in (None, "—") else float("nan")
                        sell_f = float(sell) if sell not in (None, "—") else float("nan")
                        mid_f = (buy_f + sell_f) / 2.0 if buy_f == buy_f and sell_f == sell_f else float("nan")
                        spread_abs = sell_f - buy_f if buy_f == buy_f and sell_f == sell_f else float("nan")
                        spread_bps = (spread_abs / mid_f * 10_000.0) if mid_f and mid_f == mid_f else float("nan")
                        order_q = float(rr.get("order_size_quote_capped") or 0.0)
                        # PnL teórico de ida+volta: sem considerar fills parciais, só estrutura de spread vs taxa.
                        gross_perc = (spread_abs / mid_f) if mid_f and mid_f == mid_f else 0.0
                        fees_perc = 2.0 * (fee_bps / 10_000.0)
                        net_perc = gross_perc - fees_perc
                        pnl_theoretical = order_q * net_perc if order_q > 0 and net_perc == net_perc else float("nan")
                        src = str(rr.get("agent_source") or "").lower()
                        if "gemini" in src:
                            who = "IA (Gemini)"
                        elif "heuristic" in src or bool(ui_force_h):
                            who = "Robô heurístico"
                        else:
                            who = "Robô"
                        human_rows.append(
                            {
                                "hora_utc": rr.get("ts_utc"),
                                "status": rr.get("status"),
                                "quem_operou": who,
                                "preco_compra": buy or "—",
                                "preco_venda": sell or "—",
                                "ordens_postadas": rr.get("placed_count", 0),
                                "spread_abs": spread_abs if spread_abs == spread_abs else None,
                                "spread_bps_aprox": spread_bps if spread_bps == spread_bps else None,
                                "ordem_quote_aprox": order_q or None,
                                "pnl_teorico_quote": pnl_theoretical if pnl_theoretical == pnl_theoretical else None,
                                "motivo": (
                                    "Filtro/params bloquearam cotação"
                                    if str(rr.get("status") or "") == "no_quote_filter_or_params"
                                    else (
                                        "Bloqueado por allowlist de símbolo"
                                        if str(rr.get("status") or "") == "blocked_allowlist"
                                        else (
                                            "Bloqueado por kill switch"
                                            if str(rr.get("status") or "") == "blocked_kill_switch"
                                            else (
                                                "Cotação gerada mas sem ordem efetiva"
                                                if str(rr.get("status") or "") == "quoted_blocked_min_notional_balance"
                                                else "Cotação/execução processada"
                                            )
                                        )
                                    )
                                ),
                            }
                        )
                    st.markdown('<div class="mt-panel-title">Linha do tempo humana do soak</div>', unsafe_allow_html=True)
                    st.caption(
                        "Lucro realizado por ciclo não é fechado neste executor (ele cota/cancela ordens). "
                        "As colunas de PnL são **teóricas** (spread vs taxa) e servem para comparar a eficiência da estrutura de ordens."
                    )
                    df_h = pd.DataFrame(human_rows)
                    st.dataframe(df_h, use_container_width=True, hide_index=True)
                    csv_h = df_h.to_csv(index=False).encode("utf-8-sig")
                    st.download_button(
                        "Baixar CSV resumido do soak (ciclos + PnL teórico)",
                        data=csv_h,
                        file_name="soak_cycles_humano.csv",
                        mime="text/csv",
                        key="ui_btn_tn_soak_human_csv",
                    )
            if st.session_state.get("ui_tn_soak_exit_code") is not None:
                code = int(st.session_state.get("ui_tn_soak_exit_code"))
                if code == 0:
                    st.success("Soak finalizado com sucesso.")
                elif code == 3:
                    st.warning("Soak finalizado por kill switch.")
                else:
                    st.error(f"Soak finalizado com erro (exit code {code}).")
            if st.session_state.get("ui_tn_soak_running"):
                time.sleep(1.0)
                st.rerun()
            rep = st.session_state.get("last_testnet_soak_summary") or {}
            if rep:
                st.markdown('<div class="mt-panel-title">Resumo do último soak (UI)</div>', unsafe_allow_html=True)
                st.json(rep)

        with tab_report:
            report_path = st.text_input(
                "JSONL para relatório",
                value=str(
                    st.session_state.get("ui_tn_soak_active_jsonl")
                    or st.session_state.get("ui_tn_soak_jsonl")
                    or "auditoria/testnet/soak_ui_btcusdt.jsonl"
                ),
                key="ui_tn_soak_report_path",
            )
            r1, r2 = st.columns([1.2, 1.0])
            with r1:
                do_report = st.button("Gerar relatório com 1 clique", key="ui_btn_tn_soak_report", type="primary")
            with r2:
                do_refresh = st.button("Atualizar prévia", key="ui_btn_tn_soak_report_refresh")
            if do_report or do_refresh:
                try:
                    rep = summarize_executor_jsonl(report_path)
                    st.session_state.last_testnet_soak_report = rep
                except Exception as e:
                    st.session_state.last_testnet_soak_report = {"ok": False, "error": str(e)}
            rep2 = st.session_state.get("last_testnet_soak_report") or {}
            if rep2:
                st.markdown('<div class="mt-panel-title">Relatório do soak</div>', unsafe_allow_html=True)
                k1, k2, k3, k4, k5, k6 = st.columns(6)
                k1.metric("Ciclos", str(rep2.get("rows", "—")))
                k2.metric("Quoted total", str(rep2.get("quoted_cycles", "—")))
                k3.metric("Quoted com ordem", str(rep2.get("quoted_placed_cycles", "—")))
                k4.metric("Quoted sem execução", str(rep2.get("quoted_blocked_cycles", "—")))
                k5.metric("Erros", str(rep2.get("error_rows", "—")))
                k6.metric("Ordens postadas", str(rep2.get("placed_orders_total", "—")))
                st.caption(
                    "Quoted sem execução = ciclo apto sem ordem efetiva (ex.: min_notional/saldo)."
                )
                with st.expander("Ver JSON completo do relatório", expanded=False):
                    st.json(rep2)
                st.download_button(
                    "Baixar relatório JSON",
                    data=json.dumps(rep2, ensure_ascii=False, indent=2).encode("utf-8"),
                    file_name="soak_report.json",
                    mime="application/json",
                    key="ui_btn_tn_soak_report_download",
                )

    elif page == "Auditoria":
        st.markdown("#### Histórico de ticks (auditoria)")
        st.caption(
            "Uma linha por **tick** (manual ou vigília automática): filtros, ciclo, modo de papel e patrimônio. "
            "A auditoria desta aba é do motor em papel. Execução testnet aparece na aba **Testnet** e nos logs."
        )
        ar = st.session_state.audit_rows
        if not ar:
            st.info("Use **Rodar um tick agora** ou **Iniciar vigília** na barra lateral. A aba **Logs** guarda texto bruto.")
        else:
            df_a = pd.DataFrame(ar)
            total_ticks = int(len(df_a))
            ok_roundtrip = int((df_a["sim_volta_ok"] == "sim").sum()) if "sim_volta_ok" in df_a.columns else 0
            apt_ticks = int((df_a["apto_filtro"] == "sim").sum()) if "apto_filtro" in df_a.columns else 0
            last_patr = (
                float(df_a["carteira_patrimonio"].dropna().iloc[-1])
                if "carteira_patrimonio" in df_a.columns and not df_a["carteira_patrimonio"].dropna().empty
                else 0.0
            )
            st.markdown('<div class="mt-kpi-grid">', unsafe_allow_html=True)
            k1, k2, k3, k4 = st.columns(4)
            k1.metric("Ticks auditados", str(total_ticks))
            k2.metric("Ticks aptos", str(apt_ticks))
            k3.metric("Voltas concluídas (sim)", str(ok_roundtrip))
            k4.metric("Patrimônio final (estimado)", f"{last_patr:,.4f}")
            st.markdown("</div>", unsafe_allow_html=True)
            st.caption(
                "Use `event_id` para cruzar esta tabela com a aba Logs e entender causa técnica + efeito na carteira simulada."
            )
            st.dataframe(df_a.iloc[::-1], use_container_width=True, hide_index=True)
            st.download_button(
                "Exportar auditoria (CSV)",
                data=df_a.to_csv(index=False).encode("utf-8-sig"),
                file_name="auditoria_ticks.csv",
                mime="text/csv",
                key="dl_audit_csv",
            )
        if st.button("Limpar auditoria", key="btn_clear_audit"):
            st.session_state.audit_rows = []
            st.rerun()

    elif page == "Logs":
        st.markdown("#### Logs e leitura humana")
        st.caption(
            "Logs explicam o motivo técnico. Auditoria mostra impacto na carteira simulada. Use ambos juntos para decisão."
        )
        ar = st.session_state.get("audit_rows") or []
        if ar:
            dfa = pd.DataFrame(ar)
            ops = dfa[dfa["sim_volta_ok"] == "sim"].copy() if "sim_volta_ok" in dfa.columns else pd.DataFrame()
            if not ops.empty:
                lucro_total = float(pd.to_numeric(ops["sim_resultado_quote"], errors="coerce").fillna(0.0).sum())
                st.markdown('<div class="mt-panel-title">Resumo humano das operações</div>', unsafe_allow_html=True)
                h1, h2, h3 = st.columns(3)
                h1.metric("Teve lucro no ciclo?", "Sim" if lucro_total > 0 else "Não")
                h2.metric("Lucro acumulado (quote)", f"{lucro_total:+,.6f}")
                h3.metric("Última operação", str(ops["hora_utc"].iloc[-1]))
                ops["quem_operou"] = ops["agent_fonte"].map(_human_operator) if "agent_fonte" in ops.columns else "Robô"
                cols_show = [c for c in ["hora_utc", "quem_operou", "sim_resultado_quote", "sim_preco_compra", "sim_preco_venda", "ciclo_n"] if c in ops.columns]
                pretty = ops[cols_show].rename(
                    columns={
                        "hora_utc": "hora",
                        "sim_resultado_quote": "lucro_quote",
                        "sim_preco_compra": "preco_compra",
                        "sim_preco_venda": "preco_venda",
                        "ciclo_n": "ciclo",
                    }
                )
                st.dataframe(pretty.iloc[::-1], use_container_width=True, hide_index=True)
            else:
                st.caption("Ainda sem operação concluída na auditoria desta sessão.")
        raw = st.session_state.log_lines[-300:]
        quick = str(st.session_state.get("ui_quick_search") or "").strip().lower()
        if quick:
            raw = [ln for ln in raw if quick in str(ln).lower()]
        if not raw:
            st.code("(vazio)", language="text")
            return
        dfl = _parse_structured_logs(raw)
        if not dfl.empty:
            c1, c2, c3 = st.columns(3)
            c1.metric("Eventos logados", str(len(dfl)))
            c2.metric("Erros", str(int((dfl["nivel"] == "ERROR").sum())))
            c3.metric("Avisos", str(int((dfl["nivel"] == "WARN").sum())))
            st.dataframe(dfl.iloc[::-1], use_container_width=True, hide_index=True)
        else:
            st.caption("Formato estruturado ainda não disponível neste trecho; exibindo log bruto.")
        with st.expander("Ver log bruto", expanded=False):
            st.code("\n".join(raw), language="text")

