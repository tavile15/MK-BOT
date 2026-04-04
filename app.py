from __future__ import annotations

import copy
import os
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import logging

from microtrans.agent_gemini import gemini_api_key
from microtrans.binance_public import BinancePublic
from microtrans.binance_signed import BinanceSigned
from microtrans.config_loader import load_config
from microtrans.engine import MarketMakingEngine
from microtrans.logging_config import setup_logging
from microtrans.backtest import BINANCE_KLINE_INTERVALS, run_backtest
from microtrans.testnet_order_smoke import run_limit_buy_place_and_cancel
from microtrans.symbols import split_pair
from microtrans.virtual_wallet import VirtualWallet

def _is_transient_network_error(exc: BaseException) -> bool:
    """Falhas HTTP típicas da Binance / rede — vigília não deve encerrar."""
    if isinstance(exc, (ConnectionError, TimeoutError, BrokenPipeError)):
        return True
    try:
        import urllib3.exceptions as u3e

        if isinstance(exc, u3e.HTTPError):
            return True
    except ImportError:
        pass
    try:
        import requests

        if isinstance(exc, requests.exceptions.RequestException):
            return True
    except ImportError:
        pass
    if isinstance(exc, OSError):
        return True
    s = repr(exc).lower()
    return "remotedisconnected" in s or "connection aborted" in s


class StreamlitLogHandler(logging.Handler):
    """Um handler por logger; `_is_microtrans_streamlit` permite remover só o nosso sem derrubar libs."""

    def __init__(self, append_fn):
        super().__init__()
        self._append_fn = append_fn
        self._is_microtrans_streamlit = True

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._append_fn(self.format(record))
        except Exception:
            self.handleError(record)


_BINANCE_CSS = """
<style>
    :root {
      --bn-bg: #0b0e11;
      --bn-panel: #181a20;
      --bn-line: #2b3139;
      --bn-text: #eaecef;
      --bn-muted: #848e9c;
      --bn-up: #0ecb81;
      --bn-down: #f6465d;
      --bn-yellow: #fcd535;
    }
    [data-testid="stAppViewContainer"],
    .stApp {
      background-color: var(--bn-bg) !important;
      color: var(--bn-text) !important;
    }
    [data-testid="stHeader"] { background-color: var(--bn-panel) !important; }
    section[data-testid="stSidebar"] {
      background-color: var(--bn-panel) !important;
      border-right: 1px solid var(--bn-line) !important;
    }
    section[data-testid="stSidebar"] * { color: var(--bn-text) !important; }
    .bn-bar {
      background: var(--bn-panel);
      border: 1px solid var(--bn-line);
      border-radius: 6px;
      padding: 12px 16px;
      margin-bottom: 12px;
      display: flex;
      flex-wrap: wrap;
      gap: 20px;
      align-items: baseline;
    }
    .bn-pair { font-size: 1.35rem; font-weight: 700; color: var(--bn-yellow); }
    .bn-price { font-size: 1.5rem; font-weight: 600; }
    .bn-up { color: var(--bn-up) !important; }
    .bn-down { color: var(--bn-down) !important; }
    .bn-muted { color: var(--bn-muted); font-size: 0.85rem; }
    .bn-stepper {
      display: flex; gap: 8px; flex-wrap: wrap; margin: 12px 0 20px 0;
    }
    .bn-step {
      flex: 1; min-width: 120px; text-align: center;
      padding: 10px 8px; border-radius: 6px; border: 1px solid var(--bn-line);
      background: #1e2329; font-size: 0.85rem;
    }
    .bn-step.ok { border-color: var(--bn-up); color: var(--bn-up); }
    .bn-step.wait { color: var(--bn-muted); }
    .bn-step.run { border-color: var(--bn-yellow); color: var(--bn-yellow); }
    h1, h2, h3, h4 { color: var(--bn-text) !important; }
    .stTabs [data-baseweb="tab-list"] { background: var(--bn-panel); gap: 4px; }
    div[data-testid="stMetricValue"] { color: var(--bn-text) !important; }
    .stButton button[kind="primary"] {
      background-color: var(--bn-yellow) !important; color: #0b0e11 !important; font-weight: 600;
    }
    .bn-wallet-box{
      background: var(--bn-panel);
      border: 1px solid var(--bn-line);
      border-radius: 6px;
      padding: 12px 14px;
      margin-bottom: 12px;
      box-sizing: border-box;
    }
    .bn-wallet-title{
      font-weight: 800;
      color: var(--bn-yellow);
      margin-bottom: 6px;
    }
    .bn-wallet-row{
      display:flex;
      justify-content:space-between;
      gap: 12px;
      font-size: 0.9rem;
      color: var(--bn-text);
      padding: 4px 0;
    }
    .bn-wallet-muted{
      color: var(--bn-muted);
      font-size: 0.82rem;
    }
    .bn-wallet-pnl-up{ color: var(--bn-up) !important; font-weight: 800; }
    .bn-wallet-pnl-down{ color: var(--bn-down) !important; font-weight: 800; }
</style>
"""


