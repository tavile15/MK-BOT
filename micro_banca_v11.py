"""
Micro-Banca v11 BTC - Market Making Assimetrico Dinâmico
Motor: binance_live_engine.py (plug and play)
Ativo exclusivo: BTCUSDT

Mudanças da V11:
- Lotes proporcionais ao tamanho da banca virtual.
- Identificação de regime de mercado (Lateral / Tendência).
- Histórico de preços para identificar micro-tendência.
- Código componentizado em funções.
"""
from __future__ import annotations
from typing import Any
import math

# =============================================================================
# CONSTANTES OBRIGATÓRIAS DO MOTOR
# =============================================================================
STRATEGY_DISPLAY_NAME: str = "MICRO-BANCA V11 BTC"

TESTNET_SOAK_DEFAULTS: dict = {
    "interval_sec":              10.0,
    "resting_order_min_age_sec": 25.0, # Regra: sempre >= interval_sec
    "max_cycles":                2400,
}

_SYMBOL_INDEX: dict[int, str] = {
    0: "BTCUSDT",
}

# =============================================================================
# PARÂMETROS E ESTADO GLOBAL
# =============================================================================
_BTC_CONFIG = {
    "spread_base_bps":      10.0,
    "min_spread_bps":       8.0,
    "max_spread_bps":       80.0,
    "risk_pct_per_order":   0.02,   # 2% da banca por ordem
    "max_inv_base":         0.005,  # Inventário máximo de BTC
    "emg_thresh":           0.85,   # % de inventário para emergência
    "dfn_thresh":           0.50,   # % de inventário para desova
}

# Histórico global para inferir a direção da tendência
_price_history: list[float] = []
_HISTORY_LEN = 10
_cycle_counter: int = 0
_last_regime: str = ""

# =============================================================================
# FUNÇÕES AUXILIARES (LÓGICA DE NEGÓCIO)
# =============================================================================

def _clamp(v: float, lo: float, hi: float) -> float:
    """Trava um valor dentro dos limites."""
    if not math.isfinite(v): return lo
    return max(lo, min(hi, v))

def _update_and_get_direction(mid: float) -> int:
    """
    Atualiza o histórico de preços e retorna a direção da micro-tendência.
    Retorna: 1 (Alta), -1 (Baixa), 0 (Neutra/Lateral)
    """
    global _price_history
    _price_history.append(mid)
    if len(_price_history) > _HISTORY_LEN:
        _price_history.pop(0)
        
    if len(_price_history) < 3:
        return 0
        
    sma = sum(_price_history) / len(_price_history)
    # Se o preço atual está 0.05% acima/abaixo da média curta
    if mid > sma * 1.0005: return 1
    if mid < sma * 0.9995: return -1
    return 0

def _get_market_regime(adx: float) -> str:
    """Identifica a fase atual do mercado usando o ADX."""
    if adx == 0.0: return "WARMUP"
    if adx < 25.0: return "LATERAL"
    if adx < 40.0: return "TENDENCIA_LEVE"
    return "TENDENCIA_FORTE"

def _calc_dynamic_size(equity: float, regime: str) -> float:
    """Calcula o tamanho da ordem baseado na banca e fase do mercado."""
    base_size = equity * _BTC_CONFIG["risk_pct_per_order"]
    
    # Ajusta o risco baseado no mercado
    if regime == "LATERAL":
        base_size *= 1.2 # Mais confiança na consolidação
    elif regime == "TENDENCIA_FORTE":
        base_size *= 0.5 # Menos exposição no olho do furacão
        
    return _clamp(base_size, 12.0, equity * 0.5)

