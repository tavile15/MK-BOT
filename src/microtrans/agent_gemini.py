from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from .agent_contract import AgentSnapshot

try:
    import google.generativeai as genai
except ImportError:
    genai = None  # type: ignore[misc, assignment]

_JSON_BLOCK = re.compile(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", re.DOTALL)

_GEMINI_CORE = """Você é um assistente que define parâmetros de market making em papel (simulação Binance-like).
O JSON de entrada tem:
- metrics / reasons: filtros (ATR%, ADX, liquidez, último preço, etc.).
- execution (quando presente): modo de precificação, taxas, topo do livro, saldos, e uma prévia de ida+volta no book_top.

Devolva APENAS um objeto JSON com exatamente três chaves numéricas:
- spread_bps: em modo **synthetic_mid**, é a meia-largura do spread em torno do mid (bps). Em modo **book_top**,
  o robô executa no melhor bid/ask do livro — este valor ainda importa porque o motor impõe piso de spread/taxas
  e você deve evitar propor spreads triviais que seriam ignorados ou incoerentes com as taxas.
- order_size_quote: tamanho nominal por perna em quote (ex.: USDT). Respeite a liquidez e saldo indicados em execution.
- max_inventory_base: tamanho máximo de posição no ativo base (ex.: BTC), alinhado a risco e ao tamanho de ordem.

Se execution.book_top_roundtrip_preview mostrar would_skip_roundtrip=true ou net_edge negativo em book_top,
prefira **não** aumentar agressividade: volume menor ou spread mais conservador (para referência do piso do motor),
e max_inventory_base modesto — o mercado não oferece edge bruta no topo após taxas.

Use números conservadores se atr_pct for alto ou o filtro indicar tensão.
Não inclua texto fora do JSON."""


def _system_prompt_for_cfg(cfg: dict[str, Any]) -> str:
    acfg = cfg.get("agent") or {}
    bcfg = cfg.get("backtest") or {}
    fee = float(bcfg.get("fee_bps", 10.0))
    min_sp = float(acfg.get("gemini_min_spread_bps", 15.0))
    rules = (
        f"\n\nCRÍTICO — custo, modo de preço e spread:\n"
        f"- Taxa configurada ~{fee:.0f} bps por perna (cada lado paga em quote). Ida + volta ≈ 2× essa ordem de grandeza em custo.\n"
        f"- Se execution.pricing_mode for book_top: preços de compra/venda vêm do livro; olhe execution.touch_spread_bps e "
        f"book_top_roundtrip_preview (net_edge_quote_est). Não assuma lucro só porque atr_pct está baixo.\n"
        f"- Se for synthetic_mid: spread_bps controla diretamente a largura em torno do mid (ainda sujeito a slippage em execution).\n"
        f"- Respeite execution.min_spread_engine_floor_bps e **spread_bps >= {min_sp:.0f}** (piso Gemini pós-resposta). "
        f"Diminuir spread abaixo do piso só piora edges já negativas.\n"
        f"- Se execution.data_source for backtest_synthetic_book, o livro pode ser artificialmente mais largo que o real; "
        f"use-o para calibrar fluxo, não para esperar o mesmo PnL ao vivo.\n"
    )
    return _GEMINI_CORE + rules


def gemini_api_key() -> str | None:
    """Lê só nomes de variáveis de ambiente — nunca coloque a chave literal aqui."""
    a = (os.environ.get("GEMINI_API_KEY") or "").strip()
    b = (os.environ.get("GOOGLE_API_KEY") or "").strip()
    return a or b or None


def _response_text(resp: Any) -> str:
    t = getattr(resp, "text", None) or ""
    if t:
        return str(t).strip()
    cands = getattr(resp, "candidates", None) or []
    if not cands:
        return ""
    parts = getattr(getattr(cands[0], "content", None), "parts", None) or []
    return "".join(getattr(p, "text", "") or "" for p in parts).strip()


def _parse_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    try:
        out = json.loads(text)
    except json.JSONDecodeError:
        m = _JSON_BLOCK.search(text)
        if not m:
            raise ValueError("gemini_json_parse") from None
        out = json.loads(m.group(0))
    if not isinstance(out, dict):
        raise ValueError("gemini_not_object")
    return out


def propose_strategy_gemini(
    snapshot: AgentSnapshot,
    cfg: dict[str, Any],
    logger: logging.Logger | None = None,
    *,
    silent: bool = False,
) -> dict[str, Any]:
    """
    Chama Gemini; devolve dict bruto para `finalize_agent_payload`.
    Levanta RuntimeError em falha de rede, chave ausente ou resposta inválida.
    """
    if genai is None:
        raise RuntimeError("pacote google-generativeai não instalado (pip install google-generativeai)")
    key = gemini_api_key()
    if not key:
        raise RuntimeError("defina GEMINI_API_KEY ou GOOGLE_API_KEY no ambiente")

    acfg = cfg.get("agent") or {}
    model_name = str(acfg.get("gemini_model") or "gemini-2.5-flash")
    log = logger or logging.getLogger("agent")

    genai.configure(api_key=key)
    model = genai.GenerativeModel(model_name)
    user = json.dumps(snapshot.to_llm_payload(), ensure_ascii=False, indent=2)
    prompt = f"{_system_prompt_for_cfg(cfg)}\n\nSnapshot do mercado:\n{user}"
    try:
        resp = model.generate_content(
            prompt,
            generation_config=genai.types.GenerationConfig(
                response_mime_type="application/json",
            ),
        )
    except Exception as e:
        log.warning("Gemini: falha na chamada HTTP/API: %s", e)
        raise RuntimeError(f"gemini_request: {e}") from e

    if not silent:
        log.info(
            "Gemini: 1 requisição concluída (%s) — orçamento: 1 HTTP por abertura de ciclo.",
            model_name,
        )

    raw_t = _response_text(resp)
    if not raw_t:
        raise RuntimeError("gemini_empty_response")

    try:
        data = _parse_json_object(raw_t)
    except (json.JSONDecodeError, ValueError) as e:
        log.warning("Gemini: corpo não é JSON útil (%s): %s", e, raw_t[:500])
        raise RuntimeError(f"gemini_bad_json: {e}") from e

    try:
        return {
            "spread_bps": float(data["spread_bps"]),
            "order_size_quote": float(data["order_size_quote"]),
            "max_inventory_base": float(data["max_inventory_base"]),
        }
    except (KeyError, TypeError, ValueError) as e:
        raise RuntimeError(f"gemini_missing_fields: {e}") from e
