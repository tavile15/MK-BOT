from __future__ import annotations

from typing import Any


def render_sidebar(
    st: Any,
    *,
    yaml_static: dict[str, Any],
    agent_defaults: dict[str, Any],
    eng_defaults: dict[str, Any],
    kline_intervals: list[str],
    gemini_key_present: bool,
) -> dict[str, Any]:
    _ = (kline_intervals, gemini_key_present)
    # Preserva defaults de estado sem expor controles técnicos na sidebar.
    if "ui_page" not in st.session_state:
        st.session_state.ui_page = "Tela Inicial"
    else:
        legacy_map = {
            "Painel": "Tela Inicial",
            "Mercado": "Vigília e OP",
            "Carteira": "Vigília e OP",
            "Auditoria": "Logs",
        }
        st.session_state.ui_page = legacy_map.get(str(st.session_state.ui_page), st.session_state.ui_page)
    if "symbol" not in st.session_state:
        st.session_state.symbol = "BTCUSDT"
    if "relax_liquidity" not in st.session_state:
        st.session_state.relax_liquidity = False
    if "ui_agent_provider" not in st.session_state:
        st.session_state.ui_agent_provider = str(agent_defaults.get("provider") or "heuristic").strip().lower()
    if "ui_paper_pricing" not in st.session_state:
        st.session_state.ui_paper_pricing = str(eng_defaults.get("paper_pricing") or "book_top").strip().lower()
    if "ui_paper_slip" not in st.session_state:
        st.session_state.ui_paper_slip = float(eng_defaults.get("paper_slippage_bps", 0.0))
    if "ui_paper_fill" not in st.session_state:
        st.session_state.ui_paper_fill = float(eng_defaults.get("paper_fill_probability", 1.0))
    if "ui_exit_dd" not in st.session_state:
        st.session_state.ui_exit_dd = float(eng_defaults.get("exit_drawdown_pct", 3.0))
    if "ui_tp_pct" not in st.session_state:
        st.session_state.ui_tp_pct = float(eng_defaults.get("exit_take_profit_pct", 0.0))
    if "ui_tp_quote" not in st.session_state:
        st.session_state.ui_tp_quote = float(eng_defaults.get("exit_take_profit_quote", 0.0))
    if "ui_auto_dur" not in st.session_state:
        st.session_state.ui_auto_dur = 120
    if "ui_auto_step" not in st.session_state:
        st.session_state.ui_auto_step = 15
    if "ui_bt_mode" not in st.session_state:
        st.session_state.ui_bt_mode = "last_n"
    if "ui_bt_step" not in st.session_state:
        st.session_state.ui_bt_step = 1
    if "ui_bt_preset" not in st.session_state:
        st.session_state.ui_bt_preset = "(nenhum)"
    if "ui_bt_force_heuristic" not in st.session_state:
        st.session_state.ui_bt_force_heuristic = False
    if "ui_bt_symbol" not in st.session_state:
        st.session_state.ui_bt_symbol = ""
    if "ui_bt_bars" not in st.session_state:
        bty = yaml_static.get("backtest") or {}
        st.session_state.ui_bt_bars = int(bty.get("default_bars", 500))
    if "ui_bt_fix_end" not in st.session_state:
        st.session_state.ui_bt_fix_end = False
    if "ui_bt_interval" not in st.session_state:
        fc = dict(yaml_static.get("filters") or {})
        st.session_state.ui_bt_interval = str(fc.get("kline_interval", "5m"))
    if "ui_tn_symbol" not in st.session_state:
        st.session_state.ui_tn_symbol = "BTCUSDT"
    if "ui_tn_ticks_below" not in st.session_state:
        st.session_state.ui_tn_ticks_below = 20

    with st.sidebar:
        st.markdown(
            """
            <div class="mt-side-brand">
              <div class="mt-side-brand-icon mt-wolf-icon" aria-label="wolf-mark">
                <svg viewBox="0 0 64 64" xmlns="http://www.w3.org/2000/svg" role="img" aria-hidden="true">
                  <polygon points="9,25 22,8 25,28" fill="#84ff4a"/>
                  <polygon points="55,25 42,8 39,28" fill="#84ff4a"/>
                  <polygon points="32,16 14,30 18,49 32,58 46,49 50,30" fill="#101417" stroke="#84ff4a" stroke-width="2"/>
                  <circle cx="25" cy="36" r="2.5" fill="#84ff4a"/>
                  <circle cx="39" cy="36" r="2.5" fill="#84ff4a"/>
                  <polygon points="32,40 28,45 36,45" fill="#84ff4a"/>
                </svg>
              </div>
              <div>
                <div class="mt-side-brand-title">MK BOT - MARKET MAKING</div>
                <div class="mt-side-brand-sub">Lobo em pele de cordeiro</div>
              </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        st.markdown(
            """
            <div class="mt-card soft">
              <span class="mt-badge live">MK BOT - MARKET MAKING</span>
              <div style="margin-top:8px;color:#A0A0A5;font-size:0.84rem;">
                Hub de navegacao do produto.<br/>
                As configuracoes e acoes ficam dentro de cada pagina.
              </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        st.markdown('<div class="mt-nav-title">Menu</div>', unsafe_allow_html=True)
        nav_items = [
            ("Tela Inicial", "🏠 Tela Inicial"),
            ("Backtest", "🧪 Backtest"),
            ("Vigília e OP", "⚙️ Vigília e OP"),
            ("Testnet", "🛰️ Testnet"),
            ("Logs", "🖥️ Logs"),
        ]
        for page_key, page_label in nav_items:
            is_active = st.session_state.get("ui_page") == page_key
            if st.button(
                page_label,
                key=f"ui_nav_{page_key.lower().replace(' ', '_')}",
                use_container_width=True,
                type="primary" if is_active else "secondary",
            ):
                st.session_state.ui_page = page_key
        page = str(st.session_state.get("ui_page") or "Tela Inicial")
        st.caption("Acoes e configuracoes ficam dentro das paginas.")

    return {
        "page": page,
        "run_auto": False,
        "do_ping": False,
        "do_bt_sidebar": False,
        "do_tn_account_sidebar": False,
        "do_tn_smoke_sidebar": False,
    }

