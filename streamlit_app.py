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
    """Testa conexão sem imprimir no terminal."""
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
#  ESTADO DA SESSÃO
# ═══════════════════════════════════════════════════════════════════════════════

def _init_state() -> None:
    defaults: dict = {
        "page":          "config",
        "engine":        None,
        "engine_thread": None,
        "event_q":       queue.Queue(),
        "event_log":     [],
        "started_at":    None,
        "last_cycle":    -1,
        "conn_result":   None,  # (ok: bool, msg: str) | None
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


# ═══════════════════════════════════════════════════════════════════════════════
#  LÓGICA DE SESSÃO  (idêntica ao App._start_session do engine_gui.py)
# ═══════════════════════════════════════════════════════════════════════════════

def _start_session(*, plugin, client, api_key, api_secret,
                   bank, sym_idx, symbol, live) -> None:
    _clear_session_files(_AUDIT_DIR)

    # Reseta log
    while not st.session_state.event_q.empty():
        try:
            st.session_state.event_q.get_nowait()
        except queue.Empty:
            break
    st.session_state.event_log  = []
    st.session_state.last_cycle = -1
    st.session_state.started_at = datetime.utcnow()

    session_id = f"{plugin.__name__}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
    mode_lbl   = str(getattr(plugin, "STRATEGY_DISPLAY_NAME", "PLUGIN")).upper()

    vbank     = VirtualBank(initial_quote=bank, quote_balance=bank, base_balance=0.0)
    binance_e = BinanceEngine(client=client, vbank=vbank, symbol=symbol)
    logger    = _GUILogger(
        session_id=session_id, out_dir=_AUDIT_DIR,
        event_q=st.session_state.event_q,
    )

    engine = TradingEngine(
        binance=binance_e, vbank=vbank, logger=logger,
        symbol=symbol, sym_idx=sym_idx,
        mode=0,   # preserva comportamento do engine_gui.py
        live=live, strat=plugin,
    )

    # Suprime o dashboard ANSI do terminal — a GUI web é o display
    engine._display = lambda: None  # type: ignore[method-assign]

    # Encaminha mensagens internas do motor para a fila da GUI
    # (mesmo patch do engine_gui.py, sem alterar a classe)
    _eq       = st.session_state.event_q
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

    st.session_state.engine = engine

    # Thread do motor — captura referências antes de iniciar
    _eq_ref = st.session_state.event_q
    def _run() -> None:
        try:
            engine.run()
        except Exception as exc:
            _eq_ref.put_nowait(f"[!] ERRO: {exc}")

    eng_thread = threading.Thread(target=_run, daemon=True, name="mk-engine")
    st.session_state.engine_thread = eng_thread
    eng_thread.start()

    st.session_state.page = "monitor"
    st.rerun()


# ═══════════════════════════════════════════════════════════════════════════════
#  PÁGINA — CONFIGURAÇÃO
# ═══════════════════════════════════════════════════════════════════════════════

def render_config() -> None:
    st.markdown("## ◈  BINANCE MK ENGINE")
    st.caption("Configuração da sessão")
    st.divider()

    # ── Plugin ───────────────────────────────────────────────────────────────
    st.markdown("**PLUGIN DE ESTRATÉGIA**")
    plugins = _scan_plugins()

    if not plugins:
        st.warning(
            "Nenhum plugin encontrado. "
            "Verifique se os arquivos `micro_banca_*.py` estão no mesmo diretório."
        )
        return

    plugin_names = [os.path.basename(p) for p in plugins]
    sel_idx = st.selectbox(
        "Arquivo de plugin",
        range(len(plugins)),
        format_func=lambda i: plugin_names[i],
        key="cfg_plugin_idx",
    )
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
        if not sym_key:
            st.error("Selecione um símbolo.")
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
            api_key=api_key.strip(), api_secret=api_secret.strip(),
            bank=bank, sym_idx=sym_idx, symbol=symbol, live=live_mode,
        )


# ═══════════════════════════════════════════════════════════════════════════════
#  PÁGINA — MONITOR
# ═══════════════════════════════════════════════════════════════════════════════

def _drain_log() -> None:
    """Drena a fila de eventos do motor para a lista de log da sessão."""
    try:
        while True:
            st.session_state.event_log.append(
                st.session_state.event_q.get_nowait()
            )
    except queue.Empty:
        pass


