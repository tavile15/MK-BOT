from __future__ import annotations

from typing import Any

from microtrans.virtual_wallet import VirtualWallet


def render_topbar(
    st: Any,
    *,
    page: str,
    symbol: str,
    api_ping_ok: bool,
) -> None:
    c1, c2 = st.columns([1.65, 1.0], gap="small")
    with c1:
        st.markdown(
            f"""
            <div class="mt-topbar">
              <div class="mt-topbar-title">MK BOT - MARKET MAKING · {page}</div>
              <div class="mt-topbar-sub">Par ativo: <b>{symbol}</b> · estado API: {'ok' if api_ping_ok else 'sem ping'}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    with c2:
        st.text_input(
            "Busca rápida",
            key="ui_quick_search",
            placeholder="Filtrar por evento, par, status ou contexto...",
            label_visibility="collapsed",
        )


def render_market_and_wallet(
    st: Any,
    *,
    symbol: str,
    last: dict[str, Any],
    api_ping_ok: bool,
    wallet: VirtualWallet,
) -> None:
    m24 = last.get("market_24h") or {}
    chg = m24.get("variacao_pct_24h")
    mid = float(last.get("mid") or 0)
    chg_cls = "mt-up" if (chg is not None and float(chg) >= 0) else "mt-down"
    chg_txt = f"{float(chg):+.2f}%" if chg is not None else "—"
    volq = m24.get("volume_quote_24h")
    vol_txt = f"{float(volq):,.0f}" if volq is not None else "—"
    mid_disp = f"{mid:,.2f}" if mid > 0 else "—"

    wmid = mid if mid > 0 else 1.0
    sm = wallet.summary(wmid)
    pnl = float(sm.get("pnl_vs_inicio_quote", 0.0))
    pnl_cls = "mt-wallet-pnl-up" if pnl >= 0 else "mt-wallet-pnl-down"
    carteira_box = f"""
    <div class="mt-wallet-box">
      <div class="mt-wallet-title">Saldo Estimado</div>
      <div class="mt-wallet-row"><span class="mt-wallet-muted">Quote</span><span>{sm.get("ativo_quote","QUOTE")}: {sm.get("quote_saldo",0):,.4f}</span></div>
      <div class="mt-wallet-row"><span class="mt-wallet-muted">Base</span><span>{sm.get("ativo_base","BASE")}: {sm.get("base_saldo",0):,.8f}</span></div>
      <div class="mt-wallet-row"><span class="mt-wallet-muted">Patrimônio</span><span>{sm.get("patrimonio_quote",0):,.4f} {sm.get("ativo_quote","QUOTE")}</span></div>
      <div class="mt-wallet-row"><span class="mt-wallet-muted">PnL</span><span class="{pnl_cls}">{pnl:+,.4f}</span></div>
      <div class="mt-wallet-row"><span class="mt-wallet-muted">Taxas (acc.)</span><span>{sm.get("taxas_pagas_total_quote",0):,.4f}</span></div>
      <div class="mt-wallet-row"><span class="mt-wallet-muted">Ops</span><span>{sm.get("n_operacoes",0)}</span></div>
    </div>
    """

    bar_html = f"""
    <div class="mt-bar">
      <div><span class="mt-pair">{symbol}</span>
        <span class="mt-muted"> · spot</span></div>
      <div class="mt-price {chg_cls}">{mid_disp} USDT</div>
      <div class="mt-muted">24h &nbsp;
        <span class="{chg_cls}">{chg_txt}</span></div>
      <div class="mt-muted">High {m24.get("high_24h", "—")} · Low {m24.get("low_24h", "—")}</div>
      <div class="mt-muted">Vol 24h (quote) {vol_txt}</div>
      <div class="mt-muted">API: {'ok' if api_ping_ok else '—'}</div>
    </div>
    """
    c_bar, c_wallet = st.columns([2.05, 0.95], gap="medium")
    with c_bar:
        st.markdown(bar_html, unsafe_allow_html=True)
    with c_wallet:
        st.markdown(carteira_box, unsafe_allow_html=True)


def render_stepper(st: Any, *, last: dict[str, Any]) -> None:
    eng_state = last.get("engine") or {}
    step1 = bool(last)
    step2 = bool(last.get("filter_apt"))
    step3 = bool(eng_state.get("params") or last.get("event") == "cycle_start")
    step4 = bool(eng_state.get("active"))
    s1c = "ok" if step1 else "wait"
    s2c = "ok" if step2 else "wait"
    s3c = "run" if step3 else "wait"
    s4c = "ok" if step4 else "wait"
    st.markdown(
        f"""
        <div class="mt-stepper">
          <div class="mt-step {s1c}">1 · Dados & book</div>
          <div class="mt-step {s2c}">2 · Filtros matemáticos</div>
          <div class="mt-step {s3c}">3 · Agente (parâmetros)</div>
          <div class="mt-step {s4c}">4 · Motor papel</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

