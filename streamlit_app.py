#!/usr/bin/env python3
"""
streamlit_app.py  ―  Interface web para o Motor Binance MK
──────────────────────────────────────────────────────────
Iniciar : streamlit run streamlit_app.py
Servidor: streamlit run streamlit_app.py --server.port 8501 --server.address 0.0.0.0

Requer: binance_live_engine.py no mesmo diretório
        pip install python-binance numpy streamlit
"""
from __future__ import annotations

import io, os, sys, queue, threading, importlib.util, types, zipfile, time
from datetime import datetime
from typing import Optional

import streamlit as st

# ── page_config DEVE ser a primeira chamada Streamlit ────────────────────────
st.set_page_config(
    page_title="Binance MK Engine",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="collapsed",
)

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

# ── Importa componentes do motor (motor não é modificado) ────────────────────
try:
    from binance_live_engine import (
        VirtualBank, BinanceEngine, TradingEngine, AuditLogger,
        _AUDIT_DIR, _clear_session_files,
    )
    from binance.client import Client
    from binance.exceptions import BinanceAPIException
except ImportError as _err:
    st.error(
        f"**Dependência ausente:** `{_err}`\n\n"
        "Execute no terminal:\n```\npip install python-binance numpy streamlit\n```"
    )
    st.stop()


# ═══════════════════════════════════════════════════════════════════════════════
#  ESTADO GLOBAL DO SERVIDOR
#  @st.cache_resource garante que o dict é criado UMA SÓ VEZ por processo.
#  Sobrevive a reruns e refreshes de browser — é aqui que o motor fica.
# ═══════════════════════════════════════════════════════════════════════════════

@st.cache_resource
def _get_G() -> dict:
    return {
        "engine":        None,   # TradingEngine | None
        "engine_thread": None,   # Thread | None
        "event_q":       queue.Queue(),
        "event_log":     [],     # list[str] — log acumulado da sessão
        "started_at":    None,   # datetime | None
        "last_cycle":    -1,
    }

_G = _get_G()   # na 1ª execução cria; nos reruns devolve o mesmo objeto


# ═══════════════════════════════════════════════════════════════════════════════
#  TEMA  —  CSS injetado uma vez por sessão de browser
# ═══════════════════════════════════════════════════════════════════════════════

def _inject_css() -> None:
    st.markdown("""
<style>
/* ── Fundo e tipografia base ───────────────────────────────────────────────── */
html, body, [data-testid="stAppViewContainer"], [data-testid="stMain"] {
    background-color: #0d0d0d !important;
    font-family: Consolas, 'Courier New', monospace !important;
}
[data-testid="stHeader"] { background: #0d0d0d !important; }
section[data-testid="stSidebar"] { background: #0d0d0d !important; }
hr { border-color: #1e1e1e !important; }

/* ── Inputs ────────────────────────────────────────────────────────────────── */
input, textarea, [data-baseweb="input"] input, [data-baseweb="textarea"] textarea {
    background-color: #141414 !important;
    color: #e0e0e0 !important;
    font-family: Consolas, monospace !important;
    border-color: #2e7d32 !important;
}
[data-baseweb="select"] > div {
    background-color: #141414 !important;
    border-color: #2e7d32 !important;
    color: #e0e0e0 !important;
}

/* ── Botões ────────────────────────────────────────────────────────────────── */
[data-testid="stBaseButton-primary"] {
    background-color: #1b5e20 !important;
    border: 1px solid #2e7d32 !important;
    color: #00e676 !important;
    font-family: Consolas, monospace !important;
}
[data-testid="stBaseButton-secondary"] {
    background-color: #141414 !important;
    border: 1px solid #2e7d32 !important;
    color: #00e676 !important;
    font-family: Consolas, monospace !important;
}
[data-testid="stBaseButton-secondary"]:hover,
[data-testid="stBaseButton-primary"]:hover {
    border-color: #00e676 !important;
    color: #69f0ae !important;
}

/* ── Labels e captions ─────────────────────────────────────────────────────── */
label, [data-testid="stWidgetLabel"] p, .stRadio label {
    color: #00e676 !important;
    font-family: Consolas, monospace !important;
    font-size: 12px !important;
}
[data-testid="stCaptionContainer"] p { color: #2e7d32 !important; }

/* ── Download button ───────────────────────────────────────────────────────── */
[data-testid="stDownloadButton"] button {
    background-color: #141414 !important;
    border: 1px solid #2e7d32 !important;
    color: #00e676 !important;
    font-family: Consolas, monospace !important;
}

/* ── Scrollbar ─────────────────────────────────────────────────────────────── */
::-webkit-scrollbar { width: 6px; }
::-webkit-scrollbar-track { background: #0d0d0d; }
::-webkit-scrollbar-thumb { background: #2e7d32; border-radius: 3px; }
</style>
""", unsafe_allow_html=True)


