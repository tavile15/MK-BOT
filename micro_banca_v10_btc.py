"""
Micro-Banca v10 BTC - Market Making Assimetrico
Motor: binance_live_engine.py (plug and play)
Versao: 10.0 | Ativo exclusivo: BTCUSDT

INTEGRACAO: altere linha 40 do motor de
    import micro_banca_v9 as _strat
para:
    import micro_banca_v10_btc as _strat

FASES:
  Bootstrap (ciclos 1-5): spread 5bps + lote 25 USDT.
    Inventario zero = motor so coloca BID. Acumula BTC antes do MM.
  Operacao (ciclo 6+): MM assimetrico via zonas de inventario.
    inv<15% -> spread x0.75 + boost lote (ataque/compra)
    inv 15-55% -> spread base + ATR (normal)
    inv 55-88% -> spread x1.4, lote reduzido (desova/venda)
    inv>88% -> spread x2.5, lote minimo (emergencia)
  Exit (ultimos 25 ciclos em soak): liquidacao lucrativa.
    Em live (sem max_cycles) o exit nao ativa por ciclo.
"""
from __future__ import annotations
from typing import Any

STRATEGY_DISPLAY_NAME = "Micro-Banca v10 BTC"


# =============================================================================
# ATRIBUTOS LIDOS PELO MOTOR (nomes obrigatorios - nao renomear)
# =============================================================================

_IDX_PARA_SYMBOL: dict[int, str] = {
    0: "BTCUSDT",
    1: "ETHUSDT",
    2: "SOLUSDT",
    3: "BNBUSDT",
    4: "XRPUSDT",
    5: "PEPEUSDT",
    6: "DOGEUSDT",
    7: "SHIBUSDT",
}

# Compatibilidade com o wizard do simple_engine_v1.py (usa _SYMBOL_INDEX).
_SYMBOL_INDEX: dict[int, str] = dict(_IDX_PARA_SYMBOL)

# _MODOS: motor le _label, spread_cap_bps, size_fator, sl_pct (obrigatorio)
_MODOS: dict[int, dict[str, Any]] = {
    1: {
        "_label":          "conservador",
        "spread_cap_bps":   80.0,
        "size_fator":        0.75,
        "sl_pct":            3.0,
        "pt_usdt":           1.5,
        "atr_fator":         1.3,
        "f_strict":         True,
        "exit_spread_bps":  18.0,
    },
    2: {
        "_label":          "operacional",
        "spread_cap_bps":   50.0,
        "size_fator":        1.0,
        "sl_pct":            5.0,
        "pt_usdt":           2.5,
        "atr_fator":         0.8,
        "f_strict":         False,
        "exit_spread_bps":  14.0,
    },
    3: {
        "_label":          "agressivo",
        "spread_cap_bps":   35.0,
        "size_fator":        1.3,
        "sl_pct":            7.0,
        "pt_usdt":           4.0,
        "atr_fator":         0.6,
        "f_strict":         False,
        "exit_spread_bps":  12.0,
    },
    4: {
        # SCALPING: spread minimo, maxima frequencia.
        # Ideal para BTC em mercado lateral com ATR baixo.
        "_label":          "scalping",
        "spread_cap_bps":   25.0,
        "size_fator":        1.0,
        "sl_pct":            4.0,
        "pt_usdt":           1.0,
        "atr_fator":         0.4,
        "f_strict":         False,
        "exit_spread_bps":  10.0,
    },
}

# Compatibilidade de defaults com simple_engine_v1.py
# (o motor le TESTNET_SOAK_DEFAULTS para intervalo/resting).
_SOAK_INTERVAL_SEC    = 8.0   # 8s/ciclo = ~450 ciclos/hora
_SOAK_RESTING_MIN_AGE = 16.0  # ordem vive minimo 2 ciclos antes de cancelar
TESTNET_SOAK_DEFAULTS: dict[str, Any] = {
    "interval_sec": _SOAK_INTERVAL_SEC,
    "resting_order_min_age_sec": _SOAK_RESTING_MIN_AGE,
}