def _calc_asymmetry(spread_base: float, size_base: float, inv_base: float, mid: float, regime: str, direction: int) -> dict:
    """
    Calcula os spreads e tamanhos assimétricos (BID e ASK) baseados no
    inventário atual e na direção da tendência.
    """
    inv_ratio = (inv_base / _BTC_CONFIG["max_inv_base"]) if _BTC_CONFIG["max_inv_base"] > 0 else 0
    
    bid_spread = spread_base
    ask_spread = spread_base
    bid_size = size_base
    ask_size = size_base
    skew_label = "NEUTRO"

    # 1. Defesa de Inventário (Sobrescreve tendência)
    if inv_ratio > _BTC_CONFIG["emg_thresh"]:
        # Muito BTC: Longe na compra, agressivo na venda
        bid_spread = spread_base * 2.5
        ask_spread = spread_base * 0.5
        bid_size = 12.0
        ask_size = size_base * 1.5
        skew_label = "EMERGENCIA_VENDA"
        
    elif inv_ratio > _BTC_CONFIG["dfn_thresh"]:
        # Desova: Levemente inclinado pra venda
        bid_spread = spread_base * 1.5
        ask_spread = spread_base * 0.8
        ask_size = size_base * 1.2
        skew_label = "DESOVANDO_BTC"
        
    elif inv_ratio < 0.1:
        # Pouco BTC: Precisamos comprar
        bid_spread = spread_base * 0.8
        ask_spread = spread_base * 1.5
        bid_size = size_base * 1.2
        skew_label = "ACUMULANDO_BTC"
        
    # 2. Ajuste Fino por Tendência (se não estivermos em emergência)
    if "EMERGENCIA" not in skew_label and regime != "LATERAL":
        if direction == 1: # Tendência de Alta
            bid_spread *= 1.2 # Afasta o BID para não comprar topo
            ask_spread *= 0.8 # Vende mais rápido aproveitando a alta
            skew_label += "+BULL"
        elif direction == -1: # Tendência de Baixa
            bid_spread *= 0.8 # Compra nas quedas
            ask_spread *= 1.5 # Afasta o ASK para não vender fundo
            skew_label += "+BEAR"

    return {
        "bid_spread_bps": _clamp(bid_spread, _BTC_CONFIG["min_spread_bps"], _BTC_CONFIG["max_spread_bps"]),
        "ask_spread_bps": _clamp(ask_spread, _BTC_CONFIG["min_spread_bps"], _BTC_CONFIG["max_spread_bps"]),
        "bid_size_quote": max(12.0, bid_size),
        "ask_size_quote": max(12.0, ask_size),
        "skew_label": skew_label
    }

# =============================================================================
# FUNÇÃO PRINCIPAL PROPOSE()
# =============================================================================

def propose(ctx: dict[str, Any]) -> dict[str, Any]:
    global _cycle_counter, _last_regime
    
    # Proteção blindada (Regra 1 do Guide)
    try:
        _cycle_counter += 1
        
        # Leitura de dados vitais
        ex = dict(ctx.get("execution") or {})
        met = dict(ctx.get("metrics") or {})
        
        mid = float(ex.get("mid") or 0.0)
        adx = float(met.get("adx") or 0.0)
        atr_pct = float(met.get("atr_pct") or 0.0)
        
        equity = float(ex.get("virtual_bank_equity_quote_now") or 100.0)
        inv_base = float(ex.get("inventory_base") or 0.0)

        # Regra 3: mid inválido = parking imediato
        if mid <= 0 or not math.isfinite(mid):
            return {
                "spread_bps": 20.0,
                "order_size_quote": 12.0,
                "meta": {"skip": True, "skip_reason": "Falha no Mid Price"}
            }

        # Analisa o mercado atual
        regime = _get_market_regime(adx)
        direction = _update_and_get_direction(mid)
        
        # Print específico se o regime mudar (útil pra debug no terminal)
        if regime != _last_regime and regime != "WARMUP":
            print(f"[V11 BTC] Mudança de Regime: {_last_regime} -> {regime} | ADX: {adx:.1f} | Banca: ${equity:.2f}")
            _last_regime = regime

        # Calcula o Spread Base usando o ATR
        # (ATR em %) * 10.000 transforma em bps. Multiplicamos por 2 para margem.
        atr_bps = atr_pct * 10000.0 * 2.0
        base_spread = max(_BTC_CONFIG["spread_base_bps"], atr_bps)

        # Ajuste de spread macro pelo regime
        if regime == "LATERAL":
            base_spread *= 0.8
        elif regime == "TENDENCIA_FORTE":
            base_spread *= 1.8
            
        base_spread = _clamp(base_spread, _BTC_CONFIG["min_spread_bps"], _BTC_CONFIG["max_spread_bps"])

        # Calcula o lote dinâmico baseado no tamanho da banca virtual
        base_size = _calc_dynamic_size(equity, regime)

        # Calcula a Assimetria (Skew) de compras e vendas
        skew_data = _calc_asymmetry(base_spread, base_size, inv_base, mid, regime, direction)

        # Retorna o dicionário no padrão estrito da Engine
        return {
            "spread_bps": round(base_spread, 2),
            "order_size_quote": round(base_size, 2),
            
            # Parâmetros Assimétricos
            "bid_spread_bps": round(skew_data["bid_spread_bps"], 2),
            "ask_spread_bps": round(skew_data["ask_spread_bps"], 2),
            "bid_size_quote": round(skew_data["bid_size_quote"], 2),
            "ask_size_quote": round(skew_data["ask_size_quote"], 2),
            
            "meta": {
                "skip": False,
                "skew": skew_data["skew_label"],
                "skew_status": skew_data["skew_label"],
                "regime": regime,
                "banca_atual": round(equity, 2),
            }
        }
        
    except Exception as e:
        # Fallback de segurança silencioso pro motor não crashar
        print(f"[V11 BTC] ERRO INTERNO: {e}. Operando em modo de segurança.")
        return {
            "spread_bps": 30.0,
            "order_size_quote": 12.0,
            "meta": {"skip": True, "skip_reason": "Falha no Plugin V11", "skew": "ERRO"}
        }