def _run() -> None:
    import time
    import pandas as pd
    import streamlit as st
    from datetime import date, datetime, time as time_cls, timedelta, timezone

    st.set_page_config(page_title="Microtrans — painel local", layout="wide", initial_sidebar_state="expanded")
    st.markdown(_BINANCE_CSS, unsafe_allow_html=True)

    def _sync_gemini_env_from_streamlit_secrets() -> None:
        if gemini_api_key():
            return
        try:
            sec = st.secrets
        except Exception:
            return
        for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
            val = sec.get(name)
            if val:
                os.environ[name] = str(val).strip()
                break

    _sync_gemini_env_from_streamlit_secrets()

    def _ensure_state() -> None:
        if "log_lines" not in st.session_state:
            st.session_state.log_lines = []
        if "audit_rows" not in st.session_state:
            st.session_state.audit_rows = []
        if "wallet" not in st.session_state:
            cfg0 = load_config()
            bcfg0 = cfg0["backtest"]
            base0, quote_a0 = split_pair("BTCUSDT")
            st.session_state.wallet = VirtualWallet(
                symbol="BTCUSDT",
                quote_asset=quote_a0,
                base_asset=base0,
                quote_balance=float(bcfg0["initial_quote"]),
                base_balance=0.0,
                fee_bps=float(bcfg0["fee_bps"]),
            )
        if "symbol" not in st.session_state:
            st.session_state.symbol = "BTCUSDT"
        if "engine" not in st.session_state:
            st.session_state.engine = None
        if "relax_liquidity" not in st.session_state:
            st.session_state.relax_liquidity = False
        if "api_ping_ok" not in st.session_state:
            st.session_state.api_ping_ok = False
        if "vigil_active" not in st.session_state:
            st.session_state.vigil_active = False
        if "last_testnet_account" not in st.session_state:
            st.session_state.last_testnet_account = {}
        if "last_testnet_smoke" not in st.session_state:
            st.session_state.last_testnet_smoke = {}
        if "_microtrans_ui_loggers" not in st.session_state:
            st.session_state._microtrans_ui_loggers = False

    def _append_log(msg: str) -> None:
        st.session_state.log_lines = (st.session_state.log_lines + [msg])[-400:]

    def _append_audit_row(out: dict) -> None:
        mid = float(out.get("mid") or 0)
        w = st.session_state.wallet
        sym = st.session_state.symbol
        paper = out.get("paper_step") or {}
        eng_o = out.get("engine") or {}
        liq = None
        if paper.get("ok") and paper.get("net_quote") is not None:
            liq = round(float(paper["net_quote"]), 6)
        det = "—"
        if paper:
            det = str(paper.get("reason") or paper.get("slip_note") or ("ok" if paper.get("ok") else "—"))
        params_audit = eng_o.get("params") or out.get("params") or {}
        meta_a = params_audit.get("meta") if isinstance(params_audit, dict) else {}
        agent_src = str((meta_a or {}).get("source") or "—")

        row = {
            "hora_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "par": sym,
            "apto_filtro": "sim" if out.get("filter_apt") else "não",
            "ciclo_n": eng_o.get("cycle_id"),
            "motor_ligado": "sim" if eng_o.get("active") else "não",
            "agent_fonte": agent_src,
            "preco_mid": round(mid, 2) if mid else None,
            "spread_livro_bps": round(float(out.get("spread_bps") or 0), 3),
            "evento": str(out.get("event") or "—"),
            "fim_ciclo_motivo": str(out.get("cycle_end_reason") or "—"),
            "sim_precificacao": str(paper.get("paper_pricing") or "—"),
            "sim_volta_ok": "sim" if paper.get("ok") else ("não" if paper else "—"),
            "sim_resultado_quote": liq,
            "sim_obs": det,
            "carteira_patrimonio": round(w.equity_quote(mid), 4) if mid > 0 else None,
        }
        st.session_state.audit_rows.append(row)
        st.session_state.audit_rows = st.session_state.audit_rows[-800:]

    _ensure_state()
    _yaml_static = load_config()
    _eng_defaults = dict(_yaml_static.get("engine") or {})
    _agent_defaults = dict(_yaml_static.get("agent") or {})
    setup_logging()
    flog = logging.getLogger("filter")
    alog = logging.getLogger("agent")

    _ui_fmt = logging.Formatter("%(asctime)s | %(name)s | %(message)s")

    def _attach_ui_loggers() -> None:
        for name in ("filter", "agent"):
            lg = logging.getLogger(name)
            # Um único handler de UI (remove StreamHandler de setup_logging e duplicatas).
            lg.handlers.clear()
            h = StreamlitLogHandler(_append_log)
            h.setFormatter(_ui_fmt)
            lg.addHandler(h)
            lg.setLevel(logging.INFO)
            lg.propagate = False

    if not st.session_state.get("_microtrans_ui_loggers"):
        _attach_ui_loggers()
        st.session_state._microtrans_ui_loggers = True

    def _effective_cfg():
        c = copy.deepcopy(load_config())
        if st.session_state.relax_liquidity:
            c["filters"]["relax_liquidity_filter"] = True
        ag = dict(c.get("agent") or {})
        c["agent"] = ag
        _prov = st.session_state.get("ui_agent_provider")
        if _prov:
            ag["provider"] = str(_prov).strip().lower()
        eng = dict(c.get("engine") or {})
        c["engine"] = eng
        # Padrão operacional: book_top (YAML); sem fallback silencioso para synthetic_mid.
        pp = st.session_state.get("ui_paper_pricing") or eng.get("paper_pricing") or "book_top"
        eng["paper_pricing"] = pp
        eng["paper_slippage_bps"] = float(st.session_state.get("ui_paper_slip", eng.get("paper_slippage_bps", 0)))
        eng["paper_fill_probability"] = float(
            st.session_state.get("ui_paper_fill", eng.get("paper_fill_probability", 1.0))
        )
        eng["exit_drawdown_pct"] = float(st.session_state.get("ui_exit_dd", eng.get("exit_drawdown_pct", 3.0)))
        eng["exit_take_profit_pct"] = float(
            st.session_state.get("ui_tp_pct", eng.get("exit_take_profit_pct", 0.0))
        )
        eng["exit_take_profit_quote"] = float(
            st.session_state.get("ui_tp_quote", eng.get("exit_take_profit_quote", 0.0))
        )
        return c

    with st.sidebar:
        st.markdown("### Microtrans")
        st.caption(
            "Simulação local — visual inspirado na Binance (sem afiliação). "
            "**Conta live não recebe ordens.** Opcional: smoke de ordem no Spot Testnet (saldo fictício)."
        )

        with st.expander("Passo a passo (leia primeiro)", expanded=False):
            st.markdown(
                """
1. **Par** — ex.: `BTCUSDT` (texto acima).
2. **Liquidez relaxada** — para BTC/ETH/SOL marque a caixa; senão o filtro costuma bloquear em `liquidity_too_high`.
3. **Um clique** — no painel principal use **“Rodar um tick agora”** (puxa API + filtros + simulação).
4. **Vigília** — barra lateral: duração e intervalo → **Iniciar vigília** (ticks com `st.fragment`; a UI atualiza ~a cada segundo sem “apagão”).
5. **Carteira** — só muda quando a simulação concluir uma “volta” compra+venda; veja coluna `sim_obs` na **Auditoria** se ficar estável.
6. **book_top + deslizamento** — em book muito apertado o sistema **reduz** o deslizamento sozinho; usar **0** é o mais previsível.
                """.strip()
            )

        sym = st.text_input(
            "Par negociado (ex.: BTCUSDT)",
            value=st.session_state.symbol,
            help="Símbolo spot na Binance. Deve terminar em USDT neste protótipo.",
        )
        st.session_state.symbol = sym.upper().strip()

        st.session_state.relax_liquidity = st.checkbox(
            "Permitir pares grandes (relaxar regra de liquidez)",
            value=st.session_state.relax_liquidity,
            help=(
                "Se **desmarcado**, o livro de BTC/SOL costuma passar de 250 mil USDT e o robô diz NÃO APTO "
                "(liquidez alta demais para o filtro). **Marque** para ignorar teto/mínimo só nesse critério — "
                "assim você consegue testar o restante dos filtros e a simulação."
            ),
        )

        _bty = _yaml_static.get("backtest") or {}
        _fc = dict(_yaml_static.get("filters") or {})
        _iv0 = str(_fc.get("kline_interval", "5m"))
        _iv_list = list(BINANCE_KLINE_INTERVALS)
        _iv_ix = _iv_list.index(_iv0) if _iv0 in _iv_list else 2
        _preset_opts = ["(nenhum)"] + list((_bty.get("stress_presets") or {}).keys())

        with st.expander("Backtest — replay histórico (P3b)", expanded=False):
            st.caption(
                "API pública Binance: até **1000** velas por GET; o programa **pagina** em sequência. "
                "Livro no replay continua **sintético** (não é L2 histórico)."
            )
            st.radio(
                "Modo de dados",
                options=["last_n", "range"],
                format_func=lambda x: {
                    "last_n": "Últimas N velas até o término (ou agora)",
                    "range": "Intervalo início → fim (UTC), ex. último mês",
                }.get(x, x),
                index=0,
                key="ui_bt_mode",
                horizontal=True,
            )
            st.selectbox(
                "Intervalo da vela (replay)",
                options=_iv_list,
                index=_iv_ix,
                key="ui_bt_interval",
            )
            st.number_input(
                "Passo do replay (1 = usar cada barra)",
                min_value=1,
                max_value=500,
                value=1,
                key="ui_bt_step",
            )
            st.selectbox(
                "Preset de stress (opcional)",
                options=_preset_opts,
                key="ui_bt_preset",
            )
            st.checkbox(
                "Backtest: forçar heurística (não chamar Gemini nesta corrida)",
                key="ui_bt_force_heuristic",
                help=(
                    "Tu continuas a escolher **Provedor** Gemini ou heurística para tick/vigília. "
                    "Isto só substitui o agente por heurística **durante** o replay, para economizar API."
                ),
            )
            st.text_input(
                "Par do backtest (vazio = mesmo do painel)",
                value="",
                key="ui_bt_symbol",
                placeholder=str(st.session_state.symbol or "BTCUSDT"),
            )
            if str(st.session_state.get("ui_bt_mode") or "last_n") == "range":
                _now_u = datetime.now(timezone.utc)
                _start_def = (_now_u - timedelta(days=30)).date()
                st.date_input(
                    "Início intervalo — data (UTC)",
                    value=_start_def,
                    key="ui_bt_r_start_d",
                )
                st.time_input(
                    "Início intervalo — hora (UTC)",
                    value=time_cls(0, 0, 0),
                    key="ui_bt_r_start_t",
                )
                st.date_input(
                    "Fim intervalo — data (UTC)",
                    value=_now_u.date(),
                    key="ui_bt_r_end_d",
                )
                st.time_input(
                    "Fim intervalo — hora (UTC)",
                    value=time_cls(_now_u.hour, _now_u.minute, _now_u.second),
                    key="ui_bt_r_end_t",
                )
                st.caption(
                    f"Teto **{int(_bty.get('max_bars', 20000))}** velas (`backtest.max_bars`). "
                    "Se o intervalo couber mais velas, o excesso é cortado e `capped_at_max_rows` aparece no sumário."
                )
            else:
                st.number_input(
                    "Barras (N)",
                    min_value=30,
                    max_value=int(_bty.get("max_bars", 20000)),
                    value=int(_bty.get("default_bars", 500)),
                    step=10,
                    key="ui_bt_bars",
                )
                st.checkbox(
                    "Fixar instante final da janela (UTC)",
                    key="ui_bt_fix_end",
                    help=(
                        "Desmarcado: a última vela é a mais recente disponível **no momento** do clique. "
                        "Marcado: usa data/hora abaixo — mesma janela ao repetir o backtest."
                    ),
                )
                if st.session_state.get("ui_bt_fix_end"):
                    _now_utc = datetime.now(timezone.utc)
                    st.date_input(
                        "Data término (UTC)",
                        value=_now_utc.date(),
                        key="ui_bt_end_d",
                    )
                    st.time_input(
                        "Hora término (UTC)",
                        value=time_cls(_now_utc.hour, _now_utc.minute, _now_utc.second),
                        key="ui_bt_end_t",
                    )

        with st.expander("P4t — Spot Testnet (saldo fictício)", expanded=False):
            _ex = dict((_yaml_static.get("execution") or {}))
            _tn_base = str(_ex.get("testnet_base_url") or "https://testnet.binance.vision")
            st.caption(
                "Validação de execução na Binance Testnet: API assinada + ordem LIMIT e cancelamento. "
                "Isto **não** envia ordens na conta live."
            )
            st.caption(f"Base URL: `{_tn_base}`")
            st.text_input(
                "Par do smoke testnet",
                value="BTCUSDT",
                key="ui_tn_symbol",
                help="Par spot para validação (ex.: BTCUSDT).",
            )
            st.number_input(
                "Ticks abaixo do melhor bid (order-smoke)",
                min_value=1,
                max_value=500,
                value=20,
                step=1,
                key="ui_tn_ticks_below",
                help="Maior valor = menor chance de execução imediata antes do cancel.",
            )
            c_t1, c_t2 = st.columns(2)
            with c_t1:
                do_tn_account = st.button(
                    "Testnet: consultar conta",
                    use_container_width=True,
                    key="ui_btn_tn_account",
                )
            with c_t2:
                do_tn_smoke = st.button(
                    "Testnet: order-smoke",
                    use_container_width=True,
                    key="ui_btn_tn_smoke",
                )

        page = st.radio(
            "Menu",
            ["Painel", "Mercado", "Carteira", "Backtest", "Testnet", "Logs", "Auditoria"],
            label_visibility="collapsed",
        )

        _ap_def = str(_agent_defaults.get("provider") or "heuristic").strip().lower()
        _ap_ix = 1 if _ap_def == "gemini" else 0
        st.markdown("##### Agente (P3)")
        st.selectbox(
            "Provedor",
            options=["heuristic", "gemini"],
            index=_ap_ix,
            format_func=lambda x: {
                "heuristic": "Heurística local (sem API)",
                "gemini": "Google Gemini",
            }.get(x, x),
            key="ui_agent_provider",
            help=(
                "**Gemini** exige `GEMINI_API_KEY` ou `GOOGLE_API_KEY` no ambiente. "
                "Modelo: `agent.gemini_model` no YAML. Saída passa por `finalize_agent_payload`."
            ),
        )
        st.caption(
            "Sem chave, Gemini cai automaticamente na heurística (veja aviso nos Logs)."
        )
        if str(st.session_state.get("ui_agent_provider") or "").strip().lower() == "gemini" and not gemini_api_key():
            st.warning(
                "Chave Gemini não encontrada. Opções: variável `GEMINI_API_KEY` (ou `GOOGLE_API_KEY`) "
                "antes de iniciar o Streamlit, ou arquivo `.streamlit/secrets.toml` com `GEMINI_API_KEY = \"...\"`."
            )

        st.markdown("##### Como precificamos a simulação")
        _pp_def = str(_eng_defaults.get("paper_pricing", "book_top")).strip().lower()
        _pp_idx = 1 if _pp_def in ("book_top", "book", "touch") else 0
        st.selectbox(
            "Modo de preço (só papel)",
            options=["synthetic_mid", "book_top"],
            format_func=lambda x: {
                "synthetic_mid": "Cálculo no meio do spread (mais simples)",
                "book_top": "Topo do livro real (mais realista, spread fino)",
            }.get(x, x),
            index=_pp_idx,
            key="ui_paper_pricing",
            help=(
                "**Topo do livro real**: compra no melhor preço de compra e vende no melhor de venda do snapshot da API "
                "(spread real, muitas vezes apertado). **Meio do spread**: usa o preço médio ± spread definido pelo agente."
            ),
        )
        st.number_input(
            "Deslizamento extra (em bps — 1 bps = 0,01%)",
            min_value=0.0,
            max_value=200.0,
            value=float(_eng_defaults.get("paper_slippage_bps", 0.0)),
            step=0.5,
            key="ui_paper_slip",
            help="Piora de propósito o preço de compra/venda na simulação. Em book_top, valores altos são cortados automaticamente se o livro for apertado.",
        )
        st.slider(
            "Chance de executar cada volta simulada (1 = sempre)",
            min_value=0.0,
            max_value=1.0,
            value=float(_eng_defaults.get("paper_fill_probability", 1.0)),
            step=0.05,
            key="ui_paper_fill",
            help="Abaixo de 1, alguns ticks pulam a volta; o sorteio é fixo por tick (repetível).",
        )
        st.markdown("##### Limite do ciclo (enquanto o robô está ligado)")
        st.number_input(
            "Parar se perder X% desde o início deste ciclo (stop)",
            min_value=0.1,
            max_value=90.0,
            value=float(_eng_defaults.get("exit_drawdown_pct", 3.0)),
            step=0.25,
            key="ui_exit_dd",
            help="Compara o patrimônio atual com o patrimônio gravado quando o ciclo começou (não é stop de bolsa real).",
        )
        st.number_input(
            "Parar se ganhar X% sobre o início deste ciclo (meta %)",
            min_value=0.0,
            max_value=500.0,
            value=float(_eng_defaults.get("exit_take_profit_pct", 0.0)),
            step=0.1,
            key="ui_tp_pct",
            help="0 = não usar esta regra. Ex.: 10 = encerra o ciclo quando lucro ≥ 10% do patrimônio inicial do ciclo.",
        )
        st.number_input(
            "Parar ao lucrar X USDT neste ciclo (meta em dinheiro)",
            min_value=0.0,
            max_value=1e12,
            value=float(_eng_defaults.get("exit_take_profit_quote", 0.0)),
            step=1.0,
            key="ui_tp_quote",
            help="0 = não usar. Ex.: 50 = encerra quando (patrimônio − início do ciclo) ≥ 50 USDT.",
        )

        st.markdown("##### Vigília automática")
        st.caption("Repete ticks sozinho. Útil com filtros; **não** coloca ordens na corretora.")
        st.number_input(
            "Quanto tempo rodar (segundos)",
            min_value=10,
            max_value=7200,
            value=120,
            step=10,
            key="ui_auto_dur",
        )
        st.number_input(
            "Pausa entre cada consulta (segundos)",
            min_value=2,
            max_value=600,
            value=15,
            step=1,
            key="ui_auto_step",
        )
        run_auto = st.button("Iniciar vigília", type="primary", use_container_width=True, key="ui_btn_vigil")
        if st.session_state.get("vigil_active"):
            if st.button("Parar vigília", use_container_width=True, key="ui_btn_vigil_stop"):
                st.session_state.vigil_active = False

    cfg = _effective_cfg()
    if st.sidebar.button("Ping API Binance"):
        try:
            BinancePublic().ping()
            st.session_state.api_ping_ok = True
            st.sidebar.success("Conexão estável.")
        except Exception as e:
            st.session_state.api_ping_ok = False
            st.sidebar.error(str(e))

    st.session_state.symbol = st.session_state.symbol.strip().upper()
    if st.session_state.wallet.symbol != st.session_state.symbol:
        bcfg = cfg["backtest"]
        base, quote_a = split_pair(st.session_state.symbol)
        st.session_state.wallet = VirtualWallet(
            symbol=st.session_state.symbol,
            quote_asset=quote_a,
            base_asset=base,
            quote_balance=float(bcfg["initial_quote"]),
            base_balance=0.0,
            fee_bps=float(bcfg["fee_bps"]),
        )
        st.session_state.engine = None
        st.session_state.pop("_ecfg_key", None)
        st.session_state.last_out = {}
        st.session_state.audit_rows = []
        st.session_state.log_lines = []
        st.session_state.vigil_active = False

    ecfg_key = (
        st.session_state.symbol,
        st.session_state.relax_liquidity,
        str(st.session_state.get("ui_agent_provider") or _agent_defaults.get("provider") or "heuristic"),
        str(st.session_state.get("ui_paper_pricing") or "book_top"),
        round(float(st.session_state.get("ui_paper_slip", 0.0)), 8),
        round(float(st.session_state.get("ui_paper_fill", 1.0)), 6),
        round(float(st.session_state.get("ui_exit_dd", 3.0)), 6),
        round(float(st.session_state.get("ui_tp_pct", 0.0)), 6),
        round(float(st.session_state.get("ui_tp_quote", 0.0)), 6),
    )
    if st.session_state.engine is None or st.session_state.get("_ecfg_key") != ecfg_key:
        st.session_state.engine = MarketMakingEngine(
            st.session_state.symbol,
            st.session_state.wallet,
            cfg=cfg,
            filter_logger=flog,
            agent_logger=alog,
        )
        st.session_state._ecfg_key = ecfg_key

    eng: MarketMakingEngine = st.session_state.engine

    st.info(
        "**Lembrete:** tudo aqui é **simulação** (papel). A carteira muda quando a coluna `sim_volta_ok` na Auditoria "
        "aparecer como **sim** e `sim_resultado_quote` tiver valor — se o livro for muito apertado ou filtros barrarem, "
        "pode permanecer 0 operações."
    )

    c_act1, c_act2, c_act3 = st.columns([1, 1, 1])
    with c_act1:
        do_tick = st.button("Rodar um tick agora", type="primary", use_container_width=True, help="Uma consulta completa à API + filtros + motor em papel.")
    with c_act2:
        do_bt = st.button("Rodar backtest", use_container_width=True)
    with c_act3:
        if st.button("Resetar carteira UI", use_container_width=True):
            bcfg = cfg["backtest"]
            base, quote_a = split_pair(st.session_state.symbol)
            st.session_state.wallet = VirtualWallet(
                symbol=st.session_state.symbol,
                quote_asset=quote_a,
                base_asset=base,
                quote_balance=float(bcfg["initial_quote"]),
                base_balance=0.0,
                fee_bps=float(bcfg["fee_bps"]),
            )
            st.session_state.engine = None
            st.session_state.pop("_ecfg_key", None)
            _append_log("Carteira da UI reiniciada.")

    st.caption("Vigília na barra lateral: atualiza UI incrementalmente (Streamlit ≥ 1.33 com `fragment`).")

    if run_auto:
        dur = float(st.session_state.get("ui_auto_dur", 120))
        t0 = time.time()
        st.session_state.vigil_active = True
        st.session_state.vigil_until = t0 + dur
        st.session_state.vigil_start_ts = t0
        st.session_state._vigil_last_tick_ts = 0.0
        st.session_state._vigil_ntick = 0

    _frag = getattr(st, "fragment", None)
    if _frag is not None:

        @_frag(run_every=timedelta(seconds=1))
        def _vigil_fragment() -> None:
            ss = st.session_state
            if not ss.get("vigil_active"):
                return
            now = time.time()
            bo = float(ss.get("_vigil_backoff_until") or 0.0)
            if bo > now:
                st.caption(f"Rede em recuperação · retomando em ~{bo - now:.0f}s")
                return
            until = float(ss["vigil_until"])
            start_ts = float(ss.get("vigil_start_ts", now))
            span = max(1.0, until - start_ts)
            st.progress(min(1.0, max(0.0, (now - start_ts) / span)))
            if now >= until:
                if ss.get("vigil_active"):
                    ss["vigil_active"] = False
                    n = int(ss.get("_vigil_ntick", 0))
                    st.success(
                        f"Vigência encerrada: {n} ticks. **Nenhuma ordem real** — só simulação local."
                    )
                return
            step_iv = max(1.0, float(ss.get("ui_auto_step", 15)))
            last = float(ss.get("_vigil_last_tick_ts", 0.0))
            if last > 0.0 and (now - last) < step_iv:
                st.caption(
                    f"Vigília ativa · ticks: {ss.get('_vigil_ntick', 0)} · próximo ciclo em ~{step_iv - (now - last):.0f}s"
                )
                return
            e = ss.get("engine")
            if e is None:
                ss["vigil_active"] = False
                st.error("Motor indisponível — vigília parada.")
                return
            try:
                out = e.tick(silent_logs=True)
                ss["last_out"] = out
                _append_audit_row(out)
                ss["_vigil_ntick"] = int(ss.get("_vigil_ntick", 0)) + 1
                ss["_vigil_last_tick_ts"] = now
                ss.pop("_vigil_backoff_until", None)
                mid_o = float(out.get("mid") or 0)
                _append_log(
                    f"VIGIL | n={ss['_vigil_ntick']} apt={'sim' if out.get('filter_apt') else 'não'} | "
                    f"{ss['wallet'].explain(mid_o)}"
                )
            except Exception as ex:
                if _is_transient_network_error(ex):
                    logging.getLogger("filter").warning(
                        "Vigília: falha transitória de rede (%s) — nova tentativa após ~5s.",
                        ex,
                    )
                    _append_log(f"VIGIL | rede (avisar): {ex!r} — pausa ~5s")
                    ss["_vigil_backoff_until"] = now + 5.0
                    return
                ss["vigil_active"] = False
                _append_log(f"ERRO vigília: {ex}")
                st.error(str(ex))
                return
            st.caption(
                f"Vigília · tick {ss.get('_vigil_ntick', 0)} · papel · API (log 1:1 na aba Logs)"
            )

        _vigil_fragment()
    elif st.session_state.get("vigil_active"):
        st.warning(
            "Para vigília sem travar a UI, atualize: `pip install -U streamlit` (≥ 1.33). "
            "Desligando vigília nesta sessão."
        )
        st.session_state.vigil_active = False

    if do_tick:
        with st.spinner("Coletando cotação…"):
            try:
                out = eng.tick()
                st.session_state.last_out = out
                _append_audit_row(out)
                _append_log(
                    f"TICK | apt={'sim' if out.get('filter_apt') else 'não'} | "
                    f"{st.session_state.wallet.explain(float(out.get('mid') or 0))}"
                )
            except Exception as e:
                if _is_transient_network_error(e):
                    logging.getLogger("filter").warning("Tick manual: rede instável (%s). Tente novamente.", e)
                    st.warning(f"Rede instável — tente novamente em instantes. ({e})")
                    _append_log(f"TICK | rede: {e!r}")
                else:
                    st.error(str(e))
                    _append_log(f"ERRO tick: {e}")

    if do_bt:
        with st.spinner("Backtest…"):
            try:
                _bdef = _yaml_static.get("backtest") or {}
                _fc_bt = dict(_yaml_static.get("filters") or {})
                _iv_bt_def = str(_fc_bt.get("kline_interval", "5m"))
                _raw_bt_sym = str(st.session_state.get("ui_bt_symbol") or "").strip().upper()
                sym_bt = _raw_bt_sym or st.session_state.symbol
                _pr_bt = str(st.session_state.get("ui_bt_preset") or "(nenhum)")
                preset_bt = None if _pr_bt == "(nenhum)" else _pr_bt
                _force_h = bool(st.session_state.get("ui_bt_force_heuristic"))
                _mode = str(st.session_state.get("ui_bt_mode") or "last_n")
                _skip = False
                _start_ms_bt: int | None = None
                _end_ms_bt: int | None = None
                if _mode == "range":
                    _ds = st.session_state.get("ui_bt_r_start_d")
                    _ts = st.session_state.get("ui_bt_r_start_t")
                    _de = st.session_state.get("ui_bt_r_end_d")
                    _te = st.session_state.get("ui_bt_r_end_t")
                    if isinstance(_ts, datetime):
                        _ts = _ts.time()
                    if isinstance(_te, datetime):
                        _te = _te.time()
                    if (
                        isinstance(_ds, date)
                        and isinstance(_ts, time_cls)
                        and isinstance(_de, date)
                        and isinstance(_te, time_cls)
                    ):
                        _start_ms_bt = int(
                            datetime.combine(_ds, _ts, tzinfo=timezone.utc).timestamp() * 1000
                        )
                        _end_ms_bt = int(
                            datetime.combine(_de, _te, tzinfo=timezone.utc).timestamp() * 1000
                        )
                    else:
                        _skip = True
                        st.error("Defina início e fim do intervalo (data + hora UTC).")
                        _append_log("BACKTEST cancelado: intervalo incompleto.")
                else:
                    _end_ms_bt = None
                    if st.session_state.get("ui_bt_fix_end"):
                        _d = st.session_state.get("ui_bt_end_d")
                        _t = st.session_state.get("ui_bt_end_t")
                        if isinstance(_t, datetime):
                            _t = _t.time()
                        if isinstance(_d, date) and isinstance(_t, time_cls):
                            _end_dt = datetime.combine(_d, _t, tzinfo=timezone.utc)
                            _end_ms_bt = int(_end_dt.timestamp() * 1000)
                if not _skip:
                    res = run_backtest(
                        sym_bt,
                        cfg=cfg,
                        bars=int(st.session_state.get("ui_bt_bars", _bdef.get("default_bars", 500))),
                        step_every=int(st.session_state.get("ui_bt_step", 1)),
                        kline_interval=str(st.session_state.get("ui_bt_interval", _iv_bt_def)),
                        start_time_ms=_start_ms_bt,
                        end_time_ms=_end_ms_bt,
                        stress_preset=preset_bt,
                        force_agent_heuristic=_force_h,
                    )
                    st.session_state.last_bt = res
                    if res.get("ok"):
                        _rid = res.get("bt_run_id", "")
                        _append_log(
                            f"BACKTEST ok | run={_rid} | {res.get('closing_explanation', '')}"
                        )
                    else:
                        _append_log(f"BACKTEST falhou | {res}")
            except Exception as e:
                st.error(str(e))
                _append_log(f"ERRO backtest: {e}")

    if do_tn_account or do_tn_smoke:
        with st.spinner("Spot Testnet…"):
            try:
                _ex = dict((load_config().get("execution") or {}))
                _tn_base = str(_ex.get("testnet_base_url") or "https://testnet.binance.vision")
                tpub = BinancePublic(base_url=_tn_base)
                tsigned = BinanceSigned.from_env(require_testnet=True)
                if do_tn_account:
                    acc = tsigned.account()
                    bals = acc.get("balances") or []
                    non_zero = [
                        b
                        for b in bals
                        if float(b.get("free", 0) or 0) > 0
                        or float(b.get("locked", 0) or 0) > 0
                    ]
                    out_acc = {
                        "ok": True,
                        "base_url": _tn_base,
                        "canTrade": acc.get("canTrade"),
                        "makerCommission": acc.get("makerCommission"),
                        "takerCommission": acc.get("takerCommission"),
                        "balances_total": len(bals),
                        "balances_non_zero": non_zero[:40],
                    }
                    st.session_state.last_testnet_account = out_acc
                    _append_log(
                        "TESTNET account ok | "
                        f"canTrade={out_acc.get('canTrade')} | balances_non_zero={len(non_zero)}"
                    )
                if do_tn_smoke:
                    sym_tn = str(st.session_state.get("ui_tn_symbol") or "BTCUSDT").strip().upper()
                    tk_below = int(st.session_state.get("ui_tn_ticks_below", 20))
                    out_smoke = run_limit_buy_place_and_cancel(
                        sym_tn,
                        pub=tpub,
                        signed=tsigned,
                        ticks_below_best_bid=tk_below,
                    )
                    st.session_state.last_testnet_smoke = out_smoke
                    if out_smoke.get("ok"):
                        _append_log(
                            "TESTNET order-smoke ok | "
                            f"{sym_tn} | price={out_smoke.get('price')} qty={out_smoke.get('quantity')}"
                        )
                    else:
                        _append_log(f"TESTNET order-smoke falhou | {out_smoke}")
            except Exception as e:
                st.error(str(e))
                _append_log(f"ERRO testnet: {e}")

    last = st.session_state.get("last_out") or {}
    m24 = last.get("market_24h") or {}
    chg = m24.get("variacao_pct_24h")
    mid = float(last.get("mid") or 0)
    chg_cls = "bn-up" if (chg is not None and float(chg) >= 0) else "bn-down"
    chg_txt = f"{float(chg):+.2f}%" if chg is not None else "—"
    volq = m24.get("volume_quote_24h")
    vol_txt = f"{float(volq):,.0f}" if volq is not None else "—"
    mid_disp = f"{mid:,.2f}" if mid > 0 else "—"

    # Carteira simulada (UI) — coluna à direita da barra de mercado (sem overlay)
    wmid = mid if mid > 0 else 1.0
    w = st.session_state.wallet
    sm = w.summary(wmid)
    pnl = float(sm.get("pnl_vs_inicio_quote", 0.0))
    pnl_cls = "bn-wallet-pnl-up" if pnl >= 0 else "bn-wallet-pnl-down"
    carteira_box = f"""
    <div class="bn-wallet-box">
      <div class="bn-wallet-title">Saldo Estimado</div>
      <div class="bn-wallet-row"><span class="bn-wallet-muted">Quote</span><span>{sm.get("ativo_quote","QUOTE")}: {sm.get("quote_saldo",0):,.4f}</span></div>
      <div class="bn-wallet-row"><span class="bn-wallet-muted">Base</span><span>{sm.get("ativo_base","BASE")}: {sm.get("base_saldo",0):,.8f}</span></div>
      <div class="bn-wallet-row"><span class="bn-wallet-muted">Patrimônio</span><span>{sm.get("patrimonio_quote",0):,.4f} {sm.get("ativo_quote","QUOTE")}</span></div>
      <div class="bn-wallet-row"><span class="bn-wallet-muted">PnL</span><span class="{pnl_cls}">{pnl:+,.4f}</span></div>
      <div class="bn-wallet-row"><span class="bn-wallet-muted">Taxas (acc.)</span><span>{sm.get("taxas_pagas_total_quote",0):,.4f}</span></div>
      <div class="bn-wallet-row"><span class="bn-wallet-muted">Ops</span><span>{sm.get("n_operacoes",0)}</span></div>
    </div>
    """

    bar_html = f"""
    <div class="bn-bar">
      <div><span class="bn-pair">{st.session_state.symbol}</span>
        <span class="bn-muted"> · spot</span></div>
      <div class="bn-price {chg_cls}">{mid_disp} USDT</div>
      <div class="bn-muted">24h &nbsp;
        <span class="{chg_cls}">{chg_txt}</span></div>
      <div class="bn-muted">High {m24.get("high_24h", "—")} · Low {m24.get("low_24h", "—")}</div>
      <div class="bn-muted">Vol 24h (quote) {vol_txt}</div>
      <div class="bn-muted">API: {'ok' if st.session_state.api_ping_ok else '—'}</div>
    </div>
    """
    c_bar, c_wallet = st.columns([2.05, 0.95], gap="medium")
    with c_bar:
        st.markdown(bar_html, unsafe_allow_html=True)
    with c_wallet:
        st.markdown(carteira_box, unsafe_allow_html=True)

    eng_state = last.get("engine") or {}
    step1 = bool(last)
    step2 = bool(last.get("filter_apt"))
    step3 = bool(eng_state.get("params") or last.get("event") == "cycle_start")
    step4 = bool(eng_state.get("active"))
    s1c, s2c, s3c, s4c = ("ok" if step1 else "wait"), ("ok" if step2 else "wait"), ("run" if step3 else "wait"), ("ok" if step4 else "wait")
    st.markdown(
        f"""
        <div class="bn-stepper">
          <div class="bn-step {s1c}">1 · Dados & book</div>
          <div class="bn-step {s2c}">2 · Filtros matemáticos</div>
          <div class="bn-step {s3c}">3 · Agente (parâmetros)</div>
          <div class="bn-step {s4c}">4 · Motor papel</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if page == "Painel":
        if not last:
            st.info("Use **Rodar um tick agora** para carregar o mercado e preencher o painel.")
        else:
            apt = last.get("filter_apt")
            st.markdown("#### Resumo")
            if apt:
                st.success("Mercado **apto** — filtros (com as regras atuais) permitem estratégia.")
            else:
                st.error("Mercado **não apto** — veja critérios abaixo ou ative relaxação de liquidez na barra lateral.")

            m = last.get("metrics") or {}
            g1, g2, g3, g4 = st.columns(4)
            g1.metric("Preço (mid)", f"{float(last.get('mid') or 0):,.2f}")
            g2.metric("Spread (bps)", f"{float(last.get('spread_bps') or 0):.2f}")
            g3.metric("ADX", f"{float(m.get('adx', 0)):.2f}")
            g4.metric("Range/ATR", f"{float(m.get('range_atr_ratio', 0)):.2f}")

            rows = last.get("filter_diagnostic") or []
            if rows:
                st.markdown("#### Critérios (tabela)")
                st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

            st.markdown("#### Diagnóstico textual")
            st.text(last.get("filter_human") or "")

            st.markdown("#### Motor (JSON compacto)")
            _engx = cfg.get("engine") or {}
            st.caption(
                f"Papel (barra lateral + YAML): `paper_pricing={_engx.get('paper_pricing', 'book_top')}`, "
                f"`paper_slippage_bps={_engx.get('paper_slippage_bps', 0)}`, "
                f"`paper_fill_probability={_engx.get('paper_fill_probability', 1.0)}` · "
                f"SL ciclo {_engx.get('exit_drawdown_pct', 3)}% · "
                f"TP {_engx.get('exit_take_profit_pct', 0) or 0}% / { _engx.get('exit_take_profit_quote', 0) or 0} quote"
            )
            st.json(
                {
                    "evento": last.get("event"),
                    "motor": eng_state,
                    "papel": last.get("paper_step"),
                }
            )
            prm = (eng_state.get("params") or {}) if eng_state else {}
            if prm:
                with st.expander("Contrato do agente (v1) — parâmetros validados"):
                    st.caption(
                        "Campos obrigatórios no payload: spread_bps, order_size_quote, max_inventory_base. "
                        "Limites em `config/default.yaml` → `agent.bounds`."
                    )
                    st.json(prm)

    elif page == "Mercado":
        st.markdown("#### Livro de ordens (top níveis — snapshot do último tick)")
        if not last.get("book_preview"):
            st.caption("Faça um tick para carregar o book.")
        else:
            bp = last["book_preview"]
            bc, ac = st.columns(2)
            with bc:
                st.caption("Bids (compra)")
                st.dataframe(
                    pd.DataFrame(bp.get("bids") or [], columns=["Preço", "Qtd base"]),
                    use_container_width=True,
                    hide_index=True,
                )
            with ac:
                st.caption("Asks (venda)")
                st.dataframe(
                    pd.DataFrame(bp.get("asks") or [], columns=["Preço", "Qtd base"]),
                    use_container_width=True,
                    hide_index=True,
                )
        st.markdown("#### Indicadores 24h (API)")
        st.json(last.get("market_24h") or {})

    elif page == "Carteira":
        w = st.session_state.wallet
        wmid = float(last.get("mid") or 0) if last else 0.0
        sm = w.summary(wmid if wmid > 0 else 1.0)
        st.markdown("#### Carteira simulada (somente papel da UI)")
        k1, k2, k3, k4 = st.columns(4)
        k1.metric(f"Saldo {sm.get('ativo_quote')}", f"{sm.get('quote_saldo', 0):,.4f}")
        k2.metric(f"Saldo {sm.get('ativo_base')}", f"{sm.get('base_saldo', 0):.8f}")
        k3.metric("Patrimônio", f"{sm.get('patrimonio_quote', 0):,.4f}")
        k4.metric("PnL vs início", f"{sm.get('pnl_vs_inicio_quote', 0):+,.4f}")
        k5, k6, k7 = st.columns(3)
        k5.metric("Taxas", f"{sm.get('taxas_pagas_total_quote', 0):,.4f}")
        k6.metric("Volume tradeado (quote)", f"{sm.get('volume_quote_negociado', 0):,.2f}")
        k7.metric("Taxa média / op.", f"{sm.get('taxa_media_por_operacao', 0):.6f}")
        st.caption(w.explain(wmid if wmid > 0 else 1.0))
        trades = w.recent_trades(40)
        if trades:
            st.dataframe(pd.DataFrame(trades), use_container_width=True, hide_index=True)
        csv_data = w.trades_to_csv()
        st.download_button(
            "Exportar CSV",
            data=csv_data,
            file_name=f"trades_{st.session_state.symbol}.csv",
            mime="text/csv",
        )

    elif page == "Backtest":
        st.markdown("#### Backtest (replay P3b)")
        st.caption(
            "Auditoria **só do replay** (CSV/JSONL) é distinta da aba **Auditoria** (ticks ao vivo / vigília). "
            "Configure par, N barras e intervalo na barra lateral → **Backtest — replay histórico**."
        )
        bt = st.session_state.get("last_bt") or {}
        if not bt:
            st.info("Clique em **Rodar backtest** no painel principal. A carteira do backtest é independente da carteira da UI.")
        else:
            st.markdown(bt.get("closing_explanation", ""))
            st.info(bt.get("nota_determinismo", ""))
            rs = bt.get("replay_spec") or {}
            _fim_lbl = rs.get("end_time_utc_iso") or (
                "agora (no clique)" if rs.get("fetch_mode") == "last_n" else "—"
            )
            st.caption(
                f"`bt_run_id` **{bt.get('bt_run_id', '—')}** · modo **{rs.get('fetch_mode', '—')}** · "
                f"{rs.get('symbol', '—')} · vela **{rs.get('kline_interval', '—')}** · "
                f"N pedido **{rs.get('bars_requested', '—')}** · barras_df **{rs.get('bars_fetched', '—')}** · "
                f"UTC início **{rs.get('start_time_utc_iso') or '—'}** · fim **{_fim_lbl}** · "
                f"heurística_forçada **{rs.get('force_agent_heuristic', False)}** · preset **{rs.get('stress_preset') or '—'}**"
            )
            summ = bt.get("summary") or {}
            if summ:
                s1, s2, s3, s4, s5 = st.columns(5)
                s1.metric("Passos replay", str(summ.get("replay_steps", "—")))
                s2.metric("% ticks apto", f"{summ.get('pct_steps_filter_apt', 0):.2f}%")
                s3.metric("Voltas papel OK", str(summ.get("paper_roundtrip_ok", "—")))
                s4.metric("Aberturas ciclo", str(summ.get("cycle_start_events", "—")))
                s5.metric("PnL vs início (quote)", f"{summ.get('pnl_quote_vs_start', 0):+.6f}")
                with st.expander("Diagnóstico: papel e ciclos (sem abrir o CSV)", expanded=False):
                    st.markdown("**Falhas / skips de ida+volta** (`paper_ok=false`) por motivo:")
                    st.json(summ.get("paper_failure_reason_counts") or {})
                    st.markdown("**Eventos `cycle_end`** por motivo:")
                    st.json(summ.get("cycle_end_reason_counts") or {})
                    if summ.get("klines_fetch"):
                        st.markdown("**Metadados do carregamento de velas:**")
                        st.json(summ.get("klines_fetch"))
            fw = bt.get("final_wallet") or {}
            b1, b2, b3, b4 = st.columns(4)
            b1.metric("Passos", str(bt.get("steps", "—")))
            b2.metric("Ciclos motor", str(bt.get("motor_cycle_counter", "—")))
            b3.metric("Operações", str(fw.get("n_operacoes", "—")))
            b4.metric("Patrimônio final", f"{fw.get('patrimonio_quote', 0):,.4f}")
            ap = bt.get("artifact_paths") or {}
            if ap.get("audit_csv_path"):
                st.caption(f"Ficheiros gravados: `{ap.get('audit_csv_path')}` · `{ap.get('agent_jsonl_path', '')}`")
            _bt_skip_json = {"history_tail", "audit_csv", "agent_decisions_jsonl"}
            st.json({k: v for k, v in bt.items() if k not in _bt_skip_json})
            _ac = bt.get("audit_csv") or ""
            _aj = bt.get("agent_decisions_jsonl") or ""
            d1, d2 = st.columns(2)
            with d1:
                st.download_button(
                    "Descarregar auditoria do backtest (CSV)",
                    data=_ac.encode("utf-8-sig"),
                    file_name=f"bt_{bt.get('bt_run_id', 'run')}_audit.csv",
                    mime="text/csv",
                    key="dl_bt_audit",
                    disabled=not _ac,
                )
            with d2:
                st.download_button(
                    "Descarregar decisões do agente (JSONL)",
                    data=_aj.encode("utf-8"),
                    file_name=f"bt_{bt.get('bt_run_id', 'run')}_agent.jsonl",
                    mime="text/plain",
                    key="dl_bt_agent",
                    disabled=not _aj,
                )

    elif page == "Testnet":
        st.markdown("#### Spot Testnet (P4t)")
        st.caption(
            "Esta aba usa API assinada da Binance Testnet (saldo fictício) para validar execução. "
            "Não afeta a conta live."
        )
        st.info(
            "Use os botões na barra lateral (expander **P4t — Spot Testnet**) para consultar conta e rodar "
            "order-smoke (LIMIT + cancel)."
        )
        acc = st.session_state.get("last_testnet_account") or {}
        smk = st.session_state.get("last_testnet_smoke") or {}
        if acc:
            st.markdown("##### Última consulta de conta")
            c1, c2, c3 = st.columns(3)
            c1.metric("canTrade", str(acc.get("canTrade")))
            c2.metric("Saldos (total)", str(acc.get("balances_total", "—")))
            c3.metric("Maker/Taker", f"{acc.get('makerCommission', '—')} / {acc.get('takerCommission', '—')}")
            st.json(acc)
        else:
            st.caption("Ainda sem consulta de conta nesta sessão.")
        if smk:
            st.markdown("##### Último order-smoke")
            st.json(smk)
        else:
            st.caption("Ainda sem order-smoke nesta sessão.")

    elif page == "Auditoria":
        st.markdown("#### Histórico de ticks (auditoria)")
        st.caption(
            "Uma linha por **tick** (manual ou vigília automática): filtros, ciclo, modo de papel e patrimônio. "
            "A auditoria desta aba é do motor em papel. Execução testnet aparece na aba **Testnet** e nos logs."
        )
        ar = st.session_state.audit_rows
        if not ar:
            st.info("Use **Rodar um tick agora** ou **Iniciar vigília** na barra lateral. A aba **Logs** guarda texto bruto.")
        else:
            df_a = pd.DataFrame(ar)
            st.dataframe(df_a.iloc[::-1], use_container_width=True, hide_index=True)
            st.download_button(
                "Exportar auditoria (CSV)",
                data=df_a.to_csv(index=False).encode("utf-8-sig"),
                file_name="auditoria_ticks.csv",
                mime="text/csv",
                key="dl_audit_csv",
            )
        if st.button("Limpar auditoria", key="btn_clear_audit"):
            st.session_state.audit_rows = []
            st.rerun()

    elif page == "Logs":
        st.markdown("#### Log textual (debug)")
        st.caption("Para leitura do dia a dia prefira **Auditoria** (tabela).")
        st.code("\n".join(st.session_state.log_lines[-120:]) or "(vazio)", language="text")


def main() -> None:
    _run()


def _running_inside_streamlit() -> bool:
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx

        return get_script_run_ctx() is not None
    except Exception:
        return False


if __name__ == "__main__":
    if _running_inside_streamlit():
        main()
    else:
        app_path = Path(__file__).resolve()
        print("Abrindo Streamlit…\n", f"streamlit run \"{app_path}\"")
        raise SystemExit(
            subprocess.call([sys.executable, "-m", "streamlit", "run", str(app_path), *sys.argv[1:]])
        )