# =============================================================================
# PERFIL BTC - parametros de trading hardcoded
# Para ajustar: edite aqui e reinicie o motor.
# =============================================================================
_BTC = {
    "spread_base_bps":      7.0,   # spread base (bps)
    "atr_mult":             0.5,   # multiplicador ATR
    "bid_ratio":            0.42,  # BID a 42% do spread do mid (assimetria doc.)
    "ask_ratio":            0.58,  # ASK a 58% do spread do mid (assimetria doc.)
    "bootstrap_size_usdt": 25.0,   # lote fase bootstrap
    "normal_size_usdt":    15.0,   # lote operacao normal
    "exit_size_usdt":      20.0,   # lote fase exit
    "max_inv_base":         0.004, # BTC max (~$308 @ $77k)
    "emg_thresh":           0.88,  # ratio > 88%: emergencia
    "dfn_thresh":           0.55,  # ratio 55-88%: defensivo
    "neutral_band":         0.15,  # ratio < 15%: ataque
    "adx_thresh":          28.0,   # ADX acima: penaliza lote
    "adx_pen":              0.35,  # fator de penalizacao
    "f_atr_min":            0.00018,
    "f_atr_max":            0.025,
    "f_adx_max":           52.0,
    "f_liq_min":         5000.0,
    "fee_floor_bps":       12.0,   # spread minimo operacional
}

_SPREAD_PARKING  = 500.0
_SPREAD_MIN_ABS  = 5.0
_PARKING_ALERT_N = 50

# =============================================================================
# ESTADO DE SESSAO (reinicia a cada import/reload)
# =============================================================================
_cycle_counter:      int  = 0
_skip_streak:        int  = 0
_bootstrap_complete: bool = False


# =============================================================================
# HELPERS
# =============================================================================

def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _get_modo(mode_int: int) -> dict[str, Any]:
    return _MODOS.get(mode_int, _MODOS[2])


def _metricas(ctx: dict) -> dict[str, float]:
    m = dict(ctx.get("metrics") or {})
    return {
        "atr_pct": float(m.get("atr_pct") or m.get("atr_perc") or 0.0),
        "adx":     float(m.get("adx") or 0.0),
        "liq":     float(
            m.get("liquidity_quote") or
            m.get("bid_liquidity_quote") or
            m.get("ask_liquidity_quote") or 0.0
        ),
    }


def _mid_price(ctx: dict) -> float:
    ex = dict(ctx.get("execution") or {})
    for k in ("mid", "mid_price", "last_price"):
        try:
            v = float(ex.get(k) or 0)
            if v > 0:
                return v
        except (TypeError, ValueError):
            pass
    return 0.0


def _make_parking(reason: str, modo_label: str,
                  extra: dict | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "spread_bps":         _SPREAD_PARKING,
        "order_size_quote":   10.5,
        "max_inventory_base": _BTC["max_inv_base"],
        "strategy_id":        "micro_banca_v10_btc",
        "meta": {
            "source":      "mk_v10",
            "phase":       "parking",
            "modo":        modo_label,
            "skip":        True,
            "skip_reason": reason,
            "skew":        "parking",
            "profit_lock": False,
        },
    }
    if extra:
        result["meta"].update(extra)
    return result


# =============================================================================
# propose() - chamado pelo motor a cada ciclo
# =============================================================================

