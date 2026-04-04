from __future__ import annotations

import time
from typing import Any

import requests

from .config_loader import load_config


class BinancePublic:
    """Somente endpoints públicos REST (sem chave)."""

    def __init__(self, base_url: str | None = None, session: requests.Session | None = None):
        cfg = load_config()
        self.base_url = (base_url or cfg["binance"]["base_url"]).rstrip("/")
        self.session = session or requests.Session()

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}{path}"
        r = self.session.get(url, params=params or {}, timeout=30)
        r.raise_for_status()
        return r.json()

    def ping(self) -> bool:
        self._get("/api/v3/ping")
        return True

    def exchange_info(self, symbol: str | None = None) -> dict[str, Any]:
        """GET /api/v3/exchangeInfo — filtros LOT_SIZE, PRICE_FILTER, MIN_NOTIONAL, etc."""
        p: dict[str, Any] = {}
        if symbol:
            p["symbol"] = symbol.upper()
        return self._get("/api/v3/exchangeInfo", p)

    def klines(
        self,
        symbol: str,
        interval: str,
        limit: int = 500,
        start_time_ms: int | None = None,
        end_time_ms: int | None = None,
    ) -> list[list]:
        p: dict[str, Any] = {"symbol": symbol.upper(), "interval": interval, "limit": limit}
        if start_time_ms is not None:
            p["startTime"] = start_time_ms
        if end_time_ms is not None:
            p["endTime"] = end_time_ms
        return self._get("/api/v3/klines", p)

    def klines_fetch_last_n(
        self,
        symbol: str,
        interval: str,
        total: int,
        end_time_ms: int | None = None,
        *,
        max_per_request: int = 1000,
        pause_sec: float = 0.0,
    ) -> list[list]:
        """
        Últimas `total` velas (ordem cronológica crescente), paginando `max_per_request` por chamada.
        A API pública limita 1000 klines por request — para `total` > 1000 são necessários vários GETs.
        """
        if total <= 0:
            return []
        cap = min(int(total), 100_000)
        end = int(end_time_ms) if end_time_ms is not None else int(time.time() * 1000)
        chunks: list[list[list]] = []
        need = cap
        while need > 0:
            lim = min(need, max_per_request)
            batch = self.klines(symbol, interval, limit=lim, end_time_ms=end)
            if not batch:
                break
            chunks.insert(0, batch)
            need -= len(batch)
            oldest_open = int(batch[0][0])
            end = oldest_open - 1
            if len(batch) < lim:
                break
            if pause_sec > 0:
                time.sleep(pause_sec)
        flat = [row for part in chunks for row in part]
        if len(flat) > cap:
            flat = flat[-cap:]
        return flat

    def klines_fetch_range(
        self,
        symbol: str,
        interval: str,
        start_time_ms: int,
        end_time_ms: int,
        *,
        max_rows: int = 20000,
        max_per_request: int = 1000,
        pause_sec: float = 0.0,
    ) -> tuple[list[list], dict[str, Any]]:
        """
        Velas com open_time em [start_time_ms, end_time_ms], ordem cronológica crescente.
        Paginação por startTime (até 1000 por GET). Respeita `max_rows` (trunca com aviso em meta).
        """
        meta: dict[str, Any] = {
            "mode": "range",
            "start_ms": int(start_time_ms),
            "end_ms": int(end_time_ms),
        }
        start = int(start_time_ms)
        end_lim = int(end_time_ms)
        if start > end_lim:
            return [], {**meta, "error": "start_after_end"}
        out: list[list] = []
        cur = start
        capped = False
        while cur <= end_lim and len(out) < max_rows:
            room = max_rows - len(out)
            lim = min(max_per_request, room)
            batch = self.klines(
                symbol, interval, limit=lim, start_time_ms=cur, end_time_ms=end_lim
            )
            if not batch:
                break
            for row in batch:
                ots = int(row[0])
                if ots < start or ots > end_lim:
                    continue
                out.append(row)
                if len(out) >= max_rows:
                    capped = True
                    break
            if capped:
                break
            last_open = int(batch[-1][0])
            if last_open < cur:
                break
            cur = last_open + 1
            if len(batch) < lim:
                break
            if pause_sec > 0:
                time.sleep(pause_sec)
        meta["rows_fetched"] = len(out)
        meta["capped_at_max_rows"] = capped
        return out, meta

    def depth(self, symbol: str, limit: int = 100) -> dict[str, Any]:
        return self._get(
            "/api/v3/depth",
            {"symbol": symbol.upper(), "limit": min(limit, 5000)},
        )

    def ticker_24h(self, symbol: str) -> dict[str, Any]:
        return self._get("/api/v3/ticker/24hr", {"symbol": symbol.upper()})

    @staticmethod
    def klines_to_df(klines: list[list]):
        import pandas as pd

        cols = [
            "open_time",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "close_time",
            "qav",
            "trades",
            "tb_base",
            "tb_quote",
            "ignore",
        ]
        df = pd.DataFrame(klines, columns=cols)
        for c in ("open", "high", "low", "close", "volume"):
            df[c] = df[c].astype(float)
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
        return df

    @staticmethod
    def depth_liquidity_quote(depth: dict[str, Any], levels: int = 20) -> float:
        """Soma notionals (quote) nos primeiros níveis bid+ask."""
        bids = depth.get("bids", [])[:levels]
        asks = depth.get("asks", [])[:levels]
        s = 0.0
        for p, q in bids:
            s += float(p) * float(q)
        for p, q in asks:
            s += float(p) * float(q)
        return s

    @staticmethod
    def server_time_ms() -> int:
        # lightweight; some users sync clocks — optional
        return int(time.time() * 1000)
