from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any

from .agent_contract import (
    CONTRACT_VERSION,
    AgentSnapshot,
    bounds_from_config,
    finalize_agent_payload,
    propose_strategy_heuristic,
)
from .agent_gemini import propose_strategy_gemini
from .config_loader import load_config
from .strategy_params import StrategyParams


def generate_strategy_once(
    symbol: str,
    metrics: dict[str, float],
    cfg: dict[str, Any] | None = None,
    logger: logging.Logger | None = None,
    silent: bool = False,
    *,
    filter_apt: bool | None = None,
    reasons: list[str] | None = None,
    state_extra: dict[str, Any] | None = None,
    execution_context: dict[str, Any] | None = None,
) -> StrategyParams:
    """
    Agente v1: heurística local → payload → `finalize_agent_payload` (contrato + limites).

    Para LLM: obtenha dict com as chaves `spread_bps`, `order_size_quote`, `max_inventory_base`
    e passe em `finalize_agent_payload` (nunca aplicar números crus no motor).

    `state_extra` (motor): guarda `gemini_budget_exhausted` — no máximo **uma** requisição HTTP Gemini
    por janela de abertura de ciclo (evita custo repetido se o mercado oscila ou há erro pós-API).
    """
    cfg = cfg or load_config()
    log = logger or logging.getLogger("agent")
    se = state_extra
    bounds = bounds_from_config(cfg)
    acfg = cfg.get("agent") or {}
    provider = str(acfg.get("provider") or "heuristic").strip().lower()

    if provider == "gemini":
        if se is not None and se.get("gemini_budget_exhausted"):
            raw = propose_strategy_heuristic(symbol, metrics, cfg)
            src = "agent_heuristic_gemini_budget_spent"
        else:
            snap = AgentSnapshot(
                symbol=symbol,
                contract_version=CONTRACT_VERSION,
                filter_apt=bool(filter_apt) if filter_apt is not None else True,
                metrics=dict(metrics),
                reasons=dict(reasons) if isinstance(reasons, dict) else {},
                execution_context=dict(execution_context) if execution_context else {},
            )
            if se is not None:
                se["gemini_budget_exhausted"] = True
            try:
                raw = propose_strategy_gemini(snap, cfg, log, silent=silent)
                src = "agent_gemini"
            except Exception as e:
                if not silent:
                    log.warning("Gemini indisponível (%s); usando heurística.", e)
                raw = propose_strategy_heuristic(symbol, metrics, cfg)
                src = "agent_heuristic_gemini_fallback"
    else:
        raw = propose_strategy_heuristic(symbol, metrics, cfg)
        src = "agent_heuristic"

    if src == "agent_gemini":
        floor = float(acfg.get("gemini_min_spread_bps", 15.0))
        raw = dict(raw)
        raw["spread_bps"] = max(float(raw["spread_bps"]), floor)

    params, warns = finalize_agent_payload(
        raw,
        bounds,
        symbol=symbol,
        source=src,
    )
    if params is None:
        if not silent:
            log.error("AGENT falha de contrato | %s", warns)
        raise RuntimeError(f"agent_payload_invalido: {warns}")

    if filter_apt is not None or reasons:
        meta = dict(params.meta)
        if filter_apt is not None:
            meta["filter_apt"] = filter_apt
        if reasons:
            meta["filter_reasons"] = dict(reasons) if isinstance(reasons, dict) else list(reasons)
        params = replace(params, meta=meta)

    if not silent:
        atr_pct = float(metrics.get("atr_pct", 0.0))
        warn_txt = f" Avisos: {warns}" if warns else ""
        prov = str(params.meta.get("source") or src)
        msg = (
            f"=== Agente (contrato v1): {symbol} ===\n"
            f"Proveniência: {prov} | Spread: {params.spread_bps} bps | ordem ~{params.order_size_quote} quote | "
            f"inv max base {params.max_inventory_base} (ATR% ref ~ {atr_pct:.3f}).{warn_txt}\n"
            "==="
        )
        log.info(msg)
    log.debug("AGENT_PARAMS %s", params.to_dict())
    return params