def _tick_cycle(engine: TradingEngine) -> None:
    """Adiciona linha de ciclo ao log quando o ciclo avança (espelha o _poll do Tkinter)."""
    if engine.cycle == st.session_state.last_cycle or engine.cycle <= 0:
        return
    st.session_state.last_cycle = engine.cycle
    mid = engine._last_mid or 0.0
    atr = engine._last_atr or 0.0
    adx = engine._last_adx or 0.0
    bid_tag = "↑" if engine.binance.bid_order else "·"
    ask_tag = "↑" if engine.binance.ask_order else "·"
    ts = datetime.utcnow().strftime("%H:%M:%S")
    st.session_state.event_log.append(
        f"[{ts}] ◌ C{engine.cycle:04d}  ${mid:,.2f}"
        f"  ATR {atr:.3f}%  ADX {adx:.1f}"
        f"  BID{bid_tag} ASK{ask_tag}"
    )


def _fmt_order(order, side: str) -> None:
    """Renderiza cartão de ordem BID ou ASK."""
    icon = "🔵" if side == "BID" else "🟡"
    st.markdown(f"**{icon} ORDEM {side}**")
    if order is None:
        st.caption("Sem ordem ativa")
        return
    pct = (order.filled_qty / order.qty * 100) if order.qty > 0 else 0.0
    st.markdown(
        f"Status: `{order.status}`  \n"
        f"Preço: `${order.price:,.4f}`  \n"
        f"Qty: `{order.qty:.6f}`  \n"
        f"Preenchido: `{order.filled_qty:.6f}` ({pct:.0f}%)"
    )


def _render_audit_download() -> None:
    """Botão de download ZIP da auditoria (substitui o HTTP server do Tkinter)."""
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
    engine = st.session_state.engine

    if engine is None:
        st.error("Nenhuma sessão ativa.")
        if st.button("◀  Configuração"):
            st.session_state.page = "config"
            st.rerun()
        return

    vb      = engine.vbank
    running = engine.running

    # Atualiza log e tick de ciclo
    _drain_log()
    _tick_cycle(engine)
    if len(st.session_state.event_log) > 200:
        st.session_state.event_log = st.session_state.event_log[-200:]

    # ── Cabeçalho ────────────────────────────────────────────────────────────
    strat_name = getattr(engine._strat, "STRATEGY_DISPLAY_NAME", engine._strat.__name__)
    net_lbl    = "LIVE" if engine.live else "TESTNET"
    sym_base   = engine.symbol[:-4] if engine.symbol.endswith("USDT") else engine.symbol[:3]

    col_title, col_status = st.columns([5, 1])
    with col_title:
        st.markdown(f"## ◈  {engine.symbol}  [{net_lbl}]  —  {strat_name}")
    with col_status:
        if running:
            st.success("⬤ EXECUTANDO")
        else:
            st.error("⬤ ENCERRADO")

    st.divider()

    # ── Linha 1 — banca virtual ───────────────────────────────────────────────
    pnl     = vb.total_pnl
    pnl_str = f"{'+' if pnl >= 0 else ''}{pnl:.4f} USDT"

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("PnL TOTAL",  pnl_str)
    c2.metric("EQUITY",     f"${vb.equity:,.4f}")
    c3.metric("USDT LIVRE", f"${vb.available_quote:,.4f}")
    c4.metric("BASE",       f"{vb.base_balance:.6f} {sym_base}")
    c5.metric("FILLS",      f"{vb.total_fills}  ↑{vb.buy_fills} ↓{vb.sell_fills}")

    # ── Linha 2 — mercado ─────────────────────────────────────────────────────
    mid = engine._last_mid or 0.0
    atr = engine._last_atr or 0.0
    adx = engine._last_adx or 0.0

    elapsed = "00:00:00"
    if st.session_state.started_at:
        secs   = int((datetime.utcnow() - st.session_state.started_at).total_seconds())
        h, rem = divmod(secs, 3600)
        m, s   = divmod(rem, 60)
        elapsed = f"{h:02d}:{m:02d}:{s:02d}"

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("MID PRICE", f"${mid:,.4f}" if mid else "—")
    c2.metric("ATR %",     f"{atr:.4f}%"  if atr else "—")
    c3.metric("ADX",       f"{adx:.1f}"   if adx else "—")
    c4.metric("CICLO",     str(engine.cycle))
    c5.metric("TEMPO",     elapsed)

    st.divider()

    # ── Linha 3 — ordens ativas ───────────────────────────────────────────────
    col_bid, col_ask = st.columns(2)
    with col_bid:
        _fmt_order(engine.binance.bid_order, "BID")
    with col_ask:
        _fmt_order(engine.binance.ask_order, "ASK")

    st.divider()

    # ── Log de eventos ────────────────────────────────────────────────────────
    st.markdown("**LOG DE EVENTOS**")
    log_lines = list(reversed(st.session_state.event_log[-50:]))
    st.text_area(
        label="log",
        value="\n".join(log_lines),
        height=200,
        disabled=True,
        label_visibility="collapsed",
        key="evt_log_display",
    )

    st.divider()

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

_init_state()

if st.session_state.page == "config":
    render_config()
else:
    render_monitor()
