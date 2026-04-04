"""
CLI de smoke / diagnóstico para Spot Testnet (REST assinado).

Credenciais (por ordem de prioridade):
1) Variáveis de ambiente BINANCE_TESTNET_API_KEY / BINANCE_TESTNET_API_SECRET
2) Ficheiro apontado por BINANCE_TESTNET_ENV_FILE ou MICROTRANS_BINANCE_TESTNET_ENV_FILE
3) Ficheiro ".env.testnet" na raiz do projeto ou "config/.env.testnet"

  copy .env.testnet.example .env.testnet
  set PYTHONPATH=src
  python -m microtrans.binance_testnet_cli account
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path

from .binance_public import BinancePublic
from .binance_signed import BinanceSigned
from .config_loader import load_config
from .telegram_notify import TelegramConfig, send_document, send_photo, send_text_message
from .testnet_robot_pnl import compute_robot_pnl_from_real_fills
from .testnet_credentials import load_testnet_credentials_from_files, project_root_from_package
from .testnet_executor import run_executor_iter, run_executor_once
from .testnet_order_smoke import run_limit_buy_place_and_cancel
from .testnet_soak_report import summarize_executor_jsonl


_STOP_REQUESTED = False


def _project_root() -> Path:
    return project_root_from_package()


def _make_signed_client() -> BinanceSigned:
    return BinanceSigned.from_env(require_testnet=True)


def _client_public_testnet() -> BinancePublic:
    cfg = load_config()
    ex = cfg.get("execution") or {}
    base = str(ex.get("testnet_base_url") or "https://testnet.binance.vision")
    return BinancePublic(base_url=base)


def cmd_account(_: argparse.Namespace) -> int:
    c = _make_signed_client()
    acc = c.account()
    # Resumo legível (sem despejar tudo)
    bals = acc.get("balances") or []
    non_zero = [b for b in bals if float(b.get("free", 0) or 0) > 0 or float(b.get("locked", 0) or 0) > 0]
    print(json.dumps({"makerCommission": acc.get("makerCommission"), "takerCommission": acc.get("takerCommission"), "canTrade": acc.get("canTrade"), "balances_non_zero": non_zero[:40], "balances_total": len(bals)}, indent=2, ensure_ascii=False))
    return 0


def cmd_open_orders(args: argparse.Namespace) -> int:
    c = _make_signed_client()
    oo = c.open_orders(args.symbol)
    print(json.dumps(oo, indent=2, ensure_ascii=False))
    return 0


def cmd_ping(_: argparse.Namespace) -> int:
    p = _client_public_testnet()
    p.ping()
    print("testnet public ping: ok")
    return 0


def cmd_book(args: argparse.Namespace) -> int:
    p = _client_public_testnet()
    d = p.depth(args.symbol, limit=min(args.limit, 100))
    print(json.dumps({"lastUpdateId": d.get("lastUpdateId"), "bids_head": (d.get("bids") or [])[:5], "asks_head": (d.get("asks") or [])[:5]}, indent=2, ensure_ascii=False))
    return 0


def _mask_secret(s: str) -> str:
    if not s.strip():
        return "(vazio)"
    t = s.strip()
    return f"comprimento={len(t)} início={t[:4]}…"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _signal_mark_stop(_: int, __) -> None:
    global _STOP_REQUESTED
    _STOP_REQUESTED = True
    raise KeyboardInterrupt()


def _fmt_num(v: float, nd: int = 4) -> str:
    return f"{float(v):+,.{nd}f}"


def _safe_name(s: str) -> str:
    t = re.sub(r"[^A-Za-z0-9_-]+", "_", str(s or "").strip())
    t = t.strip("._-")
    return (t[:80] or "").lower()


def _resolve_telegram_image_path(cfg: TelegramConfig) -> Path | None:
    raw = str(cfg.header_image or "").strip()
    candidates: list[Path] = []
    if raw:
        p = Path(raw).expanduser()
        candidates.append(p)
        if not p.is_absolute():
            candidates.append(_project_root() / p)
    # Fallback amigável para a arte padrão do projeto.
    candidates.append(_project_root() / "indentidade_visual" / "Gemini_Generated_Image_nvrl3lnvrl3lnvrl.png")
    for p in candidates:
        if p.exists() and p.is_file():
            return p
    return None


def _build_telegram_end_message(
    *,
    summary: dict,
    report: dict | None,
    pnl: dict | None,
) -> str:
    rep = report or {}
    pnl0 = pnl or {}
    symbol = str(summary.get("symbol") or rep.get("last_cycle", {}).get("symbol") or "—")
    rows = int(rep.get("rows", summary.get("cycles", 0)) or 0)
    placed = int(rep.get("placed_orders_total", summary.get("placed_orders_total", 0)) or 0)
    buys = int(rep.get("buy_orders_total", 0) or 0)
    sells = int(rep.get("sell_orders_total", 0) or 0)
    quoted = int(rep.get("quoted_cycles", summary.get("quoted_cycles", 0)) or 0)
    no_quote = int(rep.get("no_quote_cycles", 0) or 0)
    errs = int(rep.get("error_rows", summary.get("error_rows", 0)) or 0)
    status_txt = "OK" if bool(summary.get("ok")) else "COM ALERTAS"
    reason = str(summary.get("end_reason") or "finished")
    run_label = str(summary.get("run_label") or "").strip()
    lines: list[str] = [
        "✅ Execução do soak encerrada",
        "━━━━━━━━━━━━━━━━━━",
        f"🧪 Teste: {run_label}" if run_label else "🧪 Teste: (sem nome)",
        f"🪙 Par: {symbol}",
        f"📌 Status: {status_txt}",
        f"🧭 Motivo: {reason}",
        f"🔁 Ciclos: {rows}",
        f"💬 Cotação: {quoted} | Sem cotação: {no_quote}",
        f"📦 Ordens postadas: {placed} (BUY={buys} / SELL={sells})",
        f"⚠️ Erros: {errs}",
    ]
    if pnl0.get("ok"):
        pnl_q = float(pnl0.get("pnl_robot_total_quote") or 0.0)
        qa = str(pnl0.get("quote_asset") or "quote")
        fills = int(pnl0.get("fills_count") or 0)
        lines.append(f"💰 PnL real (fills): {_fmt_num(pnl_q)} {qa} | fills={fills}")
    else:
        lines.append("💰 PnL real (fills): n/d")
    lines.append("━━━━━━━━━━━━━━━━━━")
    return "\n".join(lines)


def _build_cycles_csv_from_jsonl(*, jsonl_path: Path, csv_path: Path) -> Path:
    rows: list[dict[str, Any]] = []
    if jsonl_path.exists():
        for ln in jsonl_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            s = ln.strip()
            if not s:
                continue
            try:
                r = json.loads(s)
            except Exception:
                continue
            cand = r.get("candidate") or {}
            buy = cand.get("buy") or {}
            sell = cand.get("sell") or {}
            rows.append(
                {
                    "ts_utc": r.get("ts_utc"),
                    "loop_cycle": r.get("loop_cycle"),
                    "symbol": r.get("symbol"),
                    "status": r.get("status"),
                    "ok": bool(r.get("ok")),
                    "placed_count": int(r.get("placed_count") or 0),
                    "cancelled_count": int(r.get("cancelled_count") or 0),
                    "agent_source": r.get("agent_source"),
                    "spread_bps": r.get("spread_bps"),
                    "order_size_quote_capped": r.get("order_size_quote_capped"),
                    "buy_price": buy.get("price"),
                    "sell_price": sell.get("price"),
                    "buy_block_reason": buy.get("block_reason"),
                    "sell_block_reason": sell.get("block_reason"),
                }
            )
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    cols = [
        "ts_utc",
        "loop_cycle",
        "symbol",
        "status",
        "ok",
        "placed_count",
        "cancelled_count",
        "agent_source",
        "spread_bps",
        "order_size_quote_capped",
        "buy_price",
        "sell_price",
        "buy_block_reason",
        "sell_block_reason",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for row in rows:
            w.writerow(row)
    return csv_path


def _write_report_json(*, path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def cmd_order_smoke(args: argparse.Namespace) -> int:
    pub = _client_public_testnet()
    signed = _make_signed_client()
    out = run_limit_buy_place_and_cancel(
        args.symbol,
        pub=pub,
        signed=signed,
        ticks_below_best_bid=int(args.ticks_below),
    )
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0 if out.get("ok") else 1


def cmd_doctor(_: argparse.Namespace) -> int:
    root = _project_root()
    f_root = root / ".env.testnet"
    f_cfg = root / "config" / ".env.testnet"
    load_testnet_credentials_from_files()
    key = os.environ.get("BINANCE_TESTNET_API_KEY", "")
    sec = os.environ.get("BINANCE_TESTNET_API_SECRET", "")
    print(
        json.dumps(
            {
                "project_root": str(root),
                "ficheiro_esperado": str(f_root),
                "existe_na_raiz": f_root.is_file(),
                "existe_em_config": f_cfg.is_file(),
                "BINANCE_TESTNET_API_KEY": _mask_secret(key),
                "BINANCE_TESTNET_API_SECRET": _mask_secret(sec),
                "nota": "Não use o caminho de exemplo F:\\caminho\\para\\.env.testnet — use o caminho real ou corra sem --env-file se o ficheiro estiver na raiz do projeto.",
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


def cmd_executor_once(args: argparse.Namespace) -> int:
    pub = _client_public_testnet()
    signed = _make_signed_client()
    cfg = load_config()
    cfg["execution"] = dict(cfg.get("execution") or {})
    cfg["execution"]["testnet_relax_liquidity_filter"] = bool(args.relax_liquidity)
    if args.max_notional_quote_per_order is not None:
        cfg["execution"]["max_notional_quote_per_order"] = float(args.max_notional_quote_per_order)
    if getattr(args, "agent_provider", None):
        cfg["agent"] = dict(cfg.get("agent") or {})
        cfg["agent"]["provider"] = str(args.agent_provider).strip().lower()
    out = run_executor_once(
        symbol=args.symbol,
        pub=pub,
        signed=signed,
        cfg=cfg,
        force_heuristic=bool(args.force_heuristic),
    )
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0 if out.get("ok") else 1


def cmd_executor_loop(args: argparse.Namespace) -> int:
    global _STOP_REQUESTED
    _STOP_REQUESTED = False
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _signal_mark_stop)
        except Exception:
            pass
    pub = _client_public_testnet()
    signed = _make_signed_client()
    cfg = load_config()
    cfg["execution"] = dict(cfg.get("execution") or {})
    cfg["execution"]["testnet_relax_liquidity_filter"] = bool(args.relax_liquidity)
    if args.max_notional_quote_per_order is not None:
        cfg["execution"]["max_notional_quote_per_order"] = float(args.max_notional_quote_per_order)
    if getattr(args, "agent_provider", None):
        cfg["agent"] = dict(cfg.get("agent") or {})
        cfg["agent"]["provider"] = str(args.agent_provider).strip().lower()
    out_path = Path(args.jsonl_out).expanduser() if args.jsonl_out else None
    run_label = str(getattr(args, "run_label", "") or "").strip()
    stats: dict[str, int] = {
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
    end_reason = "max_cycles_reached" if int(args.max_cycles) > 0 else "manual_or_signal_stop"
    try:
        for step in run_executor_iter(
            symbol=args.symbol,
            pub=pub,
            signed=signed,
            interval_sec=float(args.interval_sec),
            max_cycles=int(args.max_cycles),
            cfg=cfg,
            force_heuristic=bool(args.force_heuristic),
        ):
            row = dict(step)
            row["ts_utc"] = _utc_now_iso()
            if out_path is not None:
                _append_jsonl(out_path, row)
            if bool(args.print_each):
                print(json.dumps(row, ensure_ascii=False))
            stats["cycles"] += 1
            if bool(step.get("ok")):
                stats["ok_rows"] += 1
            else:
                stats["error_rows"] += 1
            st = str(step.get("status") or "unknown")
            status_counts[st] = status_counts.get(st, 0) + 1
            if st.startswith("quoted"):
                stats["quoted_cycles"] += 1
                if st == "quoted_placed" or st == "quoted":
                    stats["quoted_placed_cycles"] += 1
                elif st == "quoted_blocked_min_notional_balance":
                    stats["quoted_blocked_cycles"] += 1
            if st == "blocked_kill_switch":
                stats["blocked_kill_switch_cycles"] += 1
            stats["placed_orders_total"] += int(step.get("placed_count") or 0)
            stats["cancelled_orders_total"] += int(step.get("cancelled_count") or 0)
            if _STOP_REQUESTED:
                end_reason = "manual_or_signal_stop"
                break
    except KeyboardInterrupt:
        end_reason = "manual_or_signal_stop"
    if stats["blocked_kill_switch_cycles"] > 0:
        end_reason = "kill_switch"
    elif int(args.max_cycles) > 0 and stats["cycles"] >= int(args.max_cycles) and not _STOP_REQUESTED:
        end_reason = "max_cycles_reached"

    summary = {
        "ok": stats["error_rows"] == 0,
        "symbol": str(args.symbol).upper(),
        "interval_sec": float(args.interval_sec),
        "max_cycles": int(args.max_cycles),
        "force_heuristic": bool(args.force_heuristic),
        "relax_liquidity": bool(args.relax_liquidity),
        "run_label": run_label or None,
        "jsonl_out": str(out_path) if out_path is not None else None,
        "end_reason": end_reason,
        **stats,
        "status_counts": status_counts,
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    tg_cfg = TelegramConfig.from_env()
    if tg_cfg.can_send():
        rep = summarize_executor_jsonl(out_path) if out_path is not None else None
        pnl = None
        if out_path is not None and rep and rep.get("ok"):
            try:
                pnl = compute_robot_pnl_from_real_fills(
                    symbol=str(args.symbol).upper(),
                    jsonl_path=out_path,
                    pub=pub,
                    signed=signed,
                )
            except Exception as e:
                pnl = {"ok": False, "error": str(e)}
        msg = _build_telegram_end_message(summary=summary, report=rep, pnl=pnl)
        img_out: dict[str, Any] | None = None
        image_path = _resolve_telegram_image_path(tg_cfg)
        if image_path is not None:
            img_out = send_photo(
                file_path=image_path,
                caption=(
                    f"📸 Início do relatório · {run_label}"
                    if run_label
                    else f"📸 Início do relatório · {summary.get('symbol')}"
                ),
                cfg=tg_cfg,
            )
        tg_out = send_text_message(text=msg, cfg=tg_cfg)
        tg_files_out: list[dict[str, Any]] = []
        if tg_cfg.send_report_files and out_path is not None and out_path.exists():
            try:
                base_stem = _safe_name(run_label) or out_path.stem
                report_json_path = out_path.with_name(f"{base_stem}_report.json")
                report_csv_path = out_path.with_name(f"{base_stem}_cycles.csv")
                payload = {
                    "summary": summary,
                    "soak_report": rep,
                    "pnl_real_fills": pnl,
                }
                _write_report_json(path=report_json_path, payload=payload)
                _build_cycles_csv_from_jsonl(jsonl_path=out_path, csv_path=report_csv_path)
                tg_files_out.append(
                    {
                        "report_json": send_document(
                            file_path=report_json_path,
                            caption=(
                                f"Soak encerrado ({run_label}): relatório JSON"
                                if run_label
                                else f"Soak encerrado: relatório JSON ({summary.get('symbol')})"
                            ),
                            cfg=tg_cfg,
                        )
                    }
                )
                tg_files_out.append(
                    {
                        "cycles_csv": send_document(
                            file_path=report_csv_path,
                            caption=(
                                f"Soak encerrado ({run_label}): ciclos CSV"
                                if run_label
                                else f"Soak encerrado: ciclos CSV ({summary.get('symbol')})"
                            ),
                            cfg=tg_cfg,
                        )
                    }
                )
            except Exception as e:
                tg_files_out.append({"files_error": str(e)})
        if bool(args.print_each):
            print(
                json.dumps(
                    {"telegram_image": img_out, "telegram_notify": tg_out, "telegram_files": tg_files_out},
                    ensure_ascii=False,
                )
            )
    if stats["blocked_kill_switch_cycles"] > 0:
        return 3
    return 0 if stats["error_rows"] == 0 else 1


def cmd_executor_report(args: argparse.Namespace) -> int:
    rep = summarize_executor_jsonl(args.input_jsonl)
    print(json.dumps(rep, indent=2, ensure_ascii=False))
    return 0 if rep.get("ok") else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Binance Spot Testnet — utilitários REST (público + assinado).")
    p.add_argument(
        "--env-file",
        default=None,
        help='Ficheiro KEY=VALUE (ex.: ".env.testnet" ou caminho completo). NÃO use o texto fictício "F:\\caminho\\para\\...".',
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", help="Mostra raiz do projeto, se .env.testnet existe e se as chaves foram carregadas (sem revelar segredos)")

    sub.add_parser("ping", help="GET público /api/v3/ping no testnet")

    sub.add_parser("account", help="GET assinado /api/v3/account (requer env com keys)")

    po = sub.add_parser("open-orders", help="Ordens abertas (opcional símbolo)")
    po.add_argument("--symbol", default=None, help="Ex.: BTCUSDT")

    pb = sub.add_parser("book", help="Livro público testnet (top N níveis)")
    pb.add_argument("--symbol", default="BTCUSDT")
    pb.add_argument("--limit", type=int, default=20)

    osm = sub.add_parser(
        "order-smoke",
        help="P4t: BUY LIMIT abaixo do bid + cancel (valida new_order/cancel na testnet)",
    )
    osm.add_argument("--symbol", default="BTCUSDT", help="Par spot testnet, ex. BTCUSDT")
    osm.add_argument(
        "--ticks-below",
        type=int,
        default=20,
        dest="ticks_below",
        help="Nº de ticks (tickSize) abaixo do melhor bid (default 20)",
    )

    ex1 = sub.add_parser(
        "executor-once",
        help="P4t-4: roda 1 ciclo do executor (motor -> cancel/post LIMIT maker com limites)",
    )
    ex1.add_argument("--symbol", default="BTCUSDT", help="Par spot testnet")
    ex1.add_argument(
        "--force-heuristic",
        action="store_true",
        help="Força agente heurístico no ciclo (evita custo de API Gemini)",
    )
    ex1.add_argument(
        "--relax-liquidity",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Relaxa filtro de liquidez só para validação de execução testnet.",
    )
    ex1.add_argument(
        "--max-notional-quote-per-order",
        type=float,
        default=None,
        help="Override do teto de notional por ordem (quote), para gestão de risco da sessão.",
    )
    ex1.add_argument(
        "--agent-provider",
        choices=["heuristic", "gemini"],
        default=None,
        help="Seleciona provedor do agente para este ciclo (default: YAML).",
    )

    exl = sub.add_parser(
        "executor-loop",
        help="P4t-5: loop contínuo do executor na testnet (interrompa com Ctrl+C)",
    )
    exl.add_argument("--symbol", default="BTCUSDT", help="Par spot testnet")
    exl.add_argument("--interval-sec", type=float, default=15.0, dest="interval_sec", help="Pausa entre ciclos")
    exl.add_argument(
        "--max-cycles",
        type=int,
        default=0,
        help="0 = infinito; >0 para soak curto controlado",
    )
    exl.add_argument(
        "--force-heuristic",
        action="store_true",
        help="Força agente heurístico no ciclo (evita custo de API Gemini)",
    )
    exl.add_argument(
        "--relax-liquidity",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Relaxa filtro de liquidez só para validação de execução testnet.",
    )
    exl.add_argument(
        "--max-notional-quote-per-order",
        type=float,
        default=None,
        help="Override do teto de notional por ordem (quote), para gestão de risco da sessão.",
    )
    exl.add_argument(
        "--agent-provider",
        choices=["heuristic", "gemini"],
        default=None,
        help="Seleciona provedor do agente para esta sessão (default: YAML).",
    )
    exl.add_argument(
        "--jsonl-out",
        default="auditoria/testnet/executor_loop.jsonl",
        help="Ficheiro JSONL para registar 1 linha por ciclo (soak).",
    )
    exl.add_argument(
        "--print-each",
        action="store_true",
        help="Imprime cada ciclo no stdout além do resumo final.",
    )
    exl.add_argument(
        "--run-label",
        default="",
        help="Nome amigável do teste (usado na mensagem e nos nomes de anexos).",
    )

    exr = sub.add_parser(
        "executor-report",
        help="P4t-6: sumariza JSONL de soak do executor (ordens, estados, erros).",
    )
    exr.add_argument(
        "--input-jsonl",
        default="auditoria/testnet/executor_loop.jsonl",
        help="Ficheiro JSONL produzido por executor-loop.",
    )

    args = p.parse_args(argv)
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                try:
                    stream.reconfigure(encoding="utf-8")
                except Exception:
                    pass
    if getattr(args, "env_file", None):
        os.environ["BINANCE_TESTNET_ENV_FILE"] = str(Path(args.env_file).expanduser())
    try:
        if args.cmd == "ping":
            return cmd_ping(args)
        if args.cmd == "account":
            return cmd_account(args)
        if args.cmd == "open-orders":
            return cmd_open_orders(args)
        if args.cmd == "book":
            return cmd_book(args)
        if args.cmd == "doctor":
            return cmd_doctor(args)
        if args.cmd == "order-smoke":
            return cmd_order_smoke(args)
        if args.cmd == "executor-once":
            return cmd_executor_once(args)
        if args.cmd == "executor-loop":
            return cmd_executor_loop(args)
        if args.cmd == "executor-report":
            return cmd_executor_report(args)
    except OSError as e:
        msg = str(e)
        if "BINANCE_TESTNET_API" in msg:
            root = _project_root()
            msg += (
                f"\n\nCrie {root / '.env.testnet'} (veja .env.testnet.example) "
                "ou defina as variáveis nesta sessão PowerShell:\n"
                '  $env:BINANCE_TESTNET_API_KEY = "..."\n'
                '  $env:BINANCE_TESTNET_API_SECRET = "..."\n\n'
                "Diagnóstico: python -m microtrans.binance_testnet_cli doctor\n"
                "Se usou --env-file, o caminho tem de ser REAL (ex.: .\\.env.testnet), não F:\\\\caminho\\\\para\\\\..."
            )
        print(msg, file=sys.stderr)
        return 2
    except Exception as e:
        print(f"Erro: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrompido pelo utilizador.", file=sys.stderr)
        return 130
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
