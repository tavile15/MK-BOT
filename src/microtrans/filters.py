from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from .binance_public import BinancePublic
from .config_loader import load_config


@dataclass
class FilterResult:
    apt: bool
    reasons: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, float] = field(default_factory=dict)


def _true_range(high: np.ndarray, low: np.ndarray, close_prev: np.ndarray) -> np.ndarray:
    return np.maximum(
        high - low,
        np.maximum(np.abs(high - close_prev), np.abs(low - close_prev)),
    )


def _atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> pd.Series:
    h = high.to_numpy()
    l = low.to_numpy()
    c = close.to_numpy()
    prev = np.roll(c, 1)
    prev[0] = c[0]
    tr = _true_range(h, l, prev)
    s = pd.Series(tr, index=close.index)
    return s.ewm(alpha=1 / period, adjust=False).mean()


def _adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> pd.Series:
    """ADX simplificado (Wilder) em cima de +DM/-DM."""
    up = high.diff()
    down = -low.diff()
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    tr = _true_range(high.to_numpy(), low.to_numpy(), close.shift(1).to_numpy())
    tr_s = pd.Series(tr, index=close.index).ewm(alpha=1 / period, adjust=False).mean()
    plus_di = 100 * (
        pd.Series(plus_dm, index=close.index).ewm(alpha=1 / period, adjust=False).mean() / tr_s
    )
    minus_di = 100 * (
        pd.Series(minus_dm, index=close.index).ewm(alpha=1 / period, adjust=False).mean() / tr_s
    )
    dx = 100 * np.abs(plus_di - minus_di) / (plus_di + minus_di + 1e-12)
    return dx.ewm(alpha=1 / period, adjust=False).mean()


def format_filter_human(
    symbol: str,
    apt: bool,
    metrics: dict[str, float],
    reasons: dict[str, Any],
    fcfg: dict[str, Any],
) -> str:
    """Texto em português para leitura rápida (logs e UI)."""
    min_l = float(fcfg["min_liquidity_quote_usd"])
    max_l = float(fcfg["max_liquidity_quote_usd"])
    rmin = float(fcfg["range_atr_ratio_min"])
    rmax = float(fcfg["range_atr_ratio_max"])
    adx_max = float(fcfg["adx_max"])
    apmin = float(fcfg["atr_pct_min"])
    apmax = float(fcfg["atr_pct_max"])

    liq = float(metrics.get("liquidity_quote_sum", 0))
    atrp = float(metrics.get("atr_pct", 0))
    ratr = float(metrics.get("range_atr_ratio", 0))
    adx = float(metrics.get("adx", 0))
    last = float(metrics.get("last_price", 0))
    qv = float(metrics.get("quote_volume_24h", -1))
    relax = bool(fcfg.get("relax_liquidity_filter", False))
    ratio_txt = ""
    if qv > 0:
        ratio_txt = f" | book_top/vol24h = {100.0 * liq / qv:.4f}%"

    if relax:
        liq_gate = True
        liq_block = [
            "1) Liquidez no book: MODO RELAXADO (não aplica teto; majors deixam de ser barrados só por isso)",
            f"    medido (informativo): {liq:,.0f} quote{ratio_txt}",
        ]
    else:
        liq_gate = min_l <= liq <= max_l
        liq_block = [
            "1) Liquidez no book (soma bid+ask nos primeiros níveis, proxy em quote):",
            f"    medido: {liq:,.0f}  |  faixa: {min_l:,.0f} … {max_l:,.0f}  |  {'OK' if liq_gate else 'FORA'}",
        ]
    ratr_gate = rmin <= ratr <= rmax
    adx_gate = adx <= adx_max
    atr_gate = apmin <= atrp <= apmax

    blo = [
        f"=== Avaliação de mercado: {symbol} ===",
        f"Veredito: {'APTO — pode acionar o agente/motor' if apt else 'NÃO APTO — motor desligado ou deve encerrar ciclo'}",
        "",
        *liq_block,
        "",
        "2) Regime lateral — razão (amplitude recente / ATR):",
        f"    medido: {ratr:.2f}  |  permitido: {rmin:.2f} … {rmax:.2f}  |  {'OK' if ratr_gate else 'FORA'}",
        "    (valores altos: amplitude grande versus ruído local)",
        "",
        "3) Força de tendência — ADX (maior = mais direcional):",
        f"    medido: {adx:.1f}  |  teto: <= {adx_max:.1f}  |  {'OK' if adx_gate else 'FORA'}",
        "",
        "4) Volatilidade — ATR em % do preço:",
        f"    medido: {atrp:.3f}%  |  faixa: {apmin:.3f}% … {apmax:.3f}%  |  {'OK' if atr_gate else 'FORA'}",
        "",
        f"Referência: último preço ~ {last:,.2f}",
    ]
    if qv >= 0:
        blo.append(f"Volume 24h (quote, API): {qv:,.0f}")
    if not apt and reasons:
        blo.append("")
        blo.append("Motivos do bloqueio:")
        for k, v in reasons.items():
            blo.append(f"  - {k}: {v}")
    blo.append("===")
    return "\n".join(blo)