def propose(ctx: dict[str, Any]) -> dict[str, Any]:  # noqa: C901
    global _cycle_counter, _skip_streak, _bootstrap_complete

    # Leitura do contexto
    ex = dict(ctx.get("execution") or {})
    ui = dict(ex.get("plugin_ui")  or {})

    # Modo: motor injeta via plugin_ui["preset_mode"]
    try:
        modo_int = int(float(ui.get("preset_mode", 2)))
        if modo_int not in (1, 2, 3, 4):
            modo_int = 2
    except (TypeError, ValueError):
        modo_int = 2
    m = _get_modo(modo_int)

    # max_cycles: definido pelo usuario no soak; 9999 = sessao live sem fim
    try:
        max_cycles = int(float(ui.get("soak_max_cycles", 9999)))
        max_cycles = max(50, min(99999, max_cycles))
    except (TypeError, ValueError):
        max_cycles = 9999

    # Contador de ciclos
    _cycle_counter += 1
    cycle = _cycle_counter

    # Aviso de ativo diferente de BTC
    symbol_ctx = str(ctx.get("symbol") or "BTCUSDT").upper()
    btc_warn = symbol_ctx not in ("BTCUSDT", "")

    # Metricas de mercado
    mkt     = _metricas(ctx)
    atr_pct = mkt["atr_pct"]
    adx     = mkt["adx"]
    liq     = mkt["liq"]
    mid     = _mid_price(ctx)

    # Virtual Bank - campos fornecidos por VirtualBank.to_ctx() do motor
    vb_initial = float(ex.get("virtual_bank_initial_quote") or 100.0)
    vb_equity  = float(
        ex.get("virtual_bank_equity_quote_now") or
        ex.get("vb_equity_quote_now") or vb_initial
    )
    vb_pnl    = float(ex.get("virtual_bank_pnl_quote") or 0.0)
    # inventory_base: campo canonico do motor (VirtualBank.to_ctx)
    current_inv = float(ex.get("inventory_base") or 0.0)
    # avg_cost_base: nao esta em to_ctx() do motor; fallback gracioso para 0
    avg_cost = float(ex.get("avg_cost_base") or 0.0)

    # Inventario
    max_inv_base = _BTC["max_inv_base"]
    inv_ratio    = (current_inv / max_inv_base) if max_inv_base > 0 else 0.0
    abs_ratio    = abs(inv_ratio)

    # STOP-LOSS
    sl_thresh = vb_initial * (1.0 - m["sl_pct"] / 100.0)
    if vb_equity < sl_thresh:
        _skip_streak += 1
        return _make_parking(
            f"STOP_LOSS:equity={vb_equity:.2f}<thresh={sl_thresh:.2f}",
            m["_label"],
            {"vb_equity": round(vb_equity, 4), "vb_pnl": round(vb_pnl, 4),
             "cycle": cycle, "skip_streak": _skip_streak},
        )

    # =========================================================================
    # DETECCAO DE FASE
    # =========================================================================
    _BOOTSTRAP_END = 5
    # Em live (max_cycles=9999): _EXIT_START=9975 -> exit nunca ativa por ciclo
    _EXIT_START    = max_cycles - 24

    is_bootstrap = cycle <= _BOOTSTRAP_END and not _bootstrap_complete
    is_exit      = cycle >= _EXIT_START

    if cycle > _BOOTSTRAP_END:
        _bootstrap_complete = True

    # =========================================================================
    # FASE 1 - BOOTSTRAP (ciclos 1-5, ~40s)
    # Inventario=0 -> motor tenta BID e ASK, mas ASK bloqueado pela banca
    # virtual (sem base). Efeito: APENAS BUY executa. Acumulacao pura de BTC.
    # Filtros desativados para nao bloquear a entrada inicial.
    # =========================================================================
    if is_bootstrap:
        return {
            "spread_bps":         5.0,
            "order_size_quote":   _BTC["bootstrap_size_usdt"],
            "max_inventory_base": max_inv_base,
            "strategy_id":        "micro_banca_v10_btc",
            "meta": {
                "source":      "mk_v10",
                "phase":       "bootstrap_buy",
                "modo":        m["_label"],
                "cycle":       cycle,
                "skip":        False,
                "skew":        "bootstrap",
                "profit_lock": False,
                "inv_ratio":   round(inv_ratio, 4),
                "note":        f"Bootstrap {cycle}/{_BOOTSTRAP_END}: acumulando BTC",
                "aviso":       f"v10=BTC-only, ativo={symbol_ctx}" if btc_warn else None,
            },
        }

    # =========================================================================
    # FASE 3 - EXIT / LIQUIDACAO (ultimos 25 ciclos em soak com max_cycles)
    # Em live (max_cycles=9999): esta fase nao e ativada por ciclo.
    # =========================================================================
    if is_exit:
        cycles_left = max_cycles - cycle
        exit_spread = m["exit_spread_bps"]

        # Calibra spread pelo custo medio se disponivel
        _FEE_RT = 0.0015  # 0.15% round-trip maker com BNB
        if avg_cost > 0 and mid > 0:
            min_ask    = avg_cost * (1.0 + _FEE_RT + 0.0002)
            ask_offset = max(0.0, (min_ask - mid) / mid * 10_000.0)
            min_spread = (ask_offset / _BTC["ask_ratio"]) * 2.0
            exit_spread = max(exit_spread, min(min_spread, 60.0))

        exit_size = _BTC["exit_size_usdt"]
        if cycles_left <= 5:
            exit_size = min(exit_size * 1.5, 45.0)

        return {
            "spread_bps":         round(_clamp(exit_spread, _SPREAD_MIN_ABS, 60.0), 2),
            "order_size_quote":   round(max(exit_size, 10.5), 2),
            "max_inventory_base": max_inv_base,
            "strategy_id":        "micro_banca_v10_btc",
            "meta": {
                "source":           "mk_v10",
                "phase":            "exit_liquidation",
                "modo":             m["_label"],
                "cycle":            cycle,
                "cycles_left":      cycles_left,
                "skip":             False,
                "skew":             "exit",
                "profit_lock":      False,
                "inv_ratio":        round(inv_ratio, 4),
                "inv_btc":          round(current_inv, 8),
                "avg_cost":         round(avg_cost, 2) if avg_cost > 0 else None,
                "min_ask_lucrat":   round(avg_cost * (1 + _FEE_RT), 2) if avg_cost > 0 else None,
                "vb_pnl":           round(vb_pnl, 4),
            },
        }

    # =========================================================================
    # FASE 2 - OPERACAO (ciclo 6 em diante)
    # Market making assimetrico com ATR adaptativo + skew de inventario.
    # =========================================================================

    # Filtros internos
    strict    = m["f_strict"]
    f_atr_max = _BTC["f_atr_max"] * (0.70 if strict else 1.0)
    f_adx_max = _BTC["f_adx_max"] * (0.85 if strict else 1.0)
    f_liq_min = _BTC["f_liq_min"] * (1.50 if strict else 1.0)
    f_atr_min = _BTC["f_atr_min"]

    skip_reason: str | None = None
    if   f_atr_max > 0 and atr_pct > f_atr_max:
        skip_reason = f"atr_spike:{atr_pct:.4%}>max{f_atr_max:.4%}"
    elif f_atr_min > 0 and 0 < atr_pct < f_atr_min:
        skip_reason = f"atr_dead:{atr_pct:.4%}<min{f_atr_min:.4%}"
    elif f_adx_max > 0 and adx > f_adx_max:
        skip_reason = f"adx_extreme:{adx:.1f}>max{f_adx_max:.1f}"
    elif f_liq_min > 0 and 0 < liq < f_liq_min:
        skip_reason = f"low_liq:{liq:.0f}<min{f_liq_min:.0f}"

    if skip_reason:
        _skip_streak += 1
        result = _make_parking(skip_reason, m["_label"],
                               {"cycle": cycle, "skip_streak": _skip_streak,
                                "atr_pct": round(atr_pct, 6),
                                "adx": round(adx, 2)})
        if _skip_streak >= _PARKING_ALERT_N:
            result["meta"]["alert"] = (
                f"Parking {_skip_streak} ciclos. "
                "Aguardando condicoes normais de mercado."
            )
        return result

    _skip_streak = 0

    # Spread base + contribuicao ATR
    atr_mult   = _BTC["atr_mult"] * m["atr_fator"]
    atr_boost  = atr_mult * atr_pct * 10_000.0
    dyn_spread = _BTC["spread_base_bps"] + atr_boost
    dyn_size   = _BTC["normal_size_usdt"] * m["size_fator"]
    skew       = "neutro"

    # Penalizacao de lote por ADX alto (tendencia direcional)
    adx_fator = 1.0
    if adx > _BTC["adx_thresh"]:
        excess    = _clamp((adx - _BTC["adx_thresh"]) / 30.0, 0.0, 1.0)
        adx_fator = 1.0 - _BTC["adx_pen"] * excess

    # Skew de inventario - nucleo da assimetria
    # EMERGENCIA (>88%): para de comprar, espera venda
    # DEFENSIVO (55-88%): favorece venda, spread mais largo
    # NEUTRO (<15%): compra agressiva, spread apertado + boost lote
    #   Se current_inv=0: motor nao coloca ASK (sem base) -> so BID executa
    if abs_ratio >= _BTC["emg_thresh"]:
        dyn_spread = max(dyn_spread * 2.5, _BTC["spread_base_bps"] * 2.5)
        dyn_size   = max(_BTC["normal_size_usdt"] * 0.50, 10.5)
        skew       = "emergencia_cheio"
    elif abs_ratio >= _BTC["dfn_thresh"]:
        dyn_spread = max(dyn_spread * 1.4, _BTC["spread_base_bps"] * 1.4)
        dyn_size   = max(_BTC["normal_size_usdt"] * 0.70, 10.5)
        skew       = "desovando"
    elif abs_ratio <= _BTC["neutral_band"]:
        dyn_spread = max(dyn_spread * 0.75, _BTC["spread_base_bps"] * 0.70)
        dyn_size   = _BTC["normal_size_usdt"] + 5.0
        skew       = "ataque_neutro"

    if not skew.startswith("emergencia"):
        dyn_size = max(dyn_size * adx_fator, 10.5)

    # Profit lock: reduz exposicao apos atingir meta de lucro
    pt_usdt     = m["pt_usdt"]
    profit_lock = pt_usdt > 0 and vb_pnl >= pt_usdt
    if profit_lock and not skew.startswith("emergencia"):
        dyn_spread = dyn_spread * 1.5
        dyn_size   = max(dyn_size * 0.6, 10.5)
        skew       = skew + "+profit_lock"

    # Floor de taxa + cap do modo
    dyn_spread   = max(dyn_spread, _BTC["fee_floor_bps"])
    final_spread = round(_clamp(dyn_spread, _SPREAD_MIN_ABS, m["spread_cap_bps"]), 2)
    final_size   = round(max(dyn_size, 10.5), 2)

    # Assimetria documentada (informacional para auditoria)
    bid_bps = round(final_spread * _BTC["bid_ratio"], 2)
    ask_bps = round(final_spread * _BTC["ask_ratio"], 2)

    return {
        "spread_bps":         final_spread,
        "order_size_quote":   final_size,
        "max_inventory_base": round(max_inv_base, 8),
        "strategy_id":        "micro_banca_v10_btc",
        "meta": {
            "source":        "mk_v10",
            "phase":         "operating",
            "modo":          m["_label"],
            "cycle":         cycle,
            "skip":          False,
            "inv_ratio":     round(inv_ratio, 4),
            "inv_btc":       round(current_inv, 8),
            "skew":          skew,
            "mid":           round(mid, 2) if mid > 0 else None,
            "atr_pct":       round(atr_pct, 6),
            "atr_boost_bps": round(atr_boost, 2),
            "adx":           round(adx, 2),
            "adx_fator":     round(adx_fator, 3),
            "liq":           round(liq, 2),
            "bid_bps":       bid_bps,
            "ask_bps":       ask_bps,
            "profit_lock":   profit_lock,
            "vb_equity":     round(vb_equity, 4),
            "vb_pnl":        round(vb_pnl, 4),
            "sl_thresh":     round(sl_thresh, 4),
            "cycles_to_exit": max(_EXIT_START - cycle, 0) if max_cycles < 9999 else None,
            "aviso":         f"v10=BTC-only, ativo={symbol_ctx}" if btc_warn else None,
        },
    }
