"""
Micro-Banca Universal v9 — Bypass Total de UI
Criado por: MK  |  Versão: 9.0

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CAUSA RAIZ DOS 3 SOAKS COM 250 BPS (v8.1, v8.2 soak01, v8.2 soak02)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
O plugin recebia parâmetros críticos (atr_mult, spread_cap, override_enabled)
via ctx["execution"]["plugin_ui"]. Quando a UI injetava valores de uma sessão
anterior ou com cache stale, o plugin calculava spreads errados sem ter como
detectar a origem do problema.

DIAGNÓSTICO FINAL (testado):
  O plugin (hash 4028ecff) com ui={} retorna 60 bps → código estava correto.
  Mas o soak mostrou 250 bps → plugin_ui chegou com valores que forçaram
  o caminho de 250 bps (provavelmente override_enabled=1 ou valores residuais
  de sessão anterior injetados pelo motor via YAML/sessão, não pelo usuário).

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ARQUITETURA v9 — BYPASS TOTAL
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
REGRA: plugin_ui NÃO controla NENHUM parâmetro operacional.

  ✅ Lido da UI (2 campos apenas):
       target_symbol_idx  → qual ativo (0-7)
       preset_mode        → qual nível de agressividade (1-4)

  🔒 Hardcoded no Python (inacessível pela UI):
       spread base, atr_mult, spread_cap, order_size
       filtros ATR/ADX/liquidez, thresholds de inventário
       stop-loss, profit target, resting_order_min_age_sec
       virtual_bank, max_cycles, interval_sec, max_notional

  🚫 Removido completamente:
       override_enabled / override_spread_bps / override_order_size
       atr_spread_mult_override / max_spread_bps_cap
       (qualquer valor que venha da UI e afete o spread)

Se plugin_ui chegar vazio, com valores errados, ou com o conteúdo que
quiser, o spread e os parâmetros de trading NÃO MUDAM.
O único efeito possível da UI é escolher o ativo (0-7) e o modo (1-4).

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
COMO USAR
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Na UI do Motor Plug and Play, preencha apenas:

    Ativo (0-7):  0=BTC 1=ETH 2=SOL 3=BNB 4=XRP 5=PEPE 6=DOGE 7=SHIB
    Modo (1-4):   1=CONSERVADOR 2=MODERADO 3=AGRESSIVO 4=SCALPING

Para mudar qualquer outro parâmetro (spread, filtros, risco):
    Edite diretamente as tabelas _PERFIS e _MODOS neste arquivo.

YAML mínimo necessário:
    agent:
      plugin_enabled: true
      plugin_path: <caminho deste .py>
      bounds:
        max_inventory_base: 1.0
        max_notional_quote_per_order: 200.0
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
from __future__ import annotations
from typing import Any

STRATEGY_DISPLAY_NAME = "Micro-Banca v9 (Bypass UI)"


# ═════════════════════════════════════════════════════════════════════════════
# MAPA DE ATIVOS  (índice inteiro → símbolo)
# Único uso de plugin_ui: target_symbol_idx seleciona qual linha desta tabela.
# ═════════════════════════════════════════════════════════════════════════════
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
_SYMBOL_PARA_IDX: dict[str, int] = {v: k for k, v in _IDX_PARA_SYMBOL.items()}


# ═════════════════════════════════════════════════════════════════════════════
# PERFIS POR ATIVO  — todos os parâmetros hardcoded aqui
# Para ajustar um ativo: edite este dicionário, faça novo upload do .py.
# ═════════════════════════════════════════════════════════════════════════════
_PERFIS: dict[str, dict[str, Any]] = {

    "BTCUSDT": {
        # Spread base + multiplicador ATR (já calibrado para mercado lateral abr/2026)
        # Com ATR 2%: boost = 0.5 × 0.02 × 10000 = 100 bps → cap reduz para 50 bps
        # Com ATR 0.5%: boost = 0.5 × 0.005 × 10000 = 25 bps → spread ≈ 6+25 = 31 bps
        "spread_base_bps":     6.0,
        "atr_mult":            0.5,   # reduzido de 1.2 (v8.1) e 0.6 (v8.2) para mercado lateral
        "order_size_usdt":    15.0,
        "max_inv_quote":     200.0,
        # Inventário base (sincronizar com YAML bounds.max_inventory_base)
        "max_inv_base":        0.003,
        # Controle de inventário
        "emg_thresh":          0.88,  # ratio absoluto → modo emergência
        "dfn_thresh":          0.55,  # ratio absoluto → modo defensivo
        "neutral_band":        0.12,  # ratio → modo ataque
        "atk_tight_bps":       2.0,   # aperta spread no modo ataque
        "atk_size_boost":      5.0,   # aumenta lote no ataque
        "adx_thresh":         28.0,   # ADX acima disso → penalizar tamanho
        "adx_pen":             0.40,  # fator de penalização por ADX alto
        # Filtros internos (parking se violados)
        "f_atr_min":         0.0002,  # ATR mínimo (mercado morto)
        "f_atr_max":         0.030,   # ATR máximo (spike extremo)
        "f_adx_max":         55.0,    # ADX máximo (trend forte)
        "f_liq_min":         5000.0,  # Liquidez mínima USDT no book
    },

    "ETHUSDT": {
        "spread_base_bps":     8.0,
        "atr_mult":            0.7,
        "order_size_usdt":    12.0,
        "max_inv_quote":     150.0,
        "max_inv_base":        0.05,
        "emg_thresh":          0.87,
        "dfn_thresh":          0.55,
        "neutral_band":        0.13,
        "atk_tight_bps":       2.0,
        "atk_size_boost":      3.0,
        "adx_thresh":         28.0,
        "adx_pen":             0.45,
        "f_atr_min":         0.0002,
        "f_atr_max":         0.045,
        "f_adx_max":         55.0,
        "f_liq_min":         2000.0,
    },

    "SOLUSDT": {
        "spread_base_bps":    12.0,
        "atr_mult":            1.0,
        "order_size_usdt":    11.0,
        "max_inv_quote":     100.0,
        "max_inv_base":        0.5,
        "emg_thresh":          0.86,
        "dfn_thresh":          0.55,
        "neutral_band":        0.14,
        "atk_tight_bps":       2.5,
        "atk_size_boost":      2.5,
        "adx_thresh":         30.0,
        "adx_pen":             0.48,
        "f_atr_min":         0.0002,
        "f_atr_max":         0.080,
        "f_adx_max":         58.0,
        "f_liq_min":          800.0,
    },

    "BNBUSDT": {
        "spread_base_bps":    10.0,
        "atr_mult":            0.8,
        "order_size_usdt":    11.0,
        "max_inv_quote":     120.0,
        "max_inv_base":        0.05,
        "emg_thresh":          0.87,
        "dfn_thresh":          0.55,
        "neutral_band":        0.13,
        "atk_tight_bps":       2.0,
        "atk_size_boost":      2.5,
        "adx_thresh":         29.0,
        "adx_pen":             0.45,
        "f_atr_min":         0.0002,
        "f_atr_max":         0.055,
        "f_adx_max":         55.0,
        "f_liq_min":         1500.0,
    },

    "XRPUSDT": {
        "spread_base_bps":    12.0,
        "atr_mult":            1.0,
        "order_size_usdt":    11.0,
        "max_inv_quote":      90.0,
        "max_inv_base":        5.0,
        "emg_thresh":          0.86,
        "dfn_thresh":          0.55,
        "neutral_band":        0.14,
        "atk_tight_bps":       2.5,
        "atk_size_boost":      2.0,
        "adx_thresh":         30.0,
        "adx_pen":             0.50,
        "f_atr_min":         0.0001,
        "f_atr_max":         0.090,
        "f_adx_max":         58.0,
        "f_liq_min":          600.0,
    },

    "PEPEUSDT": {
        "spread_base_bps":    16.0,
        "atr_mult":            1.2,
        "order_size_usdt":    11.0,
        "max_inv_quote":      80.0,
        "max_inv_base":    10_000_000.0,
        "emg_thresh":          0.85,
        "dfn_thresh":          0.55,
        "neutral_band":        0.15,
        "atk_tight_bps":       3.0,
        "atk_size_boost":      2.0,
        "adx_thresh":         32.0,
        "adx_pen":             0.52,
        "f_atr_min":         0.0001,
        "f_atr_max":         0.130,
        "f_adx_max":         60.0,
        "f_liq_min":          200.0,
    },

    "DOGEUSDT": {
        "spread_base_bps":    14.0,
        "atr_mult":            1.1,
        "order_size_usdt":    11.0,
        "max_inv_quote":      80.0,
        "max_inv_base":      500.0,
        "emg_thresh":          0.85,
        "dfn_thresh":          0.55,
        "neutral_band":        0.15,
        "atk_tight_bps":       3.0,
        "atk_size_boost":      2.0,
        "adx_thresh":         31.0,
        "adx_pen":             0.50,
        "f_atr_min":         0.0001,
        "f_atr_max":         0.115,
        "f_adx_max":         58.0,
        "f_liq_min":          250.0,
    },

    "SHIBUSDT": {
        "spread_base_bps":    18.0,
        "atr_mult":            1.3,
        "order_size_usdt":    11.0,
        "max_inv_quote":      75.0,
        "max_inv_base":   10_000_000.0,
        "emg_thresh":          0.85,
        "dfn_thresh":          0.55,
        "neutral_band":        0.15,
        "atk_tight_bps":       3.0,
        "atk_size_boost":      1.5,
        "adx_thresh":         33.0,
        "adx_pen":             0.55,
        "f_atr_min":         0.0001,
        "f_atr_max":         0.150,
        "f_adx_max":         62.0,
        "f_liq_min":          150.0,
    },
}

# Perfil genérico para símbolos não mapeados
_PERFIL_GENERIC: dict[str, Any] = {
    "spread_base_bps":    14.0,
    "atr_mult":            1.0,
    "order_size_usdt":    11.0,
    "max_inv_quote":      80.0,
    "max_inv_base":        1.0,
    "emg_thresh":          0.85,
    "dfn_thresh":          0.55,
    "neutral_band":        0.15,
    "atk_tight_bps":       3.0,
    "atk_size_boost":      2.0,
    "adx_thresh":         30.0,
    "adx_pen":             0.50,
    "f_atr_min":         0.0001,
    "f_atr_max":         0.10,
    "f_adx_max":         55.0,
    "f_liq_min":          300.0,
}


# ═════════════════════════════════════════════════════════════════════════════
# MODOS DE OPERAÇÃO  — segundo parâmetro lido da UI
# Único uso de plugin_ui: preset_mode seleciona qual linha desta tabela.
# Todos os valores são multiplicadores/overrides sobre o perfil.
# ═════════════════════════════════════════════════════════════════════════════
_MODOS: dict[int, dict[str, Any]] = {

    1: {  # ── CONSERVADOR ────────────────────────────────────────────────────
        # Protege capital. Margem maior por trade, menos fills esperados.
        # Bom para: mercado incerto, primeiro dia com ativo novo.
        "_label":         "conservador",
        "spread_cap_bps":  90.0,   # spread máximo permitido
        "atr_mult_fator":   1.4,   # amplifica mult do perfil (mais spread)
        "size_fator":       0.7,   # reduz lote
        "sl_pct":           3.0,   # stop-loss em % do capital inicial
        "pt_usdt":          1.5,   # profit target (modo conservador após meta)
        "f_strict":        True,   # filtros ATR/ADX mais rígidos (-30%)
    },

    2: {  # ── MODERADO (default) ─────────────────────────────────────────────
        # Equilíbrio fill rate × margem. Recomendado para começar.
        "_label":         "moderado",
        "spread_cap_bps":  55.0,
        "atr_mult_fator":   0.8,
        "size_fator":       1.0,
        "sl_pct":           5.0,
        "pt_usdt":          2.0,
        "f_strict":        False,
    },

    3: {  # ── AGRESSIVO ──────────────────────────────────────────────────────
        # Mais fills, spread menor, lote maior. Risco de inventário elevado.
        # Bom para: ATR alto, mercado com movimento claro (ADX 20-30).
        "_label":         "agressivo",
        "spread_cap_bps":  40.0,
        "atr_mult_fator":   0.6,
        "size_fator":       1.3,
        "sl_pct":           7.0,
        "pt_usdt":          3.0,
        "f_strict":        False,
    },

    4: {  # ── SCALPING ───────────────────────────────────────────────────────
        # Spread mínimo, máxima frequência. Funciona em mercados laterais
        # com liquidez alta (BTC, ETH). Margem por trade pequena.
        "_label":         "scalping",
        "spread_cap_bps":  25.0,
        "atr_mult_fator":   0.4,
        "size_fator":       1.0,
        "sl_pct":           4.0,
        "pt_usdt":          1.0,
        "f_strict":        False,
    },
}


# ═════════════════════════════════════════════════════════════════════════════
# CONSTANTES FIXAS DO SOAK  — nunca lidas da UI
# ═════════════════════════════════════════════════════════════════════════════
_SOAK_SYMBOL_DEFAULT   = "BTCUSDT"
_SOAK_INTERVAL_SEC     = 8.0        # ciclos de 8s ≈ 5400 ciclos/dia
_SOAK_MAX_CYCLES       = 450        # ~1h para validação inicial
_SOAK_VB_INITIAL       = 100.0      # capital virtual inicial (USDT)
_SOAK_MAX_NOTIONAL     = 50.0       # cap de notional por ordem
_SOAK_RESTING_MIN_AGE  = 16.0       # ordem precisa ficar 16s antes de cancelar
_SOAK_FALLBACK_CYCLES  = 9999       # desativa fallback automático (plugin filtra)

# Limites de spread absolutos (independentes de modo ou perfil)
_SPREAD_MIN_BPS        = 5.0        # nunca abaixo de 5 bps
_SPREAD_PARKING_BPS    = 500.0      # spread de "saída" durante filtros/SL

# Default para quando os 2 campos da UI não chegarem
_DEFAULT_SYMBOL_IDX    = 0          # BTC
_DEFAULT_MODO          = 2          # MODERADO

# Ciclos consecutivos em parking antes de alertar
_PARKING_ALERT_CYCLES  = 50


# ═════════════════════════════════════════════════════════════════════════════
# TESTNET_SOAK_DEFAULTS  — todos hardcoded, nenhum depende de UI
# ═════════════════════════════════════════════════════════════════════════════
TESTNET_SOAK_DEFAULTS: dict[str, Any] = {
    "strategy_id":                          "micro_banca_v9_mk",
    "symbol":                               _SOAK_SYMBOL_DEFAULT,
    "interval_sec":                         _SOAK_INTERVAL_SEC,
    "max_cycles":                           _SOAK_MAX_CYCLES,
    "agent_provider":                       "plugin",
    "relax_liquidity":                      True,
    "lab_force_quote":                      False,
    "force_heuristic":                      False,
    "max_notional_quote_per_order":         _SOAK_MAX_NOTIONAL,
    "resting_order_min_age_sec":            _SOAK_RESTING_MIN_AGE,
    "no_quote_fallback_after_cycles":       _SOAK_FALLBACK_CYCLES,
    "no_quote_fallback_stage2_after_cycles":_SOAK_FALLBACK_CYCLES,
    "virtual_bank_enabled":                 True,
    "virtual_bank_enforce_orders":          True,
    "virtual_bank_initial_quote":           _SOAK_VB_INITIAL,
    "virtual_bank_initial_base":            0.0,
}

STRATEGY_FILTER_DEFAULTS: dict[str, Any] = {
    "enabled": False,  # filtros vivem no plugin
}


# ═════════════════════════════════════════════════════════════════════════════
# STRATEGY_UI_FIELDS  — APENAS 2 CAMPOS
# Todos os outros parâmetros foram removidos da UI propositalmente.
# ═════════════════════════════════════════════════════════════════════════════
STRATEGY_UI_FIELDS: list[dict[str, Any]] = [
    {
        "key":     "target_symbol_idx",
        "label":   "Ativo: 0=BTC  1=ETH  2=SOL  3=BNB  4=XRP  5=PEPE  6=DOGE  7=SHIB",
        "type":    "number",
        "default": float(_DEFAULT_SYMBOL_IDX),
        "min":     0.0,
        "max":     7.0,
        "step":    1.0,
    },
    {
        "key":     "preset_mode",
        "label":   "Modo: 1=CONSERVADOR  2=MODERADO  3=AGRESSIVO  4=SCALPING",
        "type":    "number",
        "default": float(_DEFAULT_MODO),
        "min":     1.0,
        "max":     4.0,
        "step":    1.0,
    },
]


# ═════════════════════════════════════════════════════════════════════════════
# Helpers
# ═════════════════════════════════════════════════════════════════════════════

def _get_perfil(symbol: str) -> dict[str, Any]:
    return _PERFIS.get(symbol.upper(), _PERFIL_GENERIC)


def _get_modo(mode_int: int) -> dict[str, Any]:
    return _MODOS.get(mode_int, _MODOS[_DEFAULT_MODO])


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _ler_metricas(ctx: dict) -> dict[str, float]:
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


def _mid(ctx: dict) -> float | None:
    ex = dict(ctx.get("execution") or {})
    for k in ("mid", "mid_price", "last_price"):
        try:
            v = float(ex.get(k) or 0)
            if v > 0:
                return v
        except (TypeError, ValueError):
            pass
    return None


def _ler_ui_safe(ctx: dict) -> tuple[int, int]:
    """
    Lê APENAS os 2 campos permitidos da UI.
    Se UI chegar vazia, errada ou com lixo: usa defaults hardcoded.
    Nenhum outro valor de plugin_ui é lido em qualquer lugar do plugin.
    """
    ex = dict(ctx.get("execution") or {})
    ui = dict(ex.get("plugin_ui") or {})

    try:
        sym_idx = int(float(ui.get("target_symbol_idx", _DEFAULT_SYMBOL_IDX)))
        sym_idx = _clamp(sym_idx, 0, 7)
    except (TypeError, ValueError):
        sym_idx = _DEFAULT_SYMBOL_IDX

    try:
        modo = int(float(ui.get("preset_mode", _DEFAULT_MODO)))
        if modo not in (1, 2, 3, 4):
            modo = _DEFAULT_MODO
    except (TypeError, ValueError):
        modo = _DEFAULT_MODO

    return sym_idx, modo


# Contador de skips consecutivos (memória intra-soak)
_skip_streak: int = 0


# ═════════════════════════════════════════════════════════════════════════════
# propose
# ═════════════════════════════════════════════════════════════════════════════

def propose(ctx: dict[str, Any]) -> dict[str, Any]:
    global _skip_streak

    # ── Retorno rápido para validação/vigília (§7.1) ────────────────────────
    if ctx.get("_mk_validate_quick") or ctx.get("_mk_no_blocking_io"):
        sym_idx, modo = _ler_ui_safe(ctx)
        sym = _IDX_PARA_SYMBOL.get(sym_idx, _SOAK_SYMBOL_DEFAULT)
        p = _get_perfil(sym)
        m = _get_modo(modo)
        return {
            "spread_bps":         p["spread_base_bps"],
            "order_size_quote":   p["order_size_usdt"],
            "max_inventory_base": p["max_inv_base"],
            "strategy_id":        "micro_banca_v9_mk",
            "meta": {
                "source":  "mk_plugin_v9",
                "symbol":  sym,
                "modo":    m["_label"],
                "quick":   True,
            },
        }

    # ── Lê os 2 campos da UI (único ponto de entrada de dados externos) ─────
    sym_idx, modo = _ler_ui_safe(ctx)

    # ── Perfil (spread, tamanho, limites) ────────────────────────────────────
    # Sempre a partir do **índice de ativo** na UI (`STRATEGY_UI_FIELDS.target_symbol_idx`).
    # O par que o motor/soak está a negociar vem em `ctx["symbol"]` (livro, métricas, ordens);
    # pode ser diferente do perfil (ex.: soak em BTCUSDT com UI "DOGE" — é avisado em meta).
    symbol = _IDX_PARA_SYMBOL.get(sym_idx, _SOAK_SYMBOL_DEFAULT)
    ctx_symbol = str(ctx.get("symbol") or "").upper()
    mismatch = bool(
        ctx_symbol
        and ctx_symbol not in ("UNKNOWN", "")
        and ctx_symbol != symbol
    )

    # ── Seleciona perfil e modo (100% de tabelas hardcoded) ─────────────────
    p   = _get_perfil(symbol)
    m   = _get_modo(modo)

    # ── Calcula atr_mult: perfil × fator do modo (NADA vem da UI) ───────────
    atr_mult    = p["atr_mult"] * m["atr_mult_fator"]
    spread_cap  = m["spread_cap_bps"]

    # ── Métricas de mercado ──────────────────────────────────────────────────
    ex      = dict(ctx.get("execution") or {})
    mkt     = _ler_metricas(ctx)
    atr_pct = mkt["atr_pct"]
    adx     = mkt["adx"]
    liq     = mkt["liq"]

    # ── Inventário ───────────────────────────────────────────────────────────
    mid_price    = _mid(ctx)
    max_inv_base = p["max_inv_base"]   # hardcoded no perfil
    current_inv  = float(ex.get("inventory_base", 0.0))
    inv_ratio    = (current_inv / max_inv_base) if max_inv_base > 0 else 0.0
    abs_ratio    = abs(inv_ratio)

    # ── Virtual Bank (risco) ─────────────────────────────────────────────────
    vb_initial = float(ex.get("virtual_bank_initial_quote") or _SOAK_VB_INITIAL)
    vb_equity  = float(ex.get("virtual_bank_equity_quote_now") or
                       ex.get("vb_equity_quote_now") or vb_initial)
    vb_pnl     = float(ex.get("virtual_bank_pnl_quote") or
                       ex.get("vb_pnl_quote") or 0.0)

    # ── Parâmetros de risco (hardcoded no modo) ──────────────────────────────
    sl_pct = m["sl_pct"]
    pt_usdt = m["pt_usdt"]
    sl_thresh = vb_initial * (1.0 - sl_pct / 100.0)

    # ── STOP-LOSS ────────────────────────────────────────────────────────────
    if vb_equity < sl_thresh:
        _skip_streak += 1
        return {
            "spread_bps":         _SPREAD_PARKING_BPS,
            "order_size_quote":   10.5,
            "max_inventory_base": max_inv_base,
            "strategy_id":        "micro_banca_v9_mk",
            "meta": {
                "source":      "mk_plugin_v9",
                "symbol":      symbol,
                "modo":        m["_label"],
                "skip":        True,
                "skip_reason": f"STOP_LOSS: equity={vb_equity:.2f} < thresh={sl_thresh:.2f}",
                "skip_streak": _skip_streak,
                "vb_equity":   round(vb_equity, 4),
                "vb_pnl":      round(vb_pnl, 4),
                "profit_lock": False,
            },
        }

    # ── PROFIT LOCK ──────────────────────────────────────────────────────────
    profit_lock = pt_usdt > 0 and vb_pnl >= pt_usdt

    # ── FILTROS INTERNOS (applica modo strict se configurado) ────────────────
    f_atr_max = p["f_atr_max"] * (0.70 if m["f_strict"] else 1.0)
    f_adx_max = p["f_adx_max"] * (0.85 if m["f_strict"] else 1.0)
    f_liq_min = p["f_liq_min"] * (1.50 if m["f_strict"] else 1.0)
    f_atr_min = p["f_atr_min"]

    skip_reason: str | None = None
    if f_atr_max > 0 and atr_pct > f_atr_max:
        skip_reason = f"atr_spike:{atr_pct:.4%}>max{f_atr_max:.4%}"
    elif f_atr_min > 0 and 0 < atr_pct < f_atr_min:
        skip_reason = f"atr_dead:{atr_pct:.4%}<min{f_atr_min:.4%}"
    elif f_adx_max > 0 and adx > f_adx_max:
        skip_reason = f"adx_extreme:{adx:.1f}>max{f_adx_max:.1f}"
    elif f_liq_min > 0 and 0 < liq < f_liq_min:
        skip_reason = f"low_liq:{liq:.0f}<min{f_liq_min:.0f}"

    if skip_reason:
        _skip_streak += 1
        result: dict[str, Any] = {
            "spread_bps":         _SPREAD_PARKING_BPS,
            "order_size_quote":   10.5,
            "max_inventory_base": max_inv_base,
            "strategy_id":        "micro_banca_v9_mk",
            "meta": {
                "source":      "mk_plugin_v9",
                "symbol":      symbol,
                "modo":        m["_label"],
                "skip":        True,
                "skip_reason": skip_reason,
                "skip_streak": _skip_streak,
                "atr_pct":     round(atr_pct, 6),
                "adx":         round(adx, 2),
                "liq":         round(liq, 2),
                "profit_lock": False,
            },
        }
        if _skip_streak >= _PARKING_ALERT_CYCLES:
            result["meta"]["alert"] = (
                f"Parking há {_skip_streak} ciclos. "
                "Mude o ativo (campo Ativo na UI) ou aguarde condições melhores."
            )
        return result

    _skip_streak = 0

    # ════════════════════════════════════════════════════════════════════════
    # CÁLCULO DE SPREAD E LOTE  (zero dependência de plugin_ui)
    # ════════════════════════════════════════════════════════════════════════

    # ATR boost: multiplicador hardcoded × ATR de mercado × escala bps
    atr_boost      = atr_mult * atr_pct * 10_000.0
    dyn_spread     = p["spread_base_bps"] + atr_boost
    dyn_size       = p["order_size_usdt"] * m["size_fator"]
    skew           = "neutro"

    # Penalização por ADX alto (tendência forte → lote menor)
    adx_fator = 1.0
    if adx > p["adx_thresh"]:
        excess    = _clamp((adx - p["adx_thresh"]) / 30.0, 0.0, 1.0)
        adx_fator = 1.0 - p["adx_pen"] * excess

    # Skew de inventário
    if abs_ratio >= p["emg_thresh"]:
        dyn_spread = max(dyn_spread * 3.0, p["spread_base_bps"] * 3.0)
        dyn_size   = max(p["order_size_usdt"] * 0.5, 10.5)
        skew       = "emergencia_cheio" if inv_ratio > 0 else "emergencia_vazio"
    elif abs_ratio >= p["dfn_thresh"]:
        dyn_spread = max(dyn_spread * 1.5, p["spread_base_bps"] * 1.5)
        dyn_size   = max(p["order_size_usdt"] * 0.70, 10.5)
        skew       = "desovando" if inv_ratio > 0 else "cobertura"
    elif abs_ratio <= p["neutral_band"]:
        dyn_spread = max(dyn_spread - p["atk_tight_bps"], p["spread_base_bps"] * 0.6)
        dyn_size   = dyn_size + p["atk_size_boost"]
        skew       = "ataque_neutro"

    if not skew.startswith("emergencia"):
        dyn_size = max(dyn_size * adx_fator, 10.5)

    # Profit lock: reduz exposição sem parar
    if profit_lock and not skew.startswith("emergencia"):
        dyn_spread = dyn_spread * 1.5
        dyn_size   = max(dyn_size * 0.6, 10.5)
        skew       = skew + "+profit_lock"

    # Cap final: clamp entre mínimo absoluto e cap do modo
    final_spread = round(_clamp(dyn_spread, _SPREAD_MIN_BPS, spread_cap), 2)
    final_size   = round(max(dyn_size, 10.5), 2)

    return {
        "spread_bps":         final_spread,
        "order_size_quote":   final_size,
        "max_inventory_base": round(max_inv_base, 8),
        "strategy_id":        "micro_banca_v9_mk",
        "meta": {
            "source":          "mk_plugin_v9",
            "symbol":          symbol,
            "modo":            m["_label"],
            "symbol_mismatch": mismatch,
            "mismatch_alert":  (
                f"Perfil pela UI: idx={sym_idx} → {symbol}; par em execução (soak): {ctx_symbol}. "
                "Alinhe o símbolo do teste ao ativo escolhido, ou o índice ao par."
            ) if mismatch else None,
            "skip":            False,
            "inv_ratio":       round(inv_ratio, 4),
            "skew":            skew,
            "mid_price":       round(mid_price, 8) if mid_price else None,
            "atr_pct":         round(atr_pct, 6),
            "atr_mult_usado":  round(atr_mult, 4),
            "atr_boost_bps":   round(atr_boost, 2),
            "adx":             round(adx, 2),
            "adx_fator":       round(adx_fator, 3),
            "liq":             round(liq, 2),
            "spread_cap_usado":round(spread_cap, 2),
            "max_inv_base":    round(max_inv_base, 8),
            # Risco
            "vb_equity":       round(vb_equity, 4),
            "vb_pnl":          round(vb_pnl, 4),
            "sl_thresh":       round(sl_thresh, 4),
            "profit_lock":     profit_lock,
        },
    }
