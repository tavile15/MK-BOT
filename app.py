from __future__ import annotations

import copy
import hashlib
import hmac
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
from microtrans.ui.chrome import render_market_and_wallet, render_stepper, render_topbar
from microtrans.ui.pages import render_selected_page
from microtrans.ui.sidebar import render_sidebar
from microtrans.ui.theme import MT_CSS
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


def _env_truthy(name: str, default: bool = False) -> bool:
    raw = str(os.environ.get(name, "")).strip().lower()
    if raw == "":
        return default
    return raw in {"1", "true", "on", "yes", "y"}


def _check_ui_password(password: str, plain_ref: str, sha256_ref: str) -> bool:
    if plain_ref:
        return hmac.compare_digest(password, plain_ref)
    if sha256_ref:
        got = hashlib.sha256(password.encode("utf-8")).hexdigest()
        return hmac.compare_digest(got, sha256_ref.lower())
    return False


def _run() -> None:
    import time
    import streamlit as st
    from datetime import date, datetime, time as time_cls, timedelta, timezone

    st.set_page_config(page_title="MK BOT - MARKET MAKING", layout="wide", initial_sidebar_state="expanded")
    st.markdown(MT_CSS, unsafe_allow_html=True)

    def _brand_image_path() -> Path | None:
        candidates = [
            _ROOT / "indentidade_visual" / "Gemini_Generated_Image_ntdn6xntdn6xntdn.png",
            _ROOT / "indentidade_visual" / "Gemini_Generated_Image_r9hxdwr9hxdwr9hx.png",
            _ROOT / "indentidade_visual" / "WhatsApp Image 2026-04-02 at 17.36.26.jpeg",
        ]
        for p in candidates:
            if p.is_file():
                return p
        return None

    def _ui_login_gate() -> None:
        # Segurança para uso remoto: ativar via env/secrets e bloquear o painel sem autenticação.
        required = _env_truthy("MT_UI_LOGIN_REQUIRED", False)
        user_ref = str(os.environ.get("MT_UI_LOGIN_USER", "")).strip()
        pass_ref = str(os.environ.get("MT_UI_LOGIN_PASSWORD", "")).strip()
        pass_sha256_ref = str(os.environ.get("MT_UI_LOGIN_PASSWORD_SHA256", "")).strip().lower()
        try:
            sec = st.secrets
        except Exception:
            sec = {}
        if not user_ref:
            user_ref = str(sec.get("MT_UI_LOGIN_USER", "")).strip()
        if not pass_ref:
            pass_ref = str(sec.get("MT_UI_LOGIN_PASSWORD", "")).strip()
        if not pass_sha256_ref:
            pass_sha256_ref = str(sec.get("MT_UI_LOGIN_PASSWORD_SHA256", "")).strip().lower()
        if not required and str(sec.get("MT_UI_LOGIN_REQUIRED", "")).strip():
            required = str(sec.get("MT_UI_LOGIN_REQUIRED")).strip().lower() in {"1", "true", "on", "yes", "y"}
        if not required and user_ref and (pass_ref or pass_sha256_ref):
            required = True
        if not required:
            st.session_state["ui_auth_ok"] = True
            return

        if st.session_state.get("ui_auth_ok"):
            with st.sidebar:
                if st.button("Sair (logout)", key="ui_btn_logout"):
                    st.session_state["ui_auth_ok"] = False
                    st.rerun()
            return

        st.markdown('<div class="mt-login-wrap">', unsafe_allow_html=True)
        st.markdown('<div class="mt-login-title">🐺 MK BOT - MARKET MAKING</div>', unsafe_allow_html=True)
        st.markdown(
            '<div class="mt-login-sub">Acesso protegido. Entre com usuário e senha para abrir o painel.</div>',
            unsafe_allow_html=True,
        )
        img = _brand_image_path()
        if img is not None:
            st.image(str(img), width=180)
        if not user_ref or (not pass_ref and not pass_sha256_ref):
            st.error(
                "Login obrigatório ativo, mas credenciais não configuradas. "
                "Defina MT_UI_LOGIN_USER + MT_UI_LOGIN_PASSWORD (ou MT_UI_LOGIN_PASSWORD_SHA256)."
            )
            st.stop()

        with st.form("ui_login_form", clear_on_submit=False):
            user_in = st.text_input("Usuário")
            pass_in = st.text_input("Senha", type="password")
            ok = st.form_submit_button("Entrar", type="primary")
        st.markdown("</div>", unsafe_allow_html=True)
        if ok:
            user_ok = hmac.compare_digest(str(user_in).strip(), user_ref)
            pass_ok = _check_ui_password(str(pass_in), pass_ref, pass_sha256_ref)
            if user_ok and pass_ok:
                st.session_state["ui_auth_ok"] = True
                st.rerun()
            else:
                st.error("Credenciais inválidas.")
        st.stop()

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
    _ui_login_gate()

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
        if "_event_seq" not in st.session_state:
            st.session_state._event_seq = 0
        if "ui_bt_run_pending" not in st.session_state:
            st.session_state.ui_bt_run_pending = False
        if "ui_tn_account_pending" not in st.session_state:
            st.session_state.ui_tn_account_pending = False
        if "ui_tn_smoke_pending" not in st.session_state:
            st.session_state.ui_tn_smoke_pending = False
        if "testnet_equity_rows" not in st.session_state:
            st.session_state.testnet_equity_rows = []
        if "testnet_equity_baseline_by_symbol" not in st.session_state:
            st.session_state.testnet_equity_baseline_by_symbol = {}

    def _append_log(msg: str) -> None:
        st.session_state.log_lines = (st.session_state.log_lines + [msg])[-400:]

    def _next_event_id(prefix: str) -> str:
        st.session_state._event_seq = int(st.session_state.get("_event_seq", 0)) + 1
        return f"{prefix}-{st.session_state._event_seq:06d}"

    def _append_event_log(level: str, scope: str, msg: str, event_id: str | None = None) -> None:
        _eid = event_id or _next_event_id(scope)
        _append_log(f"{level.upper()} | {scope.upper()} | {_eid} | {msg}")

    def _capture_testnet_equity_snapshot(
        *,
        pub: BinancePublic,
        signed: BinanceSigned,
        symbol: str,
    ) -> dict:
        sym = str(symbol or "BTCUSDT").strip().upper()
        acc = signed.account()
        bals = acc.get("balances") or []
        base_a, quote_a = split_pair(sym)
        by_asset: dict[str, tuple[float, float]] = {}
        for b in bals:
            a = str(b.get("asset") or "").upper()
            if not a:
                continue
            by_asset[a] = (
                float(b.get("free") or 0.0),
                float(b.get("locked") or 0.0),
            )
        qf, ql = by_asset.get(quote_a, (0.0, 0.0))
        bf, bl = by_asset.get(base_a, (0.0, 0.0))
        quote_total = qf + ql
        base_total = bf + bl
        tk = pub.ticker_24h(sym)
        px = float(tk.get("lastPrice") or 0.0)
        equity_quote = quote_total + (base_total * px if px > 0 else 0.0)
        bl_key = f"{sym}_eq0"
        base_map = st.session_state.testnet_equity_baseline_by_symbol
        if bl_key not in base_map:
            base_map[bl_key] = equity_quote
        eq0 = float(base_map.get(bl_key) or equity_quote)
        pnl = equity_quote - eq0
        row = {
            "hora_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "symbol": sym,
            "quote_asset": quote_a,
            "base_asset": base_a,
            "quote_total": quote_total,
            "base_total": base_total,
            "mark_price": px,
            "equity_quote": equity_quote,
            "pnl_vs_start": pnl,
        }
        st.session_state.testnet_equity_rows = (st.session_state.testnet_equity_rows + [row])[-1500:]
        non_zero = [
            b
            for b in bals
            if float(b.get("free", 0) or 0) > 0
            or float(b.get("locked", 0) or 0) > 0
        ]
        return {
            "snapshot": row,
            "account": acc,
            "non_zero": non_zero,
        }

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
            "event_id": str(out.get("event_id") or _next_event_id("tick")),
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
            "sim_preco_compra": float(paper["bid"]) if paper.get("ok") and paper.get("bid") is not None else None,
            "sim_preco_venda": float(paper["ask"]) if paper.get("ok") and paper.get("ask") is not None else None,
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

    sidebar_actions = render_sidebar(
        st,
        yaml_static=_yaml_static,
        agent_defaults=_agent_defaults,
        eng_defaults=_eng_defaults,
        kline_intervals=list(BINANCE_KLINE_INTERVALS),
        gemini_key_present=bool(gemini_api_key()),
    )
    page = str(sidebar_actions["page"])
    run_auto = bool(sidebar_actions["run_auto"] or st.session_state.pop("ui_run_auto_pending", False))
    do_bt = bool(sidebar_actions["do_bt_sidebar"] or st.session_state.pop("ui_bt_run_pending", False))
    do_tn_account = bool(
        sidebar_actions["do_tn_account_sidebar"] or st.session_state.pop("ui_tn_account_pending", False)
    )
    do_tn_smoke = bool(
        sidebar_actions["do_tn_smoke_sidebar"] or st.session_state.pop("ui_tn_smoke_pending", False)
    )
    do_ping = bool(sidebar_actions.get("do_ping") or st.session_state.pop("ui_ping_pending", False))

    cfg = _effective_cfg()
    if do_ping:
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

    render_topbar(
        st,
        page=page,
        symbol=st.session_state.symbol,
        api_ping_ok=bool(st.session_state.api_ping_ok),
    )
    st.caption(
        "Ambiente de validação: papel + testnet. Conta live não recebe ordens nesta interface."
    )

    do_tick = bool(st.session_state.pop("ui_tick_pending", False))
    if st.session_state.pop("ui_reset_wallet_pending", False):
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
        _append_event_log("INFO", "carteira", "Carteira da UI reiniciada.")

    if run_auto:
        dur = float(st.session_state.get("ui_auto_dur", 120))
        t0 = time.time()
        st.session_state.vigil_active = True
        st.session_state.vigil_until = t0 + dur
        st.session_state.vigil_start_ts = t0
        st.session_state._vigil_last_tick_ts = 0.0
        st.session_state._vigil_ntick = 0

    _frag = getattr(st, "fragment", None)
    def _run_vigil_step_once() -> None:
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
            out["event_id"] = _next_event_id("vigil")
            ss["last_out"] = out
            _append_audit_row(out)
            ss["_vigil_ntick"] = int(ss.get("_vigil_ntick", 0)) + 1
            ss["_vigil_last_tick_ts"] = now
            ss.pop("_vigil_backoff_until", None)
            mid_o = float(out.get("mid") or 0)
            _append_event_log(
                "INFO",
                "vigil",
                f"n={ss['_vigil_ntick']} apt={'sim' if out.get('filter_apt') else 'não'} | {ss['wallet'].explain(mid_o)}",
                event_id=str(out.get("event_id") or _next_event_id("vigil")),
            )
        except Exception as ex:
            if _is_transient_network_error(ex):
                logging.getLogger("filter").warning(
                    "Vigília: falha transitória de rede (%s) — nova tentativa após ~5s.",
                    ex,
                )
                _append_event_log("WARN", "vigil", f"rede instável: {ex!r} — pausa ~5s")
                ss["_vigil_backoff_until"] = now + 5.0
                return
            ss["vigil_active"] = False
            _append_event_log("ERROR", "vigil", f"falha fatal: {ex}")
            st.error(str(ex))
            return
        st.caption(
            f"Vigília · tick {ss.get('_vigil_ntick', 0)} · papel · API (log 1:1 na aba Logs)"
        )

    if _frag is not None:

        @_frag(run_every=timedelta(seconds=1))
        def _vigil_fragment() -> None:
            _run_vigil_step_once()

        _vigil_fragment()
    elif st.session_state.get("vigil_active"):
        st.info("Modo compatível de vigília ativo (sem fragment).")
        _run_vigil_step_once()
        if st.session_state.get("vigil_active"):
            time.sleep(1.0)
            st.rerun()

    if do_tick:
        with st.spinner("Coletando cotação…"):
            try:
                out = eng.tick()
                out["event_id"] = _next_event_id("tick")
                st.session_state.last_out = out
                _append_audit_row(out)
                _append_event_log(
                    "INFO",
                    "tick",
                    f"apt={'sim' if out.get('filter_apt') else 'não'} | {st.session_state.wallet.explain(float(out.get('mid') or 0))}",
                    event_id=out["event_id"],
                )
            except Exception as e:
                if _is_transient_network_error(e):
                    logging.getLogger("filter").warning("Tick manual: rede instável (%s). Tente novamente.", e)
                    st.warning(f"Rede instável — tente novamente em instantes. ({e})")
                    _append_event_log("WARN", "tick", f"rede instável: {e!r}")
                else:
                    st.error(str(e))
                    _append_event_log("ERROR", "tick", f"falha: {e}")

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
                        _append_event_log("WARN", "backtest", "cancelado: intervalo incompleto")
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
                        _append_event_log(
                            "INFO",
                            "backtest",
                            f"ok | run={_rid} | {res.get('closing_explanation', '')}",
                        )
                    else:
                        _append_event_log("WARN", "backtest", f"falhou | {res}")
            except Exception as e:
                st.error(str(e))
                _append_event_log("ERROR", "backtest", f"erro: {e}")

    if do_tn_account or do_tn_smoke:
        with st.spinner("Spot Testnet…"):
            try:
                _ex = dict((load_config().get("execution") or {}))
                _tn_base = str(_ex.get("testnet_base_url") or "https://testnet.binance.vision")
                tpub = BinancePublic(base_url=_tn_base)
                tsigned = BinanceSigned.from_env(require_testnet=True)
                if do_tn_account:
                    sym_tn = str(st.session_state.get("ui_tn_symbol") or st.session_state.symbol).strip().upper()
                    snap = _capture_testnet_equity_snapshot(pub=tpub, signed=tsigned, symbol=sym_tn)
                    acc = snap["account"]
                    non_zero = snap["non_zero"]
                    srow = snap["snapshot"]
                    bals = acc.get("balances") or []
                    out_acc = {
                        "ok": True,
                        "base_url": _tn_base,
                        "symbol": sym_tn,
                        "canTrade": acc.get("canTrade"),
                        "makerCommission": acc.get("makerCommission"),
                        "takerCommission": acc.get("takerCommission"),
                        "balances_total": len(bals),
                        "balances_non_zero": non_zero[:40],
                        "equity_quote_est": round(float(srow.get("equity_quote") or 0.0), 6),
                        "pnl_vs_start_quote": round(float(srow.get("pnl_vs_start") or 0.0), 6),
                        "mark_price": round(float(srow.get("mark_price") or 0.0), 6),
                    }
                    st.session_state.last_testnet_account = out_acc
                    _append_event_log(
                        "INFO",
                        "testnet",
                        f"account ok | canTrade={out_acc.get('canTrade')} | eq={out_acc.get('equity_quote_est')} {split_pair(sym_tn)[1]} | pnl={out_acc.get('pnl_vs_start_quote'):+.6f}",
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
                    try:
                        _capture_testnet_equity_snapshot(pub=tpub, signed=tsigned, symbol=sym_tn)
                    except Exception:
                        pass
                    if out_smoke.get("ok"):
                        _append_event_log(
                            "INFO",
                            "testnet",
                            f"order-smoke ok | {sym_tn} | price={out_smoke.get('price')} qty={out_smoke.get('quantity')}",
                        )
                    else:
                        _append_event_log("WARN", "testnet", f"order-smoke falhou | {out_smoke}")
            except Exception as e:
                st.error(str(e))
                _append_event_log("ERROR", "testnet", f"erro: {e}")

    last = st.session_state.get("last_out") or {}
    show_chrome = page != "Logs"
    if show_chrome:
        render_market_and_wallet(
            st,
            symbol=st.session_state.symbol,
            last=last,
            api_ping_ok=bool(st.session_state.api_ping_ok),
            wallet=st.session_state.wallet,
        )
        render_stepper(st, last=last)

    eng_state = last.get("engine") or {}

    render_selected_page(
        st,
        page=page,
        last=last,
        cfg=cfg,
        eng_state=eng_state,
    )


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
