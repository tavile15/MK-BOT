"""
Contrato do agente (v1): entrada observável + saída JSON validada antes do motor.

Substitua `propose_strategy_heuristic` por chamada a LLM que devolve o mesmo payload;
`finalize_agent_payload` permanece como gate de segurança (clamp + avisos).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .strategy_params import StrategyParams

CONTRACT_VERSION = "2"


@dataclass
class AgentBounds:
    spread_bps_min: float
    spread_bps_max: float
    order_size_quote_min: float
    order_size_quote_max: float


@dataclass
class AgentSnapshot:
    """
    O que o agente (ou um LLM) vê ao decidir.

    `metrics` / `reasons`: filtros técnicos (ATR, ADX, liquidez…).
    `execution_context`: observáveis de execução/carteira no instante do ciclo
    (modo book_top vs synthetic_mid, taxas, topo do livro, piso de spread do motor).
    Não inclui proposta do agente — só o que o mercado/motor tornam visíveis.
    """

    symbol: str
    contract_version: str
    filter_apt: bool
    metrics: dict[str, float]
    reasons: dict[str, Any] = field(default_factory=dict)
    execution_context: dict[str, Any] = field(default_factory=dict)

    def to_llm_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "contract_version": self.contract_version,
            "symbol": self.symbol,
            "filter_apt": self.filter_apt,
            "metrics": self.metrics,
            "reasons": self.reasons,
        }
        if self.execution_context:
            payload["execution"] = self.execution_context
        return payload


def bounds_from_config(cfg: dict[str, Any]) -> AgentBounds:
    raw = (cfg.get("agent") or {}).get("bounds") or {}
    return AgentBounds(
        spread_bps_min=float(raw.get("spread_bps_min", 1)),
        spread_bps_max=float(raw.get("spread_bps_max", 500)),
        order_size_quote_min=float(raw.get("order_size_quote_min", 1)),
        order_size_quote_max=float(raw.get("order_size_quote_max", 50_000)),
    )


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def finalize_agent_payload(
    data: dict[str, Any],
    bounds: AgentBounds,
    *,
    symbol: str,
    source: str,
    extra_meta: dict[str, Any] | None = None,
) -> tuple[StrategyParams | None, list[str]]:
    """
    Valida tipos, limita valores aos bounds e devolve StrategyParams.

    Retorna (None, [erro]) só se payload ilegível (falta campo, não numérico, NaN).
    Ajustes por limites viram strings em `warnings` (não abortam o motor).
    """
    warnings: list[str] = []
    try:
        spread = float(data["spread_bps"])
        size_q = float(data["order_size_quote"])
        max_inv = float(data["max_inventory_base"])
    except (KeyError, TypeError, ValueError) as e:
        return None, [f"payload_invalido: {e}"]

    for label, v in (
        ("spread_bps", spread),
        ("order_size_quote", size_q),
        ("max_inventory_base", max_inv),
    ):
        if v != v:  # NaN
            return None, [f"{label}_nan"]

    spread_c = _clamp(spread, bounds.spread_bps_min, bounds.spread_bps_max)
    size_c = _clamp(size_q, bounds.order_size_quote_min, bounds.order_size_quote_max)
    max_inv_c = max(0.0, max_inv)

    if spread_c != spread:
        warnings.append("spread_bps_clamped")
    if size_c != size_q:
        warnings.append("order_size_quote_clamped")
    if max_inv_c != max_inv:
        warnings.append("max_inventory_base_clamped")

    meta: dict[str, Any] = {
        "source": source,
        "contract_version": CONTRACT_VERSION,
        "symbol": symbol,
        "warnings": warnings,
        "raw_proposal": dict(data),
    }
    if extra_meta:
        meta.update(extra_meta)

    return (
        StrategyParams(
            spread_bps=round(spread_c, 4),
            order_size_quote=round(size_c, 6),
            max_inventory_base=round(max_inv_c, 10),
            meta=meta,
        ),
        warnings,
    )


def propose_strategy_heuristic(
    symbol: str,
    metrics: dict[str, float],
    cfg: dict[str, Any],
) -> dict[str, Any]:
    """Proposta bruta. Um LLM deve devolver dict com as mesmas chaves."""
    _ = symbol
    acfg = cfg["agent"]
    spread = float(acfg["default_spread_bps"])
    atr_pct = float(metrics.get("atr_pct", 0.2))
    spread *= 1.0 + min(1.0, atr_pct / 2.0)
    size_q = float(acfg["default_order_size_quote"])
    last = float(metrics.get("last_price", 1.0))
    inv_pct = float(acfg["max_inventory_pct"])
    max_base = (size_q * 3 / last) * inv_pct if last else 0.01
    return {
        "spread_bps": spread,
        "order_size_quote": size_q,
        "max_inventory_base": max_base,
    }