# ── Helpers de card HTML ──────────────────────────────────────────────────────

def _card(title: str, value: str, color: str = "#e0e0e0") -> str:
    return (
        '<div style="background:#141414;border:1px solid #1e1e1e;border-radius:4px;'
        'padding:10px 14px;font-family:Consolas,monospace;height:100%">'
        f'<div style="color:#2e7d32;font-size:10px;letter-spacing:.06em;'
        f'text-transform:uppercase;margin-bottom:6px">{title}</div>'
        f'<div style="color:{color};font-size:15px;font-weight:bold">{value}</div>'
        '</div>'
    )


def _order_card_html(order, side: str) -> str:
    color = "#40c4ff" if side == "BID" else "#ffd740"
    header = (
        '<div style="background:#141414;border:1px solid #1e1e1e;border-radius:4px;'
        f'padding:12px 16px;font-family:Consolas,monospace">'
        f'<div style="color:{color};font-size:13px;font-weight:bold;'
        f'letter-spacing:.05em;margin-bottom:10px">ORDEM {side}</div>'
    )
    if order is None:
        return header + '<div style="color:#2e7d32;font-size:12px">Sem ordem ativa</div></div>'
    pct = (order.filled_qty / order.qty * 100) if order.qty > 0 else 0.0
    rows = (
        '<div style="display:grid;grid-template-columns:max-content 1fr;'
        'column-gap:14px;row-gap:5px;font-size:12px">'
        f'<span style="color:#2e7d32">Status</span>'
        f'<span style="color:#e0e0e0">{order.status}</span>'
        f'<span style="color:#2e7d32">Preço</span>'
        f'<span style="color:#e0e0e0">${order.price:,.4f}</span>'
        f'<span style="color:#2e7d32">Qty</span>'
        f'<span style="color:#e0e0e0">{order.qty:.6f}</span>'
        f'<span style="color:#2e7d32">Preenchido</span>'
        f'<span style="color:#e0e0e0">{order.filled_qty:.6f} ({pct:.0f}%)</span>'
        '</div>'
    )
    return header + rows + '</div>'


# ═══════════════════════════════════════════════════════════════════════════════
#  UTILITÁRIOS  (mesma lógica do engine_gui.py — sem alterações no motor)
# ═══════════════════════════════════════════════════════════════════════════════

def _load_plugin(path: str) -> Optional[types.ModuleType]:
    """Carrega .py como plugin; valida que possui propose() callable."""
    try:
        name = os.path.splitext(os.path.basename(path))[0]
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return mod if callable(getattr(mod, "propose", None)) else None
    except Exception:
        return None


def _scan_plugins() -> list[str]:
    """Varre o diretório em busca de plugins .py com propose()."""
    excluded = {"engine_gui.py", "streamlit_app.py", "binance_live_engine.py"}
    result = []
    for fn in sorted(os.listdir(_HERE)):
        if fn.endswith(".py") and fn not in excluded:
            path = os.path.join(_HERE, fn)
            if _load_plugin(path) is not None:
                result.append(path)
    return result


def _test_conn(client: Client, symbol: str) -> tuple[bool, str]:
    try:
        client.ping()
        ticker = client.get_symbol_ticker(symbol=symbol)
        price  = float(ticker["price"])
        acct   = client.get_account()
        usdt   = next(
            (float(b["free"]) for b in acct["balances"] if b["asset"] == "USDT"),
            0.0,
        )
        return True, f"${price:,.4f}   USDT livre: ${usdt:,.2f}"
    except BinanceAPIException as exc:
        return False, f"Erro API [{exc.code}]: {exc.message}"
    except Exception as exc:
        return False, str(exc)


class _GUILogger(AuditLogger):
    """AuditLogger que espelha eventos de sessão para a fila da GUI."""

    def __init__(self, *args, event_q: queue.Queue, **kwargs):
        super().__init__(*args, **kwargs)
        self._q = event_q

    def log(self, event_type: str, data: dict) -> None:
        super().log(event_type, data)
        ts = datetime.utcnow().strftime("%H:%M:%S")
        if event_type == "SESSION_START":
            self._q.put_nowait(f"[{ts}] ▶ Sessão iniciada")
        elif event_type == "SESSION_END":
            self._q.put_nowait(f"[{ts}] ■ Sessão encerrada")


