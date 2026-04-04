from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def summarize_executor_jsonl(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {"ok": False, "error": "file_not_found", "path": str(p)}
    total = 0
    quoted = 0
    quoted_placed = 0
    quoted_blocked = 0
    blocked_kill = 0
    blocked_allow = 0
    no_quote = 0
    errors = 0
    placed_total = 0
    cancelled_total = 0
    buy_orders_total = 0
    sell_orders_total = 0
    first_ts_utc = ""
    last_ts_utc = ""
    last: dict[str, Any] = {}
    status_counts: dict[str, int] = {}
    for ln in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        s = ln.strip()
        if not s:
            continue
        try:
            row = json.loads(s)
        except Exception:
            continue
        total += 1
        st = str(row.get("status") or "unknown")
        status_counts[st] = status_counts.get(st, 0) + 1
        if st.startswith("quoted"):
            quoted += 1
            if st == "quoted_placed":
                quoted_placed += 1
            elif st == "quoted_blocked_min_notional_balance":
                quoted_blocked += 1
            elif st == "quoted":
                # Compatibilidade com JSONL antigos.
                quoted_placed += 1
        elif st == "blocked_kill_switch":
            blocked_kill += 1
        elif st == "blocked_allowlist":
            blocked_allow += 1
        elif st == "no_quote_filter_or_params":
            no_quote += 1
        if not bool(row.get("ok", False)):
            errors += 1
        placed_total += int(row.get("placed_count") or 0)
        cancelled_total += int(row.get("cancelled_count") or 0)
        if not first_ts_utc:
            first_ts_utc = str(row.get("ts_utc") or "")
        last_ts_utc = str(row.get("ts_utc") or "") or last_ts_utc
        for od in row.get("placed") or []:
            side = str(od.get("side") or "").upper()
            if side == "BUY":
                buy_orders_total += 1
            elif side == "SELL":
                sell_orders_total += 1
        last = row

    return {
        "ok": True,
        "path": str(p),
        "rows": total,
        "quoted_cycles": quoted,
        "quoted_placed_cycles": quoted_placed,
        "quoted_blocked_cycles": quoted_blocked,
        "blocked_kill_switch_cycles": blocked_kill,
        "blocked_allowlist_cycles": blocked_allow,
        "no_quote_cycles": no_quote,
        "error_rows": errors,
        "placed_orders_total": placed_total,
        "cancelled_orders_total": cancelled_total,
        "buy_orders_total": buy_orders_total,
        "sell_orders_total": sell_orders_total,
        "first_ts_utc": first_ts_utc or None,
        "last_ts_utc": last_ts_utc or None,
        "status_counts": status_counts,
        "last_cycle": {
            "loop_cycle": last.get("loop_cycle"),
            "status": last.get("status"),
            "placed_count": last.get("placed_count"),
            "cancelled_count": last.get("cancelled_count"),
            "symbol": last.get("symbol"),
        },
    }