def diagnostic_table(fr: FilterResult, fcfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Linhas para exibir em tabela (Streamlit / validação humana)."""
    m = fr.metrics
    min_l = float(fcfg["min_liquidity_quote_usd"])
    max_l = float(fcfg["max_liquidity_quote_usd"])
    rmin = float(fcfg["range_atr_ratio_min"])
    rmax = float(fcfg["range_atr_ratio_max"])
    adx_max = float(fcfg["adx_max"])
    apmin = float(fcfg["atr_pct_min"])
    apmax = float(fcfg["atr_pct_max"])

    liq = float(m.get("liquidity_quote_sum", 0))
    ratr = float(m.get("range_atr_ratio", 0))
    adx = float(m.get("adx", 0))
    atrp = float(m.get("atr_pct", 0))

    def ok_gate(cond: bool) -> str:
        return "Sim" if cond else "Não"

    relax = bool(fcfg.get("relax_liquidity_filter", False))
    liq_row: dict[str, Any] = (
        {
            "Critério": "Liquidez (book)",
            "Medido": f"{liq:,.0f}",
            "Regra": "Relaxada (majors / teste)",
            "Passa": "Sim",
        }
        if relax
        else {
            "Critério": "Liquidez (book)",
            "Medido": f"{liq:,.0f}",
            "Regra": f"{min_l:,.0f} – {max_l:,.0f}",
            "Passa": ok_gate(min_l <= liq <= max_l),
        }
    )

    return [
        liq_row,
        {
            "Critério": "Lateralidade (range/ATR)",
            "Medido": f"{ratr:.2f}",
            "Regra": f"{rmin:.2f} – {rmax:.2f}",
            "Passa": ok_gate(rmin <= ratr <= rmax),
        },
        {
            "Critério": "Tendência (ADX)",
            "Medido": f"{adx:.1f}",
            "Regra": f"≤ {adx_max:.1f}",
            "Passa": ok_gate(adx <= adx_max),
        },
        {
            "Critério": "Volatilidade (ATR %)",
            "Medido": f"{atrp:.3f}%",
            "Regra": f"{apmin:.3f}% – {apmax:.3f}%",
            "Passa": ok_gate(apmin <= atrp <= apmax),
        },
    ]


def evaluate_market(
    symbol: str,
    df: pd.DataFrame,
    depth: dict[str, Any],
    ticker_24h: dict[str, Any] | None,
    cfg: dict[str, Any] | None = None,
    logger: logging.Logger | None = None,
    silent: bool = False,
) -> FilterResult:
    """
    Filtros matemáticos:
    - liquidez (book) em faixa "baixa/média" (proxy)
    - regime lateral (range vs ATR, ADX)
    - volatilidade em faixa operável
    """
    cfg = cfg or load_config()
    fcfg = cfg["filters"]
    log = logger or logging.getLogger("filter")

    levels = min(fcfg.get("depth_limit", 100), 20)
    liq = BinancePublic.depth_liquidity_quote(depth, levels=levels)

    high = df["high"]
    low = df["low"]
    close = df["close"]
    last = float(close.iloc[-1])

    atr_period = int(fcfg["atr_period"])
    adx_period = int(fcfg["adx_period"])
    atr_s = _atr(high, low, close, atr_period)
    atr_last = float(atr_s.iloc[-1])
    atr_pct = 100.0 * atr_last / last if last else 0.0

    lookback = min(48, len(df) - 1)
    recent_high = float(high.iloc[-lookback:].max())
    recent_low = float(low.iloc[-lookback:].min())
    price_range = recent_high - recent_low
    range_atr_ratio = price_range / (atr_last + 1e-12)

    adx_s = _adx(high, low, close, adx_period)
    adx_last = float(adx_s.iloc[-1])
    if adx_last != adx_last:  # NaN
        adx_last = 99.0

    quote_vol = float(ticker_24h["quoteVolume"]) if ticker_24h else -1.0
    relax = bool(fcfg.get("relax_liquidity_filter", False))
    book_vol_ratio = (liq / quote_vol) if quote_vol > 0 else -1.0

    metrics = {
        "liquidity_quote_sum": liq,
        "atr_pct": atr_pct,
        "range_atr_ratio": range_atr_ratio,
        "adx": adx_last,
        "quote_volume_24h": quote_vol,
        "last_price": last,
        "book_to_volume_24h_ratio": book_vol_ratio,
    }

    reasons: dict[str, Any] = {}
    ok = True

    min_l = float(fcfg["min_liquidity_quote_usd"])
    max_l = float(fcfg["max_liquidity_quote_usd"])
    if not relax:
        if liq < min_l:
            ok = False
            reasons["liquidity_too_low"] = f"{liq:.0f} < {min_l}"
        elif liq > max_l:
            ok = False
            reasons["liquidity_too_high"] = f"{liq:.0f} > {max_l}"

    rmin = float(fcfg["range_atr_ratio_min"])
    rmax = float(fcfg["range_atr_ratio_max"])
    if range_atr_ratio < rmin or range_atr_ratio > rmax:
        ok = False
        reasons["range_atr_out_of_band"] = f"{range_atr_ratio:.2f} fora de [{rmin},{rmax}]"

    adx_max = float(fcfg["adx_max"])
    if adx_last > adx_max:
        ok = False
        reasons["trend_too_strong"] = f"ADX {adx_last:.1f} > {adx_max}"

    apmin = float(fcfg["atr_pct_min"])
    apmax = float(fcfg["atr_pct_max"])
    if atr_pct < apmin or atr_pct > apmax:
        ok = False
        reasons["atr_pct_out_of_band"] = f"{atr_pct:.3f}% fora de [{apmin},{apmax}]"

    if not silent:
        log.info(format_filter_human(symbol, ok, metrics, reasons, fcfg))
    raw = {"apt": ok, **metrics, **reasons}
    log.debug(
        "FILTER_RAW %s | %s",
        symbol,
        " ".join(f"{k}={v}" for k, v in sorted(raw.items())),
    )

    return FilterResult(apt=ok, reasons=reasons, metrics=metrics)