# ═══════════════════════════════════════════════════════════════════════════════
#  ESTADO DA SESSÃO DE BROWSER
#  Apenas o que é exclusivo de cada aba/browser (não sobrevive a refresh).
# ═══════════════════════════════════════════════════════════════════════════════

def _init_state() -> None:
    if "page" not in st.session_state:
        # Ao abrir/reabrir o browser: reconecta ao monitor se há motor ativo
        st.session_state.page = "monitor" if _G["engine"] is not None else "config"
    for k, v in {"conn_result": None, "last_plugin_idx": None}.items():
        if k not in st.session_state:
            st.session_state[k] = v


# ═══════════════════════════════════════════════════════════════════════════════
#  LÓGICA DE SESSÃO  (idêntica ao App._start_session do engine_gui.py)
# ═══════════════════════════════════════════════════════════════════════════════

def _start_session(*, plugin, client, bank, sym_idx, symbol, live) -> None:
    _clear_session_files(_AUDIT_DIR)

    # Reseta estado global
    while not _G["event_q"].empty():
        try:
            _G["event_q"].get_nowait()
        except queue.Empty:
            break
    _G["event_log"]  = []
    _G["last_cycle"] = -1
    _G["started_at"] = datetime.utcnow()

    session_id = f"{plugin.__name__}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
    mode_lbl   = str(getattr(plugin, "STRATEGY_DISPLAY_NAME", "PLUGIN")).upper()

    vbank     = VirtualBank(initial_quote=bank, quote_balance=bank, base_balance=0.0)
    binance_e = BinanceEngine(client=client, vbank=vbank, symbol=symbol)
    logger    = _GUILogger(
        session_id=session_id, out_dir=_AUDIT_DIR,
        event_q=_G["event_q"],
    )

    engine = TradingEngine(
        binance=binance_e, vbank=vbank, logger=logger,
        symbol=symbol, sym_idx=sym_idx,
        mode=0,
        live=live, strat=plugin,
    )

    # Suprime o dashboard ANSI do terminal — a GUI web é o display
    engine._display = lambda: None  # type: ignore[method-assign]

    # Encaminha mensagens internas do motor para a fila (mesmo patch do engine_gui.py)
    _eq       = _G["event_q"]
    _orig_log = engine._log
    def _fwd_log(msg: str, _orig=_orig_log, _eng=engine, _q=_eq) -> None:
        _orig(msg)
        if _eng._log_lines:
            _q.put_nowait(_eng._log_lines[-1])
    engine._log = _fwd_log  # type: ignore[method-assign]

    logger.log("SESSION_START", {
        "symbol":     symbol,
        "mode_label": mode_lbl,
        "notes":      f"live={live}  vb_initial={bank}  plugin={plugin.__name__}",
        "cycle":      0,
    })

    _G["engine"] = engine

    _eq_ref = _G["event_q"]
    def _run() -> None:
        try:
            engine.run()
        except Exception as exc:
            _eq_ref.put_nowait(f"[!] ERRO: {exc}")

    eng_thread = threading.Thread(target=_run, daemon=True, name="mk-engine")
    _G["engine_thread"] = eng_thread
    eng_thread.start()

    st.session_state.page = "monitor"
    st.rerun()


# ═══════════════════════════════════════════════════════════════════════════════
#  PÁGINA — CONFIGURAÇÃO
# ═══════════════════════════════════════════════════════════════════════════════

