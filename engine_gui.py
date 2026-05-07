#!/usr/bin/env python3
"""
engine_gui.py  ―  Interface gráfica para o Motor Binance MK
──────────────────────────────────────────────────────────────
Execução normal  : python engine_gui.py
Sem janela CMD   : pythonw engine_gui.py     (Windows — oculta terminal)

Requer: binance_live_engine.py no mesmo diretório
        pip install python-binance numpy
"""
from __future__ import annotations

import os, sys, queue, threading, importlib.util, types
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from datetime import datetime
from typing import Optional, Tuple

# ── Garante que o motor seja encontrado ──────────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

# ── Importa componentes do motor (motor não é modificado) ─────────────────────
try:
    from binance_live_engine import (
        VirtualBank, BinanceEngine, TradingEngine, AuditLogger,
        _AUDIT_DIR, _clear_session_files,
    )
    from binance.client import Client
    from binance.exceptions import BinanceAPIException
except ImportError as _err:
    _r = tk.Tk()
    _r.withdraw()
    messagebox.showerror(
        "Dependência ausente",
        f"{_err}\n\nExecute no terminal:\n  pip install python-binance numpy",
    )
    sys.exit(1)


# ═══════════════════════════════════════════════════════════════════════════════
#  TEMA — Verde sobre Preto (espelha o terminal do motor)
# ═══════════════════════════════════════════════════════════════════════════════
C_BG     = "#0d0d0d"
C_BG2    = "#141414"
C_BG3    = "#1e1e1e"
C_FG     = "#00e676"
C_FG2    = "#69f0ae"
C_DIM    = "#2e7d32"
C_WHITE  = "#e0e0e0"
C_YELLOW = "#ffd740"
C_RED    = "#ff5252"
C_BLUE   = "#40c4ff"

F_NORMAL = ("Consolas", 10)
F_BOLD   = ("Consolas", 10, "bold")
F_LARGE  = ("Consolas", 13, "bold")
F_SMALL  = ("Consolas", 9)
F_MONO   = ("Consolas", 11)


def _apply_theme(root: tk.Tk) -> None:
    s = ttk.Style(root)
    s.theme_use("clam")
    base = dict(
        background=C_BG, foreground=C_FG, font=F_NORMAL,
        fieldbackground=C_BG2, insertcolor=C_FG,
        bordercolor=C_BG3, lightcolor=C_BG3, darkcolor=C_BG3,
        troughcolor=C_BG2,
    )
    s.configure(".",            **base)
    s.configure("TFrame",       background=C_BG)
    s.configure("TLabel",       background=C_BG, foreground=C_FG, font=F_NORMAL)
    s.configure("TEntry",       fieldbackground=C_BG2, foreground=C_WHITE,
                                insertcolor=C_FG, font=F_NORMAL, relief="flat")
    s.configure("TCombobox",    fieldbackground=C_BG2, foreground=C_WHITE,
                                selectbackground=C_DIM, font=F_NORMAL)
    s.configure("TScrollbar",   background=C_BG2, relief="flat", arrowcolor=C_DIM)
    s.configure("TButton",      background=C_BG2, foreground=C_FG, font=F_BOLD,
                                relief="flat", padding=(12, 6), focuscolor=C_DIM)
    s.map("TButton",
          background=[("active", C_DIM), ("pressed", "#1b5e20")],
          foreground=[("active", C_FG2)])
    s.configure("Red.TButton",  foreground=C_RED)
    s.map("Red.TButton",
          background=[("active", "#3d0a0a")],
          foreground=[("active", "#ff8a80")])
    s.configure("TCheckbutton", background=C_BG, foreground=C_FG)
    s.configure("TRadiobutton", background=C_BG, foreground=C_FG)


# ═══════════════════════════════════════════════════════════════════════════════
#  UTILITÁRIOS
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


def _test_conn(client: Client, symbol: str) -> Tuple[bool, str]:
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
    """AuditLogger que espelha eventos formatados para a fila da GUI."""

    def __init__(self, *args, event_q: queue.Queue, **kwargs):
        super().__init__(*args, **kwargs)
        self._q = event_q

    def log(self, event_type: str, data: dict) -> None:
        super().log(event_type, data)
        # Apenas marcos de sessão: fills/cancels/ordens chegam via engine._log (patch)
        ts = datetime.utcnow().strftime("%H:%M:%S")
        if event_type == "SESSION_START":
            self._q.put_nowait(f"[{ts}] ▶ Sessão iniciada")
        elif event_type == "SESSION_END":
            self._q.put_nowait(f"[{ts}] ■ Sessão encerrada")


