from __future__ import annotations


def split_pair(symbol: str) -> tuple[str, str]:
    s = symbol.upper().strip()
    for quote in ("USDT", "USDC", "BUSD", "BTC", "ETH", "BNB"):
        if s.endswith(quote) and len(s) > len(quote):
            return s[: -len(quote)], quote
    return s, "QUOTE"