def render_config() -> None:
    st.markdown(
        '<div style="background:#141414;border:1px solid #1e1e1e;border-radius:4px;'
        'padding:12px 20px;margin-bottom:20px">'
        '<span style="color:#69f0ae;font-family:Consolas,monospace;font-size:17px;font-weight:bold">'
        '◈  BINANCE MK ENGINE</span>'
        '<span style="color:#2e7d32;font-family:Consolas,monospace;font-size:11px;'
        'margin-left:16px">CONFIGURAÇÃO</span>'
        '</div>',
        unsafe_allow_html=True,
    )

    # ── Upload de plugin do computador local para o servidor ─────────────────
    with st.expander("📤  Enviar plugin do computador local para o servidor"):
        uploaded = st.file_uploader(
            "Selecione um arquivo `.py` com a função `propose()`",
            type=["py"],
            key="plugin_upload",
        )
        if uploaded is not None:
            import tempfile
            raw = uploaded.read()
            # Valida antes de salvar: grava em temp e testa propose()
            with tempfile.NamedTemporaryFile(suffix=".py", delete=False) as tmp:
                tmp.write(raw)
                tmp_path = tmp.name
            mod = _load_plugin(tmp_path)
            os.unlink(tmp_path)
            if mod is None:
                st.error(
                    f"`{uploaded.name}` não possui uma função `propose()` válida. "
                    "Upload cancelado."
                )
            else:
                dest = os.path.join(_HERE, uploaded.name)
                with open(dest, "wb") as fh:
                    fh.write(raw)
                st.success(f"Plugin `{uploaded.name}` salvo no servidor.")
                st.rerun()

    # ── Plugin ───────────────────────────────────────────────────────────────
    st.markdown("**PLUGIN DE ESTRATÉGIA**")
    plugins = _scan_plugins()

    if not plugins:
        st.warning(
            "Nenhum plugin encontrado. "
            "Envie um plugin acima ou verifique se os arquivos `micro_banca_*.py` "
            "estão no diretório do servidor."
        )
        return

    plugin_names = [os.path.basename(p) for p in plugins]
    sel_idx = st.selectbox(
        "Arquivo de plugin",
        range(len(plugins)),
        format_func=lambda i: plugin_names[i],
        key="cfg_plugin_idx",
    )

    # Limpa status de conexão ao trocar de plugin
    if st.session_state.last_plugin_idx != sel_idx:
        st.session_state.last_plugin_idx = sel_idx
        st.session_state.conn_result = None

    plugin_path = plugins[sel_idx]
    plugin_mod  = _load_plugin(plugin_path)

    if plugin_mod is None:
        st.error("Plugin inválido — sem função `propose()` válida.")
        return

    name = getattr(plugin_mod, "STRATEGY_DISPLAY_NAME", plugin_mod.__name__)
    defs = getattr(plugin_mod, "TESTNET_SOAK_DEFAULTS", {})
    cyc  = defs.get("max_cycles", "∞")
    ival = defs.get("interval_sec", "?")
    st.caption(f"{name}   |   Ciclos: {cyc}   Intervalo: {ival}s")

    sym_index = getattr(plugin_mod, "_SYMBOL_INDEX", {0: "BTCUSDT"})
    sym_map   = {f"{v}  (idx {k})": k for k, v in sorted(sym_index.items())}

    if not sym_map:
        st.error("Plugin sem símbolos definidos em `_SYMBOL_INDEX`.")
        return

    st.divider()

    # ── Credenciais ──────────────────────────────────────────────────────────
    st.markdown("**CREDENCIAIS API BINANCE**")
    col_key, col_sec = st.columns(2)
    with col_key:
        api_key = st.text_input("API Key", key="cfg_api_key")
    with col_sec:
        api_secret = st.text_input("API Secret", type="password", key="cfg_api_secret")

    st.divider()

    # ── Rede / Símbolo / Banca ────────────────────────────────────────────────
    st.markdown("**CONFIGURAÇÃO DE SESSÃO**")
    col_net, col_sym, col_bank = st.columns(3)

    with col_net:
        net_choice = st.radio(
            "Rede",
            ["Testnet (seguro)", "LIVE  ⚠  dinheiro real"],
            key="cfg_net",
        )
        live_mode = (net_choice == "LIVE  ⚠  dinheiro real")

    with col_sym:
        sym_key = st.selectbox("Símbolo", list(sym_map.keys()), key="cfg_symbol")
        sym_idx = sym_map[sym_key]
        symbol  = sym_index[sym_idx]

    with col_bank:
        bank = st.number_input(
            "Banca (USDT)", min_value=1.0, value=1000.0, step=10.0, key="cfg_bank",
        )

    st.divider()

    # ── Status da última tentativa de conexão ─────────────────────────────────
    if st.session_state.conn_result is not None:
        ok, msg = st.session_state.conn_result
        if ok:
            st.success(f"Conexão OK — {msg}")
        else:
            st.error(f"Falha na conexão: {msg}")

    # ── Botão Iniciar ────────────────────────────────────────────────────────
    col_btn, col_note = st.columns([1, 3])
    with col_btn:
        start = st.button("▶  INICIAR SESSÃO", type="primary", use_container_width=True)
    with col_note:
        st.caption("O botão ⏹ Parar no monitor encerra o motor com segurança.")

    if start:
        if not api_key.strip() or not api_secret.strip():
            st.error("Preencha a API Key e o API Secret.")
            return

        with st.spinner("Conectando à Binance…"):
            client = Client(api_key.strip(), api_secret.strip(), testnet=not live_mode)
            ok, msg = _test_conn(client, symbol)

        st.session_state.conn_result = (ok, msg)

        if not ok:
            st.rerun()
            return

        _start_session(
            plugin=plugin_mod, client=client,
            bank=bank, sym_idx=sym_idx, symbol=symbol, live=live_mode,
        )