# ═══════════════════════════════════════════════════════════════════════════════
#  PÁGINA — CONFIGURAÇÃO
# ═══════════════════════════════════════════════════════════════════════════════

class ConfigPage(ttk.Frame):
    def __init__(self, master: tk.Widget, on_start) -> None:
        super().__init__(master)
        self._on_start    = on_start
        self._plugin:     Optional[types.ModuleType] = None
        self._plugin_path = tk.StringVar()
        self._api_key     = tk.StringVar()
        self._api_secret  = tk.StringVar()
        self._live_mode   = tk.BooleanVar(value=False)
        self._bank_val    = tk.StringVar(value="1000")
        self._sym_var     = tk.StringVar()
        self._sym_map:    dict = {}
        self._status_var  = tk.StringVar()
        self._build()

    # ── Construção da UI ──────────────────────────────────────────────────

    def _lbl(self, parent, text, color=C_FG, font=F_NORMAL, **kw) -> tk.Label:
        return tk.Label(parent, text=text, bg=C_BG, fg=color, font=font, **kw)

    def _sep(self, parent) -> None:
        tk.Frame(parent, bg=C_BG3, height=1).pack(fill="x", padx=16, pady=10)

    def _build(self) -> None:
        self.pack(fill="both", expand=True)

        # Cabeçalho
        hdr = tk.Frame(self, bg=C_BG2)
        hdr.pack(fill="x")
        tk.Label(hdr, text="  ◈  BINANCE MK ENGINE",
                 bg=C_BG2, fg=C_FG2, font=F_LARGE, anchor="w", pady=13
                 ).pack(side="left", fill="x", expand=True, padx=4)
        tk.Label(hdr, text="CONFIGURAÇÃO  ",
                 bg=C_BG2, fg=C_DIM, font=F_SMALL, anchor="e"
                 ).pack(side="right", pady=13)

        body = tk.Frame(self, bg=C_BG)
        body.pack(fill="both", expand=True, padx=28, pady=14)

        # ── Plugin ────────────────────────────────────────────────────────
        self._lbl(body, "PLUGIN DE ESTRATÉGIA", color=C_FG2, font=F_BOLD).pack(anchor="w")
        pl_row = tk.Frame(body, bg=C_BG)
        pl_row.pack(fill="x", pady=(4, 0))
        ttk.Entry(pl_row, textvariable=self._plugin_path,
                  state="readonly").pack(side="left", fill="x", expand=True)
        ttk.Button(pl_row, text="Procurar…",
                   command=self._browse_plugin, width=10
                   ).pack(side="left", padx=(6, 0))
        self._plugin_info = tk.Label(body, text="", bg=C_BG, fg=C_DIM, font=F_SMALL)
        self._plugin_info.pack(anchor="w", pady=(2, 0))

        self._sep(body)

        # ── Credenciais ───────────────────────────────────────────────────
        self._lbl(body, "CREDENCIAIS API BINANCE", color=C_FG2, font=F_BOLD).pack(anchor="w")
        cred = tk.Frame(body, bg=C_BG)
        cred.pack(fill="x", pady=(6, 0))
        for row_i, (lbl, var, show) in enumerate([
            ("API Key",    self._api_key,    ""),
            ("API Secret", self._api_secret, "•"),
        ]):
            tk.Label(cred, text=lbl, bg=C_BG, fg=C_FG, font=F_NORMAL,
                     width=12, anchor="w").grid(row=row_i, column=0, sticky="w", pady=3)
            ttk.Entry(cred, textvariable=var, show=show, width=52
                      ).grid(row=row_i, column=1, sticky="ew", padx=(6, 0), pady=3)
        cred.columnconfigure(1, weight=1)

        self._sep(body)

        # ── Rede / Símbolo / Banca ─────────────────────────────────────────
        self._lbl(body, "CONFIGURAÇÃO DE SESSÃO", color=C_FG2, font=F_BOLD).pack(anchor="w")
        cfg = tk.Frame(body, bg=C_BG)
        cfg.pack(fill="x", pady=(6, 0))

        tk.Label(cfg, text="Rede", bg=C_BG, fg=C_FG, font=F_NORMAL,
                 width=13, anchor="w").grid(row=0, column=0, sticky="w", pady=4)
        net = tk.Frame(cfg, bg=C_BG)
        net.grid(row=0, column=1, sticky="w", padx=(4, 0))
        ttk.Radiobutton(net, text="Testnet (seguro)",
                        variable=self._live_mode, value=False).pack(side="left", padx=(0, 20))
        ttk.Radiobutton(net, text="LIVE  ⚠  dinheiro real",
                        variable=self._live_mode, value=True).pack(side="left")

        tk.Label(cfg, text="Símbolo", bg=C_BG, fg=C_FG, font=F_NORMAL,
                 width=13, anchor="w").grid(row=1, column=0, sticky="w", pady=4)
        self._sym_combo = ttk.Combobox(cfg, textvariable=self._sym_var,
                                       state="disabled", width=32)
        self._sym_combo.grid(row=1, column=1, sticky="w", padx=(4, 0), pady=4)

        tk.Label(cfg, text="Banca (USDT)", bg=C_BG, fg=C_FG, font=F_NORMAL,
                 width=13, anchor="w").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Entry(cfg, textvariable=self._bank_val, width=14
                  ).grid(row=2, column=1, sticky="w", padx=(4, 0), pady=4)
        cfg.columnconfigure(1, weight=1)

        self._sep(body)

        # ── Status + Botão iniciar ─────────────────────────────────────────
        self._status_lbl = tk.Label(body, textvariable=self._status_var,
                                    bg=C_BG, fg=C_YELLOW, font=F_SMALL,
                                    anchor="w", wraplength=530, justify="left")
        self._status_lbl.pack(fill="x")
        self._btn_start = ttk.Button(body, text="▶  INICIAR SESSÃO",
                                     command=self._do_start)
        self._btn_start.pack(pady=(12, 4), ipadx=22, ipady=5)
        tk.Label(body, text="Ctrl+C ou o botão Parar encerram o motor com segurança.",
                 bg=C_BG, fg=C_DIM, font=F_SMALL).pack()

    # ── Eventos ───────────────────────────────────────────────────────────

    def reset_button(self) -> None:
        self._btn_start.config(state="normal")

    def _set_status(self, msg: str, color: str = C_YELLOW) -> None:
        self._status_var.set(msg)
        self._status_lbl.config(fg=color)

    def _browse_plugin(self) -> None:
        path = filedialog.askopenfilename(
            title="Selecionar plugin de estratégia",
            initialdir=_HERE,
            filetypes=[("Python", "*.py"), ("Todos os arquivos", "*.*")],
        )
        if not path:
            return
        mod = _load_plugin(path)
        if mod is None:
            messagebox.showerror("Plugin inválido",
                                 "O arquivo não possui uma função propose() válida.")
            return
        self._plugin = mod
        self._plugin_path.set(path)
        name = getattr(mod, "STRATEGY_DISPLAY_NAME", mod.__name__)
        defs = getattr(mod, "TESTNET_SOAK_DEFAULTS", {})
        cyc  = defs.get("max_cycles", "∞")
        ival = defs.get("interval_sec", "?")
        self._plugin_info.config(
            text=f"{name}   |   Ciclos: {cyc}   Intervalo: {ival}s",
            fg=C_FG2,
        )
        sym_index = getattr(mod, "_SYMBOL_INDEX", {0: "BTCUSDT"})
        self._sym_map = {f"{v}  (idx {k})": k for k, v in sorted(sym_index.items())}
        vals = list(self._sym_map.keys())
        self._sym_combo.config(values=vals, state="readonly")
        if vals:
            self._sym_combo.current(0)
        self._set_status("Plugin carregado.", C_FG)

    def _do_start(self) -> None:
        if self._plugin is None:
            self._set_status("Selecione um plugin antes de iniciar.")
            return
        key = self._api_key.get().strip()
        sec = self._api_secret.get().strip()
        if not key or not sec:
            self._set_status("Preencha a API Key e o API Secret.")
            return
        if not self._sym_var.get():
            self._set_status("Selecione um símbolo.")
            return
        try:
            bank = float(self._bank_val.get().replace(",", "."))
            if bank <= 0:
                raise ValueError("banco negativo")
        except ValueError:
            self._set_status("Valor de banca inválido.")
            return

        sym_idx   = self._sym_map[self._sym_var.get()]
        sym_index = getattr(self._plugin, "_SYMBOL_INDEX", {0: "BTCUSDT"})
        symbol    = sym_index[sym_idx]
        live      = self._live_mode.get()

        self._btn_start.config(state="disabled")
        self._set_status("Conectando à Binance…", C_YELLOW)
        self.update()

        def _connect() -> None:
            client = Client(key, sec, testnet=not live)
            ok, msg = _test_conn(client, symbol)
            self.after(0, lambda: self._after_conn(
                ok, msg, client, key, sec, bank, sym_idx, symbol, live
            ))

        threading.Thread(target=_connect, daemon=True).start()

    def _after_conn(self, ok: bool, msg: str, client: Client,
                    key: str, sec: str, bank: float,
                    sym_idx: int, symbol: str, live: bool) -> None:
        if not ok:
            self._set_status(f"Falha na conexão: {msg}", C_RED)
            self._btn_start.config(state="normal")
            return
        self._set_status(f"Conexão OK — {msg}", C_FG)
        self._on_start(
            plugin=self._plugin, client=client,
            api_key=key, api_secret=sec,
            bank=bank, sym_idx=sym_idx, symbol=symbol, live=live,
        )


