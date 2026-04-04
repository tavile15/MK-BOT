"""
Stress em lote: vários `stress_preset` sobre a mesma janela (últimas N ou intervalo UTC).

Uso (na raiz do projeto, com PYTHONPATH a incluir `src`):

  set PYTHONPATH=src
  python -m microtrans.batch_stress --symbol BTCUSDT --all-stress-presets --bars 500 --force-heuristic

  python -m microtrans.batch_stress --symbol BTCUSDT --presets baseline,taxas_altas \\
    --start-utc 2026-03-01T00:00:00+00:00 --end-utc 2026-03-08T00:00:00+00:00 --interval 5m
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .backtest import run_backtest
from .config_loader import load_config

log = logging.getLogger("filter")


def _parse_utc_to_ms(s: str) -> int:
    t = s.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(t)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _default_out_path() -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return _project_root() / "auditoria" / "backtest" / f"stress_batch_{ts}.csv"


def _preset_names(cfg: dict[str, Any], all_presets: bool, presets_csv: str | None) -> list[str]:
    block = (cfg.get("backtest") or {}).get("stress_presets") or {}
    if not isinstance(block, dict):
        return []
    if all_presets:
        return [str(k) for k in block if isinstance(block.get(k), dict)]
    if not presets_csv:
        return []
    names = [p.strip() for p in presets_csv.split(",") if p.strip()]
    unknown = [n for n in names if n not in block]
    if unknown:
        known = ", ".join(sorted(block.keys()))
        raise SystemExit(f"Presets desconhecidos: {unknown}. Definidos no YAML: {known}")
    return names


def _top_counts(d: dict[str, Any] | None, n: int = 2) -> str:
    if not d:
        return ""
    items = sorted(d.items(), key=lambda kv: (-int(kv[1]), str(kv[0])))[:n]
    return "; ".join(f"{k}:{v}" for k, v in items)


def _row(
    batch_ts: str,
    seq: int,
    res: dict[str, Any],
) -> dict[str, Any]:
    spec = res.get("replay_spec") or {}
    summ = res.get("summary") or {}
    paths = res.get("artifact_paths") or {}
    ok = bool(res.get("ok"))
    row: dict[str, Any] = {
        "batch_ts_utc": batch_ts,
        "row_seq": seq,
        "ok": ok,
        "error": res.get("error", "") if not ok else "",
        "detail": res.get("detail", "") if not ok else "",
        "bt_run_id": res.get("bt_run_id", ""),
        "symbol": res.get("symbol", spec.get("symbol", "")),
        "stress_preset": spec.get("stress_preset", ""),
        "kline_interval": spec.get("kline_interval", ""),
        "fetch_mode": spec.get("fetch_mode", ""),
        "force_agent_heuristic": spec.get("force_agent_heuristic", ""),
        "bars_fetched": res.get("bars", summ.get("bars_in_replay_df", "")),
        "replay_steps": summ.get("replay_steps", res.get("steps", "")),
        "motor_cycles": summ.get("motor_cycle_counter_final", ""),
        "pct_steps_filter_apt": summ.get("pct_steps_filter_apt", ""),
        "pnl_quote_vs_start": summ.get("pnl_quote_vs_start", ""),
        "paper_failure_reason_counts_json": json.dumps(
            summ.get("paper_failure_reason_counts") or {},
            ensure_ascii=False,
        ),
        "cycle_end_reason_counts_json": json.dumps(
            summ.get("cycle_end_reason_counts") or {},
            ensure_ascii=False,
        ),
        "paper_failure_top2": _top_counts(summ.get("paper_failure_reason_counts")),
        "cycle_end_top2": _top_counts(summ.get("cycle_end_reason_counts")),
        "audit_csv_path": paths.get("audit_csv_path", ""),
        "agent_jsonl_path": paths.get("agent_jsonl_path", ""),
    }
    rb = res.get("replay_book") or {}
    row["synthetic_book_spread_bps"] = rb.get("synthetic_book_spread_bps", "")
    row["paper_slippage_bps_applied"] = rb.get("paper_slippage_bps_applied", "")
    return row


FIELDNAMES = [
    "batch_ts_utc",
    "row_seq",
    "ok",
    "error",
    "detail",
    "bt_run_id",
    "symbol",
    "stress_preset",
    "kline_interval",
    "fetch_mode",
    "force_agent_heuristic",
    "bars_fetched",
    "replay_steps",
    "motor_cycles",
    "pct_steps_filter_apt",
    "pnl_quote_vs_start",
    "paper_failure_top2",
    "cycle_end_top2",
    "paper_failure_reason_counts_json",
    "cycle_end_reason_counts_json",
    "synthetic_book_spread_bps",
    "paper_slippage_bps_applied",
    "audit_csv_path",
    "agent_jsonl_path",
]


def run_batch(
    *,
    cfg: dict[str, Any],
    symbol: str,
    preset_names: list[str],
    kline_interval: str | None,
    bars: int | None,
    start_time_ms: int | None,
    end_time_ms: int | None,
    step_every: int,
    force_agent_heuristic: bool,
    write_audit_files: bool,
    out_path: Path,
) -> Path:
    batch_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []

    for seq, preset in enumerate(preset_names, start=1):
        log.info(
            "batch_stress | %s/%s preset=%s symbol=%s",
            seq,
            len(preset_names),
            preset,
            symbol.upper(),
        )
        res = run_backtest(
            symbol,
            cfg=cfg,
            bars=bars,
            step_every=step_every,
            kline_interval=kline_interval,
            start_time_ms=start_time_ms,
            end_time_ms=end_time_ms,
            stress_preset=preset,
            force_agent_heuristic=force_agent_heuristic,
            write_audit_files=write_audit_files,
        )
        rows.append(_row(batch_ts, seq, res))

    with open(out_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)

    log.info("batch_stress | CSV escrito: %s", out_path)
    return out_path


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    p = argparse.ArgumentParser(
        description="Corre vários stress_presets em sequência e grava um CSV comparativo.",
    )
    p.add_argument("--config", type=Path, default=None, help="YAML (default: config/default.yaml)")
    p.add_argument("--symbol", required=True, help="Ex.: BTCUSDT")
    p.add_argument("--interval", default=None, help="Intervalo de vela (default: filters.kline_interval)")
    p.add_argument("--step-every", type=int, default=1, dest="step_every")

    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument(
        "--presets",
        help="Lista separada por vírgulas (nomes em backtest.stress_presets)",
    )
    g.add_argument(
        "--all-stress-presets",
        action="store_true",
        dest="all_stress",
        help="Todos os presets definidos no YAML",
    )

    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--bars",
        type=int,
        help="Modo últimas N velas (>=30)",
    )
    mode.add_argument(
        "--start-utc",
        dest="start_utc",
        help="Início intervalo UTC (ISO-8601, ex. 2026-03-01T00:00:00+00:00)",
    )

    p.add_argument(
        "--end-utc",
        dest="end_utc",
        default=None,
        help="Fim: no modo last_n, instante final da última vela; no modo intervalo, fim da janela",
    )
    p.add_argument(
        "--force-heuristic",
        action="store_true",
        help="Força agente heurístico (sem Gemini) em todas as corridas",
    )
    p.add_argument(
        "--no-audit-files",
        action="store_true",
        help="Não gravar *_audit.csv / *_agent.jsonl em disco",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=None,
        help="CSV de saída (default: auditoria/backtest/stress_batch_UTC.csv)",
    )

    args = p.parse_args(argv)

    cfg = load_config(args.config) if args.config else load_config()

    presets = _preset_names(cfg, args.all_stress, args.presets)
    if not presets:
        print("Nenhum preset a correr.", file=sys.stderr)
        return 2

    start_ms: int | None = None
    end_ms: int | None = None
    bars: int | None = None

    if args.start_utc:
        start_ms = _parse_utc_to_ms(args.start_utc)
        end_ms = _parse_utc_to_ms(args.end_utc) if args.end_utc else None
    else:
        bars = int(args.bars)  # type: ignore[arg-type]
        if args.end_utc:
            end_ms = _parse_utc_to_ms(args.end_utc)

    out = args.out or _default_out_path()
    run_batch(
        cfg=cfg,
        symbol=args.symbol.strip().upper(),
        preset_names=presets,
        kline_interval=args.interval.strip() if args.interval else None,
        bars=bars,
        start_time_ms=start_ms,
        end_time_ms=end_ms,
        step_every=max(1, int(args.step_every)),
        force_agent_heuristic=bool(args.force_heuristic),
        write_audit_files=not bool(args.no_audit_files),
        out_path=out,
    )
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