# ═══════════════════════════════════════════════════════════════════════════════
#  PÁGINA — MONITOR
# ═══════════════════════════════════════════════════════════════════════════════

def _drain_log() -> None:
    """Drena a fila de eventos do motor para a lista de log global."""
    try:
        while True:
            _G["event_log"].append(_G["event_q"].get_nowait())
    except queue.Empty:
        pass


def _tick_cycle(engine: TradingEngine) -> None:
    """Adiciona linha de ciclo ao log quando o ciclo avança."""
    if engine.cycle == _G["last_cycle"] or engine.cycle <= 0:
        return
    _G["last_cycle"] = engine.cycle
    mid = engine._last_mid or 0.0
    atr = engine._last_atr or 0.0
    adx = engine._last_adx or 0.0
    bid_tag = "↑" if engine.binance.bid_order else "·"
    ask_tag = "↑" if engine.binance.ask_order else "·"
    ts = datetime.utcnow().strftime("%H:%M:%S")
    _G["event_log"].append(
        f"[{ts}] ◌ C{engine.cycle:04d}  ${mid:,.2f}"
        f"  ATR {atr:.3f}%  ADX {adx:.1f}"
        f"  BID{bid_tag} ASK{ask_tag}"
    )


def _render_audit_download() -> None:
    """Botão de download ZIP da auditoria."""
    if not os.path.isdir(_AUDIT_DIR):
        st.caption("Pasta de auditoria não encontrada.")
        return
    files = [
        f for f in os.listdir(_AUDIT_DIR)
        if f.startswith("audit_") and (f.endswith(".csv") or f.endswith(".json"))
    ]
    if not files:
        st.caption("Sem arquivos de auditoria.")
        return
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for fn in files:
            z.write(os.path.join(_AUDIT_DIR, fn), fn)
    buf.seek(0)
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    st.download_button(
        label="↓  Auditoria",
        data=buf,
        file_name=f"auditoria_{ts}.zip",
        mime="application/zip",
        use_container_width=True,
    )