# ═══════════════════════════════════════════════════════════════════════════════
#  PÁGINA — MONITOR
# ═══════════════════════════════════════════════════════════════════════════════

class MonitorPage(ttk.Frame):
    _POLL_MS = 1000

    def __init__(self, master: tk.Widget, on_stop, on_download, on_config) -> None:
        super().__init__(master)
        self._on_stop     = on_stop
        self._on_download = on_download
        self._on_config   = on_config
        self._engine:     Optional[TradingEngine] = None
        self._event_q:    queue.Queue = queue.Queue()
        self._started_at: Optional[datetime] = None
        self._last_cycle: int = -1
        self._build()

    # ── Construção da UI ──────────────────────────────────────────────────

    @staticmethod
    def _stat_card(parent: tk.Widget, title: str) -> tk.Label:
        f = tk.Frame(parent, bg=C_BG2, padx=10, pady=6)
        f.pack(side="left", fill="both", expand=True, padx=3)
        tk.Label(f, text=title, bg=C_BG2, fg=C_DIM, font=F_SMALL).pack(anchor="w")
        val = tk.Label(f, text="—", bg=C_BG2, fg=C_WHITE, font=F_MONO)
        val.pack(anchor="w")
        return val

    @staticmethod
    def _order_card(parent: tk.Widget, side: str) -> dict:
        color = C_BLUE if side == "BID" else C_YELLOW
        f = tk.Frame(parent, bg=C_BG2, padx=10, pady=6)
        f.pack(side="left", fill="both", expand=True, padx=3)
        tk.Label(f, text=f"ORDEM {side}", bg=C_BG2, fg=color, font=F_BOLD).pack(anchor="w")
        vals: dict = {}
        for key in ("Status", "Preço", "Qty", "Preenchido"):
            row = tk.Frame(f, bg=C_BG2)
            row.pack(fill="x", pady=1)
            tk.Label(row, text=f"{key}:", bg=C_BG2, fg=C_DIM,
                     font=F_SMALL, width=11, anchor="w").pack(side="left")
            v = tk.Label(row, text="—", bg=C_BG2, fg=C_WHITE, font=F_SMALL)
            v.pack(side="left")
            vals[key] = v
        return vals

    def _build(self) -> None:
        self.pack(fill="both", expand=True)

        # Cabeçalho
        hdr = tk.Frame(self, bg=C_BG2)
        hdr.pack(fill="x")
        self._hdr_title = tk.Label(
            hdr, text="  ◈  MONITOR",
            bg=C_BG2, fg=C_FG2, font=F_LARGE, anchor="w", pady=10,
        )
        self._hdr_title.pack(side="left", fill="x", expand=True, padx=4)
        self._hdr_status = tk.Label(
            hdr, text="⬤ PARADO",
            bg=C_BG2, fg=C_DIM, font=F_BOLD, anchor="e", pady=10,
        )
        self._hdr_status.pack(side="right", padx=12)

        body = tk.Frame(self, bg=C_BG)
        body.pack(fill="both", expand=True, padx=10, pady=8)

        # Linha 1 — banca virtual
        row1 = tk.Frame(body, bg=C_BG)
        row1.pack(fill="x", pady=(0, 4))
        self._v_pnl    = self._stat_card(row1, "PnL TOTAL")
        self._v_equity = self._stat_card(row1, "EQUITY")
        self._v_quote  = self._stat_card(row1, "USDT LIVRE")
        self._v_base   = self._stat_card(row1, "BASE")
        self._v_fills  = self._stat_card(row1, "FILLS")

        # Linha 2 — mercado
        row2 = tk.Frame(body, bg=C_BG)
        row2.pack(fill="x", pady=(0, 4))
        self._v_mid    = self._stat_card(row2, "MID PRICE")
        self._v_atr    = self._stat_card(row2, "ATR %")
        self._v_adx    = self._stat_card(row2, "ADX")
        self._v_cycle  = self._stat_card(row2, "CICLO")
        self._v_time   = self._stat_card(row2, "TEMPO")

        # Linha 3 — ordens ativas
        row3 = tk.Frame(body, bg=C_BG)
        row3.pack(fill="x", pady=(0, 4))
        self._bid_vals = self._order_card(row3, "BID")
        self._ask_vals = self._order_card(row3, "ASK")

        # Log de eventos
        tk.Label(body, text="LOG DE EVENTOS",
                 bg=C_BG, fg=C_DIM, font=F_SMALL).pack(anchor="w", pady=(4, 2))
        log_wrap = tk.Frame(body, bg=C_BG2)
        log_wrap.pack(fill="both", expand=True)
        self._log_txt = tk.Text(
            log_wrap, bg=C_BG2, fg=C_FG, font=F_SMALL,
            height=8, state="disabled", wrap="word",
            relief="flat", cursor="arrow", selectbackground=C_DIM,
        )
        sb = ttk.Scrollbar(log_wrap, orient="vertical", command=self._log_txt.yview)
        self._log_txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self._log_txt.pack(fill="both", expand=True, padx=4, pady=4)
        self._log_txt.tag_config("fill",   foreground=C_FG2)
        self._log_txt.tag_config("cancel", foreground=C_YELLOW)
        self._log_txt.tag_config("order",  foreground=C_BLUE)
        self._log_txt.tag_config("info",   foreground=C_DIM)

        # Rodapé
        footer = tk.Frame(body, bg=C_BG)
        footer.pack(fill="x", pady=(6, 0))
        self._btn_stop = ttk.Button(
            footer, text="⏹  PARAR",
            style="Red.TButton", command=self._on_stop,
        )
        self._btn_stop.pack(side="left", ipadx=10, ipady=4, padx=(0, 6))
        ttk.Button(
            footer, text="↓  Auditoria",
            command=self._on_download,
        ).pack(side="left", ipadx=8, ipady=4, padx=(0, 6))
        self._btn_config = ttk.Button(
            footer, text="◀  Configuração",
            command=self._on_config,
        )
        self._btn_config.pack(side="left", ipadx=8, ipady=4)
        self._footer_note = tk.Label(
            footer, text="", bg=C_BG, fg=C_DIM, font=F_SMALL,
        )
        self._footer_note.pack(side="right")

    # ── API pública ───────────────────────────────────────────────────────

    def attach(self, engine: TradingEngine, event_q: queue.Queue) -> None:
        self._engine     = engine
        self._event_q    = event_q
        self._started_at = datetime.utcnow()
        self._last_cycle = -1
        self._hdr_status.config(text="⬤ EXECUTANDO", fg=C_FG)
        self._btn_stop.config(state="normal")
        self._btn_config.config(state="disabled")
        self._footer_note.config(text="")
        self._poll()

    def detach(self) -> None:
        self._engine = None
        self._hdr_status.config(text="⬤ ENCERRADO", fg=C_RED)
        self._btn_stop.config(state="disabled")
        self._btn_config.config(state="normal")
        self._footer_note.config(
            text="Sessão encerrada — use ◀ Configuração para novo soak.",
            fg=C_YELLOW,
        )

    # ── Polling de estado ─────────────────────────────────────────────────

    def _poll(self) -> None:
        if self._engine is None:
            return
        eng = self._engine
        vb  = eng.vbank

        # Banca virtual
        pnl   = vb.total_pnl
        pnl_c = C_FG2 if pnl >= 0 else C_RED
        self._v_pnl.config(
            text=f"{'+' if pnl >= 0 else ''}{pnl:.4f} USDT", fg=pnl_c
        )
        self._v_equity.config(text=f"${vb.equity:,.4f}")
        self._v_quote.config(text=f"${vb.available_quote:,.4f}")
        sym_base = eng.symbol[:-4] if eng.symbol.endswith("USDT") else eng.symbol[:3]
        self._v_base.config(text=f"{vb.base_balance:.6f} {sym_base}")
        self._v_fills.config(
            text=f"{vb.total_fills}  ↑{vb.buy_fills} ↓{vb.sell_fills}"
        )

        # Mercado
        mid = eng._last_mid
        self._v_mid.config(text=f"${mid:,.4f}" if mid else "—")
        self._v_atr.config(
            text=f"{eng._last_atr:.4f}%" if eng._last_atr else "—"
        )
        self._v_adx.config(
            text=f"{eng._last_adx:.1f}" if eng._last_adx else "—"
        )
        self._v_cycle.config(text=str(eng.cycle))

        if self._started_at:
            secs    = int((datetime.utcnow() - self._started_at).total_seconds())
            h, rem  = divmod(secs, 3600)
            m, s    = divmod(rem, 60)
            self._v_time.config(text=f"{h:02d}:{m:02d}:{s:02d}")

        # Ordens
        self._refresh_order(self._bid_vals, eng.binance.bid_order, "BID")
        self._refresh_order(self._ask_vals, eng.binance.ask_order, "ASK")

        # Tick de mercado a cada novo ciclo (mostra atividade ao usuário)
        if eng.cycle != self._last_cycle and eng.cycle > 0:
            self._last_cycle = eng.cycle
            ts = datetime.utcnow().strftime("%H:%M:%S")
            bid_tag = "↑" if eng.binance.bid_order else "·"
            ask_tag = "↑" if eng.binance.ask_order else "·"
            self._event_q.put_nowait(
                f"[{ts}] ◌ C{eng.cycle:04d}"
                f"  ${mid:,.2f}"
                f"  ATR {eng._last_atr:.3f}%"
                f"  ADX {eng._last_adx:.1f}"
                f"  BID{bid_tag} ASK{ask_tag}"
            )

        # Drena fila de eventos → log
        try:
            while True:
                self._append_log(self._event_q.get_nowait())
        except queue.Empty:
            pass

        # Título dinâmico
        strat_name = getattr(eng._strat, "STRATEGY_DISPLAY_NAME", eng._strat.__name__)
        net_lbl    = "LIVE" if eng.live else "TESTNET"
        self._hdr_title.config(
            text=f"  ◈  {eng.symbol}  [{net_lbl}]  {strat_name}"
        )

        if eng.running:
            self.after(self._POLL_MS, self._poll)
        else:
            self.detach()

    @staticmethod
    def _refresh_order(vals: dict, order, side: str) -> None:
        if order is None:
            for v in vals.values():
                v.config(text="—", fg=C_DIM)
            return
        pct   = (order.filled_qty / order.qty * 100) if order.qty > 0 else 0.0
        color = C_BLUE if side == "BID" else C_YELLOW
        vals["Status"].config(    text=order.status,                     fg=color)
        vals["Preço"].config(     text=f"${order.price:,.4f}",           fg=C_WHITE)
        vals["Qty"].config(       text=f"{order.qty:.6f}",               fg=C_WHITE)
        vals["Preenchido"].config(text=f"{order.filled_qty:.6f} ({pct:.0f}%)", fg=C_WHITE)

    def _append_log(self, msg: str) -> None:
        tag = "info"
        if "FILL"   in msg: tag = "fill"
        elif "CANCEL" in msg: tag = "cancel"
        elif "ORDEM"  in msg: tag = "order"
        self._log_txt.config(state="normal")
        self._log_txt.insert("end", msg + "\n", tag)
        self._log_txt.see("end")
        self._log_txt.config(state="disabled")


