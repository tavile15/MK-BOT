"""
Cliente REST Binance Spot com assinatura HMAC SHA256 (endpoints SIGNED).

Uso previsto: **Spot Testnet** (`https://testnet.binance.vision`) antes de qualquer URL live.
Chaves: variáveis de ambiente `BINANCE_TESTNET_API_KEY` e `BINANCE_TESTNET_API_SECRET`
(ou passar explicitamente no construtor). No testnet use **HMAC** (“Generate HMAC_SHA256 Key”);
chaves **RSA/Ed25519** não usam este fluxo e devolvem -1022.

Referência: https://developers.binance.com/docs/binance-spot-api-docs/rest-api/request-security
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any
from urllib.parse import quote, urlencode

import requests

from .config_loader import load_config


def _is_testnet_base_url(url: str) -> bool:
    u = (url or "").lower()
    return "testnet.binance.vision" in u


def _query_string_for_sign(params: dict[str, Any]) -> str:
    items: list[tuple[str, str]] = []
    for k in sorted(params.keys()):
        v = params[k]
        if v is None:
            continue
        items.append((str(k), str(v)))
    return urlencode(items)


class BinanceSigned:
    """POST/GET assinados na API Spot (v3)."""

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        *,
        base_url: str | None = None,
        recv_window_ms: int | None = None,
        session: requests.Session | None = None,
        require_testnet: bool = True,
    ):
        cfg = load_config()
        ex = cfg.get("execution") or {}
        if base_url is None:
            base_url = str(ex.get("testnet_base_url") or "https://testnet.binance.vision")
        self.base_url = str(base_url).rstrip("/")
        if require_testnet and not _is_testnet_base_url(self.base_url):
            raise ValueError(
                "BinanceSigned: `base_url` deve ser o Spot Testnet "
                "(ex.: https://testnet.binance.vision). Para live, use require_testnet=False explicitamente."
            )
        self.api_key = api_key.strip().lstrip("\ufeff")
        self.api_secret = api_secret.strip().lstrip("\ufeff")
        if not self.api_key or not self.api_secret:
            raise ValueError("api_key e api_secret não podem ser vazios.")
        rw = recv_window_ms if recv_window_ms is not None else int(ex.get("recv_window_ms", 5000))
        self.recv_window_ms = max(1000, min(rw, 60_000))
        self.session = session or requests.Session()
        # Diferença servidor Binance − relógio local (evita -1021 / 400 por timestamp).
        self._time_offset_ms: int = 0
        self._sync_server_time()

    def _sync_server_time(self) -> None:
        try:
            r = self.session.get(f"{self.base_url}/api/v3/time", timeout=15)
            r.raise_for_status()
            server_ms = int(r.json().get("serverTime", 0))
            local_ms = int(time.time() * 1000)
            if server_ms > 0:
                self._time_offset_ms = server_ms - local_ms
        except Exception:
            self._time_offset_ms = 0

    def _now_ms(self) -> int:
        return int(time.time() * 1000) + self._time_offset_ms

    def _headers(self) -> dict[str, str]:
        return {"X-MBX-APIKEY": self.api_key}

    def _request(self, method: str, path: str, params: dict[str, Any] | None = None) -> Any:
        """
        A assinatura HMAC tem de ser calculada sobre a **mesma** query string que a API recebe.
        Passar `params=` ao requests pode alterar encoding/ordem e gerar -1022; por isso
        montamos `?recvWindow=…&timestamp=…&signature=…` manualmente.
        """
        p = {k: v for k, v in dict(params or {}).items() if v is not None}
        p["timestamp"] = self._now_ms()
        p["recvWindow"] = self.recv_window_ms
        qs = _query_string_for_sign(p)
        sig = hmac.new(
            self.api_secret.encode("utf-8"),
            qs.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        sig_q = quote(sig, safe="")
        full_query = f"{qs}&signature={sig_q}" if qs else f"signature={sig_q}"
        url = f"{self.base_url}{path}?{full_query}"
        r = self.session.request(
            method.upper(),
            url,
            headers=self._headers(),
            timeout=30,
        )
        if not r.ok:
            try:
                body = r.json()
                detail = f"code={body.get('code')} msg={body.get('msg')!r}"
            except Exception:
                detail = r.text[:500]
            raise RuntimeError(f"HTTP {r.status_code} {r.reason} — {detail}") from None
        if r.text.strip() == "":
            return {}
        return r.json()

    def account(self) -> dict[str, Any]:
        """GET /api/v3/account — saldos e permissões."""
        return self._request("GET", "/api/v3/account")

    def open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        p: dict[str, Any] = {}
        if symbol:
            p["symbol"] = symbol.upper()
        return self._request("GET", "/api/v3/openOrders", p)

    def new_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        *,
        time_in_force: str | None = None,
        quantity: str | float | None = None,
        quote_order_qty: str | float | None = None,
        price: str | float | None = None,
        new_client_order_id: str | None = None,
        new_order_resp_type: str | None = None,
    ) -> dict[str, Any]:
        """
        POST /api/v3/order

        `side`: BUY | SELL
        `order_type`: LIMIT, MARKET, ...
        LIMIT exige `quantity`, `price`, `time_in_force` (ex.: GTC).
        """
        p: dict[str, Any] = {
            "symbol": symbol.upper(),
            "side": side.upper(),
            "type": order_type.upper(),
        }
        if time_in_force:
            p["timeInForce"] = time_in_force.upper()
        if quantity is not None:
            p["quantity"] = quantity
        if quote_order_qty is not None:
            p["quoteOrderQty"] = quote_order_qty
        if price is not None:
            p["price"] = price
        if new_client_order_id:
            p["newClientOrderId"] = new_client_order_id
        if new_order_resp_type:
            p["newOrderRespType"] = new_order_resp_type
        return self._request("POST", "/api/v3/order", p)

    def cancel_order(
        self,
        symbol: str,
        *,
        order_id: int | None = None,
        orig_client_order_id: str | None = None,
    ) -> dict[str, Any]:
        p: dict[str, Any] = {"symbol": symbol.upper()}
        if order_id is not None:
            p["orderId"] = order_id
        if orig_client_order_id:
            p["origClientOrderId"] = orig_client_order_id
        return self._request("DELETE", "/api/v3/order", p)

    @staticmethod
    def from_env(*, require_testnet: bool = True) -> BinanceSigned:
        import os

        from .testnet_credentials import load_testnet_credentials_from_files

        load_testnet_credentials_from_files()
        key = os.environ.get("BINANCE_TESTNET_API_KEY", "").strip()
        sec = os.environ.get("BINANCE_TESTNET_API_SECRET", "").strip()
        if not key or not sec:
            raise OSError(
                "Defina BINANCE_TESTNET_API_KEY e BINANCE_TESTNET_API_SECRET no ambiente "
                "(ou use o construtor BinanceSigned(..., require_testnet=False) só se souber o que faz)."
            )
        return BinanceSigned(key, sec, require_testnet=require_testnet)