def render_monitor() -> None:
    engine = _G["engine"]

    if engine is None:
        st.error("Nenhuma sessão ativa.")
        if st.button("◀  Configuração"):
            st.session_state.page = "config"
            st.rerun()
        return

    vb      = engine.vbank
    running = engine.running

    # Atualiza log global
    _drain_log()
    _tick_cycle(engine)
    if len(_G["event_log"]) > 200:
        _G["event_log"] = _G["event_log"][-200:]

    # ── Cabeçalho ────────────────────────────────────────────────────────────
    strat_name   = getattr(engine._strat, "STRATEGY_DISPLAY_NAME", engine._strat.__name__)
    net_lbl      = "LIVE" if engine.live else "TESTNET"
    sym_base     = engine.symbol[:-4] if engine.symbol.endswith("USDT") else engine.symbol[:3]
    status_color = "#00e676" if running else "#ff5252"
    status_text  = "⬤  EXECUTANDO" if running else "⬤  ENCERRADO"

    st.markdown(
        '<div style="background:#141414;border:1px solid #1e1e1e;border-radius:4px;'
        'padding:12px 20px;display:flex;justify-content:space-between;'
        'align-items:center;margin-bottom:4px">'
        '<span style="color:#69f0ae;font-family:Consolas,monospace;font-size:17px;font-weight:bold">'
        f'◈  {engine.symbol}  [{net_lbl}]  —  {strat_name}</span>'
        f'<span style="color:{status_color};font-family:Consolas,monospace;'
        f'font-size:13px;font-weight:bold">{status_text}</span>'
        '</div>',
        unsafe_allow_html=True,
    )
    st.markdown("<div style='margin-bottom:12px'></div>", unsafe_allow_html=True)

    # ── Linha 1 — banca virtual ───────────────────────────────────────────────
    pnl     = vb.total_pnl
    pnl_str = f"{'+' if pnl >= 0 else ''}{pnl:.4f} USDT"
    pnl_col = "#00e676" if pnl >= 0 else "#ff5252"

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.markdown(_card("PnL TOTAL",  pnl_str,                                        pnl_col), unsafe_allow_html=True)
    c2.markdown(_card("EQUITY",     f"${vb.equity:,.4f}"),                                    unsafe_allow_html=True)
    c3.markdown(_card("USDT LIVRE", f"${vb.available_quote:,.4f}"),                           unsafe_allow_html=True)
    c4.markdown(_card("BASE",       f"{vb.base_balance:.6f} {sym_base}"),                     unsafe_allow_html=True)
    c5.markdown(_card("FILLS",      f"{vb.total_fills}  ↑{vb.buy_fills} ↓{vb.sell_fills}"),  unsafe_allow_html=True)

    st.markdown("<div style='margin-top:6px'></div>", unsafe_allow_html=True)

    # ── Linha 2 — mercado ─────────────────────────────────────────────────────
    mid = engine._last_mid or 0.0
    atr = engine._last_atr or 0.0
    adx = engine._last_adx or 0.0

    elapsed = "00:00:00"
    if _G["started_at"]:
        secs   = int((datetime.utcnow() - _G["started_at"]).total_seconds())
        h, rem = divmod(secs, 3600)
        m, s   = divmod(rem, 60)
        elapsed = f"{h:02d}:{m:02d}:{s:02d}"

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.markdown(_card("MID PRICE", f"${mid:,.4f}" if mid else "—"), unsafe_allow_html=True)
    c2.markdown(_card("ATR %",     f"{atr:.4f}%"  if atr else "—"), unsafe_allow_html=True)
    c3.markdown(_card("ADX",       f"{adx:.1f}"   if adx else "—"), unsafe_allow_html=True)
    c4.markdown(_card("CICLO",     str(engine.cycle)),               unsafe_allow_html=True)
    c5.markdown(_card("TEMPO",     elapsed),                         unsafe_allow_html=True)

    st.markdown("<div style='margin-top:8px'></div>", unsafe_allow_html=True)

    # ── Linha 3 — ordens ativas ───────────────────────────────────────────────
    col_bid, col_ask = st.columns(2)
    col_bid.markdown(_order_card_html(engine.binance.bid_order, "BID"), unsafe_allow_html=True)
    col_ask.markdown(_order_card_html(engine.binance.ask_order, "ASK"), unsafe_allow_html=True)

    st.markdown("<div style='margin-top:8px'></div>", unsafe_allow_html=True)

    # ── Log de eventos ────────────────────────────────────────────────────────
    log_lines = list(reversed(_G["event_log"][-50:]))
    log_html = (
        '<div style="background:#141414;border:1px solid #1e1e1e;border-radius:4px;'
        'padding:10px 14px;font-family:Consolas,monospace;font-size:11px;'
        'height:180px;overflow-y:auto;white-space:pre">'
        '<div style="color:#1b5e20;font-size:10px;letter-spacing:.06em;'
        'text-transform:uppercase;margin-bottom:8px">LOG DE EVENTOS</div>'
        '<span style="color:#69f0ae">'
        + "\n".join(log_lines)
        + '</span></div>'
    )
    st.markdown(log_html, unsafe_allow_html=True)

    st.markdown("<div style='margin-top:8px'></div>", unsafe_allow_html=True)

    # ── Rodapé ────────────────────────────────────────────────────────────────
    col_stop, col_audit, col_back, col_note = st.columns([1, 1, 1, 3])

    with col_stop:
        if st.button("⏹  PARAR", type="primary",
                     disabled=not running, use_container_width=True):
            engine.shutdown()
            st.rerun()

    with col_audit:
        _render_audit_download()

    with col_back:
        if st.button("◀  Configuração",
                     disabled=running, use_container_width=True):
            _G["engine"] = None
            st.session_state.conn_result = None
            st.session_state.page        = "config"
            st.rerun()

    with col_note:
        if not running:
            st.caption("Sessão encerrada — use ◀ Configuração para novo soak.")

    # ── Auto-refresh a cada 1s enquanto o motor estiver rodando ───────────────
    if running:
        time.sleep(1)
        st.rerun()


# ═══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

_inject_css()
_init_state()

if st.session_state.page == "config":
    render_config()
else:
    render_monitor()