# ═══════════════════════════════════════════════════════════════════════════════
#  JANELA DE DOWNLOAD DE AUDITORIA
# ═══════════════════════════════════════════════════════════════════════════════

def _open_download_window(parent: tk.Widget) -> None:
    win = tk.Toplevel(parent)
    win.title("Download de Auditoria")
    win.geometry("420x200")
    win.resizable(False, False)
    win.configure(bg=C_BG)
    win.grab_set()

    tk.Label(win, text="DOWNLOAD DE ARQUIVOS DE AUDITORIA",
             bg=C_BG, fg=C_FG2, font=F_BOLD).pack(pady=(18, 6))
    tk.Label(win, text="Escolha o método:",
             bg=C_BG, fg=C_FG, font=F_NORMAL).pack()

    def _audit_files():
        return [
            f for f in os.listdir(_AUDIT_DIR)
            if f.startswith("audit_") and (f.endswith(".csv") or f.endswith(".json"))
        ]

    def _do_zip() -> None:
        import zipfile as zf
        files = _audit_files()
        if not files:
            messagebox.showinfo("Vazio", "Nenhum arquivo de auditoria encontrado.", parent=win)
            return
        ts   = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        dest = filedialog.asksaveasfilename(
            parent=win,
            title="Salvar ZIP de auditoria",
            initialfile=f"auditoria_{ts}.zip",
            defaultextension=".zip",
            filetypes=[("ZIP", "*.zip")],
        )
        if not dest:
            return
        with zf.ZipFile(dest, "w", zf.ZIP_DEFLATED) as z:
            for fn in files:
                z.write(os.path.join(_AUDIT_DIR, fn), fn)
        messagebox.showinfo("Concluído", f"ZIP salvo:\n{dest}", parent=win)
        win.destroy()

    def _do_http() -> None:
        import http.server, socketserver, socket as sk
        port = 8765

        class _H(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *a, **kw):
                super().__init__(*a, directory=_AUDIT_DIR, **kw)
            def log_message(self, *_):
                pass

        try:
            httpd = socketserver.TCPServer(("", port), _H, bind_and_activate=False)
            httpd.allow_reuse_address = True
            httpd.server_bind()
            httpd.server_activate()
        except OSError:
            messagebox.showerror("Porta ocupada",
                                 f"Porta {port} já em uso.\nUse o método ZIP.", parent=win)
            return

        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            ip = sk.gethostbyname(sk.gethostname())
        except Exception:
            ip = "127.0.0.1"

        info = tk.Toplevel(win)
        info.title("Servidor HTTP ativo")
        info.geometry("400x160")
        info.resizable(False, False)
        info.configure(bg=C_BG)
        tk.Label(info, text="Servidor HTTP ativo — abra no navegador:",
                 bg=C_BG, fg=C_FG2, font=F_BOLD).pack(pady=(18, 6))
        url = tk.StringVar(value=f"http://{ip}:{port}/")
        tk.Entry(info, textvariable=url, state="readonly",
                 bg=C_BG2, fg=C_FG2, font=F_BOLD,
                 readonlybackground=C_BG2, relief="flat"
                 ).pack(fill="x", padx=20, pady=6)

        def _stop():
            httpd.shutdown()
            info.destroy()
            win.destroy()

        ttk.Button(info, text="Encerrar servidor", command=_stop
                   ).pack(pady=4, ipadx=10, ipady=4)
        win.destroy()

    btn_row = tk.Frame(win, bg=C_BG)
    btn_row.pack(pady=18)
    ttk.Button(btn_row, text="📦  Baixar ZIP",
               command=_do_zip).pack(side="left", padx=10, ipadx=12, ipady=5)
    ttk.Button(btn_row, text="🌐  Servidor HTTP",
               command=_do_http).pack(side="left", padx=10, ipadx=12, ipady=5)
    ttk.Button(btn_row, text="Cancelar",
               command=win.destroy).pack(side="left", padx=10, ipadx=12, ipady=5)


# ═══════════════════════════════════════════════════════════════════════════════
#  APLICAÇÃO PRINCIPAL
# ═══════════════════════════════════════════════════════════════════════════════

class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Binance MK Engine")
        self.geometry("740x630")
        self.minsize(700, 580)
        self.configure(bg=C_BG)
        _apply_theme(self)

        self._engine:      Optional[TradingEngine] = None
        self._eng_thread:  Optional[threading.Thread] = None
        self._event_q:     queue.Queue = queue.Queue()

        self._cfg_page = ConfigPage(self, on_start=self._start_session)
        self._mon_page = MonitorPage(
            self,
            on_stop=self._stop_engine,
            on_download=lambda: _open_download_window(self),
            on_config=self._go_to_config,
        )
        self._show_config()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── Navegação ─────────────────────────────────────────────────────────

    def _show_config(self) -> None:
        self._mon_page.pack_forget()
        self._cfg_page.pack(fill="both", expand=True)

    def _show_monitor(self) -> None:
        self._cfg_page.pack_forget()
        self._mon_page.pack(fill="both", expand=True)

    # ── Ciclo de sessão ───────────────────────────────────────────────────

    def _start_session(self, *, plugin, client, api_key, api_secret,
                       bank, sym_idx, symbol, live) -> None:
        # Limpa arquivos da sessão anterior
        _clear_session_files(_AUDIT_DIR)
        while not self._event_q.empty():
            try:
                self._event_q.get_nowait()
            except queue.Empty:
                break

        session_id = (
            f"{plugin.__name__}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
        )
        mode_lbl = str(getattr(plugin, "STRATEGY_DISPLAY_NAME", "PLUGIN")).upper()

        vbank     = VirtualBank(initial_quote=bank, quote_balance=bank, base_balance=0.0)
        binance_e = BinanceEngine(client=client, vbank=vbank, symbol=symbol)
        logger    = _GUILogger(
            session_id=session_id, out_dir=_AUDIT_DIR, event_q=self._event_q
        )

        self._engine = TradingEngine(
            binance=binance_e, vbank=vbank, logger=logger,
            symbol=symbol, sym_idx=sym_idx,
            mode=0,   # _ask_config sempre retorna 0; preservado aqui
            live=live, strat=plugin,
        )
        # Suprime o dashboard ANSI do terminal — a GUI é o display
        self._engine._display = lambda: None  # type: ignore[method-assign]

        # Encaminha mensagens internas do motor (fills, cancels, ordens, parking…)
        # para a fila da GUI sem alterar a classe — patch só nesta instância.
        _eq        = self._event_q
        _orig_log  = self._engine._log
        def _fwd_log(msg: str, _orig=_orig_log) -> None:
            _orig(msg)                          # comportamento original preservado
            if self._engine and self._engine._log_lines:
                _eq.put_nowait(self._engine._log_lines[-1])
        self._engine._log = _fwd_log  # type: ignore[method-assign]

        logger.log("SESSION_START", {
            "symbol":     symbol,
            "mode_label": mode_lbl,
            "notes":      f"live={live}  vb_initial={bank}  plugin={plugin.__name__}",
            "cycle":      0,
        })

        self._show_monitor()
        self._mon_page.attach(self._engine, self._event_q)

        self._eng_thread = threading.Thread(
            target=self._run_engine, daemon=True, name="mk-engine"
        )
        self._eng_thread.start()

    def _run_engine(self) -> None:
        try:
            self._engine.run()  # type: ignore[union-attr]
        except Exception as exc:
            self._event_q.put_nowait(f"[!] ERRO: {exc}")
        finally:
            self.after(0, self._after_session)

    def _after_session(self) -> None:
        # Apenas atualiza o estado visual — o botão ◀ Configuração fica disponível.
        # Não usa dialog: o usuário decide quando e se quer iniciar nova sessão.
        self._mon_page.detach()

    def _go_to_config(self) -> None:
        """Navega para a tela de configuração. Desabilitado enquanto o motor roda."""
        if self._engine and self._engine.running:
            messagebox.showwarning(
                "Motor em execução",
                "Pare o motor antes de voltar à configuração.\n"
                "Use o botão ⏹ PARAR.",
            )
            return
        self._cfg_page.reset_button()
        self._show_config()

    def _stop_engine(self) -> None:
        if self._engine and self._engine.running:
            self._engine.shutdown()

    # ── Fechamento da janela ──────────────────────────────────────────────

    def _on_close(self) -> None:
        if self._engine and self._engine.running:
            if not messagebox.askyesno(
                "Motor em execução",
                "O motor está rodando.\nDeseja encerrar?",
            ):
                return
            self._engine.shutdown()
            if self._eng_thread:
                self._eng_thread.join(timeout=6)
        self.destroy()


# ═══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    App().mainloop()


if __name__ == "__main__":
    main()
