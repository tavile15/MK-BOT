#!/usr/bin/env python3
"""
Micro-Banca v9 — Motor Binance Live/Testnet
Conexão direta com a API Binance para operações de Market Making.

REGRA ABSOLUTA: A lógica da estratégia em micro_banca_v9.py NÃO É ALTERADA.
Este módulo importa propose() e executa as ordens de acordo com os parâmetros
retornados. Nenhuma linha de micro_banca_v9.py é modificada.

Dependências:
    pip install python-binance numpy

Uso:
    python binance_live_engine.py
    (Altere LIVE_MODE abaixo para trocar entre Testnet e Live)
"""
from __future__ import annotations
import sys, os, re, time, csv, json, math, signal, getpass, threading, types, importlib.util
import zipfile, http.server, socketserver, socket
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional, List, Tuple, Dict, Any

import numpy as np

# Pasta base da aplicação (script em dev, pasta do .exe em modo frozen)
_APP_DIR = (
    os.path.dirname(os.path.abspath(sys.executable))
    if getattr(sys, "frozen", False)
    else os.path.dirname(os.path.abspath(__file__))
)

# ═══════════════════════════════════════════════════════════════════════════
#  CHAVE PRINCIPAL — altere aqui para trocar o modo de operação
#
#  False  =  Testnet  (https://testnet.binance.vision)  — SEGURO PARA TESTAR
#  True   =  Live     (api.binance.com)                 — DINHEIRO REAL ⚠
# ═══════════════════════════════════════════════════════════════════════════
LIVE_MODE: bool = False
# ═══════════════════════════════════════════════════════════════════════════

# Plugin de estratégia — carregado dinamicamente no wizard de startup
_strat: Optional[types.ModuleType] = None

try:
    from binance.client import Client
    from binance.exceptions import BinanceAPIException, BinanceOrderException
except ImportError:
    print("ERRO: python-binance não instalado.\nExecute: pip install python-binance")
    sys.exit(1)


# ═══════════════════════════════════════════════════════════════════════════
# TERMINAL — Verde sobre Preto
# ═══════════════════════════════════════════════════════════════════════════

def _enable_ansi() -> None:
    if sys.platform == "win32":
        try:
            import ctypes
            k32 = ctypes.windll.kernel32
            k32.SetConsoleMode(k32.GetStdHandle(-11), 7)
        except Exception:
            pass

_enable_ansi()

G   = "\033[92m"      # Verde
BG  = "\033[1;92m"    # Verde brilhante / negrito
Y   = "\033[93m"      # Amarelo (alertas)
R   = "\033[91m"      # Vermelho (erros / perda)
W   = "\033[97m"      # Branco brilhante
DIM = "\033[2;32m"    # Verde escuro (dim)
BOX = "\033[32m"      # Bordas verdes
RST = "\033[0m"       # Reset

_ANSI_RE = re.compile(r'\033\[[0-9;]*m')
BOX_W = 74  # largura total da caixa incluindo bordas


def _strip(s: str) -> str:
    return _ANSI_RE.sub("", s)


def _clr() -> None:
    print("\033[2J\033[H", end="", flush=True)


def _box_top() -> str:
    return BOX + "╔" + "═" * (BOX_W - 2) + "╗" + RST


def _box_sep() -> str:
    return BOX + "╠" + "═" * (BOX_W - 2) + "╣" + RST


def _box_bot() -> str:
    return BOX + "╚" + "═" * (BOX_W - 2) + "╝" + RST


def _row(content: str) -> str:
    inner = "  " + content
    pad = BOX_W - 2 - len(_strip(inner))
    return BOX + "║" + RST + inner + " " * max(0, pad) + BOX + "║" + RST


def _pnl_str(val: float) -> str:
    if val > 0:
        return BG + f"+${val:.4f}" + RST
    elif val < 0:
        return R + f"-${abs(val):.4f}" + RST
    return G + f"${val:.4f}" + RST


# ═══════════════════════════════════════════════════════════════════════════
# VIRTUAL BANK
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class VirtualBank:
    """
    Banca virtual que define os tetos de operação do bot.

    Em modo Live:    mesmo que o saldo real seja maior, apenas
                     initial_quote é utilizado para operações.
    Em modo Testnet: funciona da mesma forma, mas com saldo simulado.

    O PnL é calculado mark-to-market (equity = quote + base * mid).
    """
    initial_quote: float     # capital inicial virtual (USDT)
    quote_balance: float     # USDT livre
    base_balance: float = 0.0
    quote_reserved: float = 0.0   # USDT em ordens BID abertas
    base_reserved: float = 0.0    # base em ordens ASK abertas
    realized_pnl: float = 0.0
    total_fills: int = 0
    buy_fills: int = 0
    sell_fills: int = 0
    avg_cost_base: float = 0.0    # custo médio do ativo base

    _mid: float = field(default=0.0, repr=False)

    def update_mid(self, price: float) -> None:
        if price > 0:
            self._mid = price

    @property
    def equity(self) -> float:
        return self.quote_balance + self.base_balance * self._mid

    @property
    def total_pnl(self) -> float:
        return self.equity - self.initial_quote

    @property
    def available_quote(self) -> float:
        return max(0.0, self.quote_balance - self.quote_reserved)

    @property
    def available_base(self) -> float:
        return max(0.0, self.base_balance - self.base_reserved)

    def reserve_bid(self, qty: float, price: float) -> None:
        self.quote_reserved += qty * price

    def reserve_ask(self, qty: float) -> None:
        self.base_reserved += qty

    def release(self, side: str, qty: float, price: float) -> None:
        if side == "BUY":
            self.quote_reserved = max(0.0, self.quote_reserved - qty * price)
        else:
            self.base_reserved = max(0.0, self.base_reserved - qty)

    def on_fill(self, side: str, qty: float, avg_price: float) -> float:
        """Atualiza saldos após fill. Retorna PnL do trade (só vendas)."""
        trade_pnl = 0.0
        if side == "BUY":
            cost = qty * avg_price
            self.quote_balance = max(0.0, self.quote_balance - cost)
            self.quote_reserved = max(0.0, self.quote_reserved - cost)
            total_base = self.base_balance + qty
            if total_base > 0:
                self.avg_cost_base = (
                    self.base_balance * self.avg_cost_base + cost
                ) / total_base
            self.base_balance += qty
            self.buy_fills += 1
        else:
            proceeds = qty * avg_price
            trade_pnl = (avg_price - self.avg_cost_base) * qty
            self.realized_pnl += trade_pnl
            self.quote_balance += proceeds
            self.base_reserved = max(0.0, self.base_reserved - qty)
            self.base_balance = max(0.0, self.base_balance - qty)
            self.sell_fills += 1
        self.total_fills += 1
        return trade_pnl

    def to_ctx(self) -> dict:
        """Retorna campos esperados pelo propose() da estratégia."""
        return {
            "virtual_bank_initial_quote":   self.initial_quote,
            "virtual_bank_equity_quote_now": self.equity,
            "virtual_bank_pnl_quote":        self.total_pnl,
            "inventory_base":                self.base_balance,
        }


# ═══════════════════════════════════════════════════════════════════════════
# AUDIT LOGGER
# ═══════════════════════════════════════════════════════════════════════════

class AuditLogger:
    """
    Registra todas as operações em CSV e JSON para auditoria profunda.
      CSV  — uma linha por evento, abre sempre em append (nunca perde dados)
      JSON — snapshot completo salvo periodicamente e no encerramento
    """

    _CSV_FIELDS = [
        "timestamp_utc", "session_id", "event_type",
        "symbol", "side", "order_id",
        "price", "qty_base", "qty_quote",
        "fill_price", "fill_qty_base", "trade_pnl",
        "vb_equity", "vb_quote_bal", "vb_base_bal",
        "vb_realized_pnl", "vb_total_pnl",
        "spread_bps", "mid_price", "atr_pct", "adx",
        "skew", "cycle", "mode_label", "notes",
    ]

    def __init__(self, session_id: str, out_dir: str = "."):
        self.session_id = session_id
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        self.csv_path  = os.path.join(out_dir, f"audit_{ts}_{session_id}.csv")
        self.json_path = os.path.join(out_dir, f"audit_{ts}_{session_id}.json")
        self._events: List[dict] = []
        self._lock = threading.Lock()
        self._init_csv()

    def _init_csv(self) -> None:
        with open(self.csv_path, "w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=self._CSV_FIELDS).writeheader()

    def log(self, event_type: str, data: dict) -> None:
        ts = datetime.utcnow().isoformat(timespec="milliseconds")
        row_csv = {k: "" for k in self._CSV_FIELDS}
        row_csv.update({"timestamp_utc": ts, "session_id": self.session_id,
                        "event_type": event_type})
        row_csv.update({k: v for k, v in data.items() if k in self._CSV_FIELDS})

        full = {"timestamp_utc": ts, "session_id": self.session_id,
                "event_type": event_type, **data}

        with self._lock:
            with open(self.csv_path, "a", newline="", encoding="utf-8") as f:
                csv.DictWriter(f, fieldnames=self._CSV_FIELDS,
                               extrasaction="ignore").writerow(row_csv)
            self._events.append(full)

    def save_json(self) -> None:
        with self._lock:
            payload = {
                "session_id":   self.session_id,
                "saved_at_utc": datetime.utcnow().isoformat(),
                "total_events": len(self._events),
                "events":       self._events,
            }
            with open(self.json_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, default=str)


# ═══════════════════════════════════════════════════════════════════════════
# INDICADORES TÉCNICOS
# ═══════════════════════════════════════════════════════════════════════════

def _wilder_ema(data: np.ndarray, period: int) -> np.ndarray:
    out = np.zeros(len(data))
    if len(data) < period:
        return out
    out[period - 1] = float(np.mean(data[:period]))
    alpha = 1.0 / period
    for i in range(period, len(data)):
        out[i] = out[i - 1] * (1.0 - alpha) + data[i] * alpha
    return out


def calc_atr_adx(klines: list) -> Tuple[float, float]:
    """
    Retorna (atr_pct, adx) a partir dos klines da Binance.
    klines: [[open_time, open, high, low, close, volume, ...], ...]
    atr_pct é ATR/close (decimal, não porcentagem — ex: 0.0082 para 0.82%)
    """
    if len(klines) < 30:
        return 0.0, 0.0

    highs  = np.array([float(k[2]) for k in klines])
    lows   = np.array([float(k[3]) for k in klines])
    closes = np.array([float(k[4]) for k in klines])
    n = len(closes)

    tr       = np.zeros(n)
    plus_dm  = np.zeros(n)
    minus_dm = np.zeros(n)

    for i in range(1, n):
        hl  = highs[i] - lows[i]
        hpc = abs(highs[i] - closes[i - 1])
        lpc = abs(lows[i]  - closes[i - 1])
        tr[i] = max(hl, hpc, lpc)
        up = highs[i] - highs[i - 1]
        dn = lows[i - 1] - lows[i]
        if up > dn and up > 0:
            plus_dm[i] = up
        if dn > up and dn > 0:
            minus_dm[i] = dn

    period  = 14
    atr14   = _wilder_ema(tr, period)
    plus14  = _wilder_ema(plus_dm, period)
    minus14 = _wilder_ema(minus_dm, period)

    atr_val = float(atr14[-1])
    atr_pct = atr_val / float(closes[-1]) if closes[-1] > 0 else 0.0

    safe_atr = atr14 + 1e-10
    plus_di  = plus14  / safe_atr * 100.0
    minus_di = minus14 / safe_atr * 100.0
    dx       = np.abs(plus_di - minus_di) / (plus_di + minus_di + 1e-10) * 100.0
    adx_val  = float(_wilder_ema(dx, period)[-1])

    # Guarda contra NaN/Inf que se propagariam silenciosamente para propose()
    if not math.isfinite(atr_pct) or not math.isfinite(adx_val):
        return 0.0, 0.0

    return float(atr_pct), float(adx_val)


def calc_liquidity(order_book: dict, levels: int = 5) -> float:
    """Soma dos primeiros 'levels' níveis bid em USDT."""
    total = 0.0
    for price_s, qty_s in (order_book.get("bids") or [])[:levels]:
        try:
            total += float(price_s) * float(qty_s)
        except (ValueError, TypeError):
            pass
    return total


# ═══════════════════════════════════════════════════════════════════════════
# ORDER INFO
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class OrderInfo:
    order_id:    str
    side:        str      # "BUY" | "SELL"
    price:       float
    qty:         float    # quantidade em base
    placed_at:   float    # timestamp unix
    symbol:      str
    status:        str   = "NEW"
    filled_qty:    float = 0.0
    filled_quote:  float = 0.0   # USDT acumulado (rastreia cummulativeQuoteQty)
    _unknown_hits: int   = 0     # contagem de retornos UNKNOWN consecutivos


# ═══════════════════════════════════════════════════════════════════════════
# BINANCE ENGINE
# ═══════════════════════════════════════════════════════════════════════════

class BinanceEngine:
    """
    Gerencia a conexão com a API Binance e a execução de ordens.
    Todas as ordens são filtradas pela banca virtual antes de envio.
    """

    def __init__(self, client: Client, vbank: VirtualBank, symbol: str):
        self.client  = client
        self.vbank   = vbank
        self.symbol  = symbol.upper()

        # Filtros do par (carregados da exchange)
        self.step_size:    float = 1e-8
        self.tick_size:    float = 0.01
        self.min_notional: float = 10.0
        self.qty_prec:     int   = 8
        self.price_prec:   int   = 2

        self.bid_order: Optional[OrderInfo] = None
        self.ask_order: Optional[OrderInfo] = None
        self.last_mid:  float = 0.0

        self._load_exchange_info()

    # ── Exchange info ────────────────────────────────────────────────────

    @staticmethod
    def _decimal_places(step: float) -> int:
        if step >= 1.0:
            return 0
        s = f"{step:.10f}".rstrip("0")
        return len(s.split(".")[-1]) if "." in s else 0

    def _load_exchange_info(self) -> None:
        try:
            info = self.client.get_symbol_info(self.symbol)
            if not info:
                return
            for f in info.get("filters", []):
                ft = f.get("filterType", "")
                if ft == "LOT_SIZE":
                    self.step_size = float(f["stepSize"])
                    self.qty_prec  = self._decimal_places(self.step_size)
                elif ft == "PRICE_FILTER":
                    self.tick_size  = float(f["tickSize"])
                    self.price_prec = self._decimal_places(self.tick_size)
                elif ft in ("MIN_NOTIONAL", "NOTIONAL"):
                    # Binance usa "minNotional" (legado) ou "minNotionalValue" (novo)
                    raw = f.get("minNotional") or f.get("minNotionalValue") or 10.0
                    self.min_notional = float(raw)
        except Exception as exc:
            print(f"{Y}AVISO exchange info: {exc}{RST}")

    def _round_qty(self, qty: float) -> float:
        if self.step_size <= 0:
            return qty
        return math.floor(qty / self.step_size) * self.step_size

    def _round_price(self, price: float) -> float:
        if self.tick_size <= 0:
            return price
        return math.floor(price / self.tick_size) * self.tick_size

    # ── Market data ─────────────────────────────────────────────────────

    def get_mid(self) -> float:
        try:
            t = self.client.get_orderbook_ticker(symbol=self.symbol)
            mid = (float(t["bidPrice"]) + float(t["askPrice"])) / 2.0
            self.last_mid = mid
            return mid
        except Exception:
            return self.last_mid

    def get_market_data(self) -> Tuple[float, float, float, float, str]:
        """
        Retorna (mid, atr_pct, adx, liquidity_usdt, warn).
        warn é uma string vazia ou mensagem de aviso (para exibição no dashboard).
        """
        atr_pct = adx = 0.0
        liq = 0.0
        mid = self.last_mid
        warn = ""

        try:
            klines = self.client.get_klines(
                symbol=self.symbol,
                interval=Client.KLINE_INTERVAL_1MINUTE,
                limit=60,
            )
            atr_pct, adx = calc_atr_adx(klines)
        except Exception as exc:
            warn = f"klines stale: {exc}"

        try:
            book = self.client.get_order_book(symbol=self.symbol, limit=10)
            liq  = calc_liquidity(book)
            mid  = self.get_mid()
        except Exception as exc:
            mid  = self.get_mid()
            if not warn:
                warn = f"book stale: {exc}"

        return mid, atr_pct, adx, liq, warn

    # ── Order management ─────────────────────────────────────────────────

    def place_order(self, side: str, price: float,
                    size_usdt: float) -> Optional[OrderInfo]:
        """Coloca ordem limit. Verifica banca virtual antes de enviar."""
        if price <= 0:
            return None

        qty_raw   = size_usdt / price
        qty       = self._round_qty(qty_raw)
        price_r   = self._round_price(price)

        if qty <= 0 or price_r <= 0:
            return None

        notional = qty * price_r
        if notional < self.min_notional:
            return None

        # Verificação de banca virtual
        if side == "BUY":
            if self.vbank.available_quote < notional:
                return None
        else:
            if self.vbank.available_base < qty:
                return None

        qty_str   = f"{qty:.{self.qty_prec}f}"
        price_str = f"{price_r:.{self.price_prec}f}"

        try:
            if side == "BUY":
                resp = self.client.order_limit_buy(
                    symbol=self.symbol, quantity=qty_str,
                    price=price_str, timeInForce="GTC",
                )
            else:
                resp = self.client.order_limit_sell(
                    symbol=self.symbol, quantity=qty_str,
                    price=price_str, timeInForce="GTC",
                )
        except BinanceAPIException as exc:
            print(f"{Y}[place_order] API err {exc.code}: {exc.message}{RST}")
            return None
        except BinanceOrderException as exc:
            print(f"{Y}[place_order] Order err {exc.code}: {exc.message}{RST}")
            return None
        except Exception as exc:
            print(f"{Y}[place_order] Erro inesperado: {exc}{RST}")
            return None

        order = OrderInfo(
            order_id  = str(resp["orderId"]),
            side      = side,
            price     = price_r,
            qty       = float(resp.get("origQty", qty)),
            placed_at = time.time(),
            symbol    = self.symbol,
        )

        if side == "BUY":
            self.vbank.reserve_bid(order.qty, price_r)
        else:
            self.vbank.reserve_ask(order.qty)

        return order

    def check_and_process(self, order: OrderInfo) -> Tuple[str, float, float, float]:
        """
        Consulta status da ordem na exchange, processa fills novos.
        Retorna (status, filled_qty_delta, trade_pnl, avg_fill_price).

        avg_fill_price é o preço médio INCREMENTAL do delta atual,
        calculado via (ΔquoteQty / ΔbaseQty) para refletir o custo
        real do novo fill — não a média cumulativa total.
        """
        try:
            resp = self.client.get_order(
                symbol=self.symbol, orderId=int(order.order_id)
            )
            order._unknown_hits = 0   # consulta bem-sucedida — zera contador
        except BinanceAPIException as exc:
            if exc.code == -2013:   # Order does not exist
                return "NOT_FOUND", 0.0, 0.0, order.price
            # Falha de API (rate-limit, timeout parcial etc.)
            order._unknown_hits += 1
            if order._unknown_hits >= 5:
                # 5 falhas consecutivas → trata como NOT_FOUND e libera reservas
                return "NOT_FOUND", 0.0, 0.0, order.price
            return "UNKNOWN", 0.0, 0.0, order.price
        except Exception:
            order._unknown_hits += 1
            if order._unknown_hits >= 5:
                return "NOT_FOUND", 0.0, 0.0, order.price
            return "UNKNOWN", 0.0, 0.0, order.price

        status   = resp.get("status", "UNKNOWN")
        exec_qty = float(resp.get("executedQty", 0.0))
        cum_q    = float(resp.get("cummulativeQuoteQty", 0.0))

        delta_base  = exec_qty - order.filled_qty
        delta_quote = cum_q    - order.filled_quote

        # Preço médio incremental: USDT_novo / base_novo
        # Fallback para order.price quando delta é zero ou negligível
        avg_p = (delta_quote / delta_base) if delta_base > 1e-12 else order.price

        pnl = 0.0
        if delta_base > 1e-12:
            pnl = self.vbank.on_fill(order.side, delta_base, avg_p)
            order.filled_qty   = exec_qty
            order.filled_quote = cum_q

            # BUG 4: on_fill libera delta_base * avg_p das reservas, mas a reserva
            # foi feita com order.price. Se avg_p < order.price (price improvement),
            # o excedente fica preso em quote_reserved indefinidamente.
            # Liberamos aqui o saldo residual da reserva para este delta.
            if order.side == "BUY":
                surplus = delta_base * (order.price - avg_p)
                if surplus > 1e-8:
                    self.vbank.quote_reserved = max(
                        0.0, self.vbank.quote_reserved - surplus
                    )

        order.status = status
        return status, delta_base, pnl, avg_p

    def cancel_order(self, order: OrderInfo) -> bool:
        """Cancela ordem e libera reservas na banca virtual."""
        # Quantidade realmente ainda reservada (o fill já deduziu a parte executada)
        unfilled = max(0.0, order.qty - order.filled_qty)
        try:
            self.client.cancel_order(
                symbol=self.symbol, orderId=int(order.order_id)
            )
            self.vbank.release(order.side, unfilled, order.price)
            return True
        except BinanceAPIException as exc:
            if exc.code in (-2011, -2013):  # already filled/cancelled
                self.vbank.release(order.side, unfilled, order.price)
                return True
            # Mesmo com falha de API, libera reservas para evitar acúmulo
            self.vbank.release(order.side, unfilled, order.price)
            return False
        except Exception:
            self.vbank.release(order.side, unfilled, order.price)
            return False

    def cancel_all(self) -> int:
        """Cancela todas as ordens abertas do símbolo e limpa reservas."""
        count = 0
        try:
            for o in self.client.get_open_orders(symbol=self.symbol):
                try:
                    self.client.cancel_order(
                        symbol=self.symbol, orderId=o["orderId"]
                    )
                    count += 1
                except Exception:
                    pass
        except Exception:
            pass
        self.vbank.quote_reserved = 0.0
        self.vbank.base_reserved  = 0.0
        return count


# ═══════════════════════════════════════════════════════════════════════════
# TRADING ENGINE — Loop principal
# ═══════════════════════════════════════════════════════════════════════════

class TradingEngine:
    """
    Coordena o ciclo completo:
      1. Dados de mercado
      2. Verificação de ordens abertas e processamento de fills
      3. Chamada a propose() da estratégia (100% inalterada)
      4. Colocação de novas ordens filtradas pela banca virtual
      5. Display do dashboard e logging
    """

    _LOG_LINES = 8   # linhas de log visíveis no dashboard

    def __init__(
        self,
        binance:    BinanceEngine,
        vbank:      VirtualBank,
        logger:     AuditLogger,
        symbol:     str,
        sym_idx:    int,
        mode:       int,
        live:       bool,
        strat:      types.ModuleType,
    ) -> None:
        self.binance    = binance
        self.vbank      = vbank
        self.logger     = logger
        self.symbol     = symbol
        self.sym_idx    = sym_idx
        self.mode       = mode
        self.live       = live
        self._strat     = strat
        self.mode_label = str(getattr(strat, "STRATEGY_DISPLAY_NAME", "MICRO-BANCA")).upper()

        # Constantes lidas do plugin (podem variar por plugin carregado)
        _defaults       = getattr(strat, "TESTNET_SOAK_DEFAULTS", {})
        self._INTERVAL  = float(_defaults.get("interval_sec", 8.0))
        self._RESTING   = float(_defaults.get("resting_order_min_age_sec", 16.0))

        if self._RESTING < self._INTERVAL:
            print(
                f"{Y}AVISO: resting_order_min_age_sec ({self._RESTING:.0f}s) "
                f"< interval_sec ({self._INTERVAL:.0f}s).\n"
                f"  Todas as ordens serão canceladas antes da próxima verificação.\n"
                f"  Ajuste o plugin para resting >= interval.{RST}"
            )

        self._MAX_CYCLES  : int       = int(_defaults.get("max_cycles", 0))  # 0 = ilimitado
        self._shutdown_called: bool  = False   # idempotência de shutdown()

        self.cycle     : int      = 0
        self.running   : bool     = True
        self.started_at: datetime = datetime.utcnow()

        self._last_proposal : dict          = {}
        self._last_mid      : float         = 0.0
        self._last_atr      : float         = 0.0
        self._last_adx      : float         = 0.0
        self._last_liq      : float         = 0.0
        self._log_lines     : List[str]     = []
        self._parking_cycles: int           = 0

    # ── Helpers internos ────────────────────────────────────────────────

    def _log(self, msg: str) -> None:
        ts = datetime.utcnow().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        self._log_lines.append(line)
        if len(self._log_lines) > self._LOG_LINES:
            self._log_lines.pop(0)

    def _build_ctx(self, mid: float, atr_pct: float,
                   adx: float, liq: float) -> dict:
        return {
            "symbol":  self.symbol,
            "metrics": {
                "atr_pct":        atr_pct,
                "adx":            adx,
                "liquidity_quote": liq,
            },
            "execution": {
                "mid":        mid,
                "mid_price":  mid,
                "last_price": mid,
                # Estado das ordens ativas — permite o plugin decidir assimetria
                "bid_active": self.binance.bid_order is not None,
                "ask_active": self.binance.ask_order is not None,
                **self.vbank.to_ctx(),
                "plugin_ui": {
                    "target_symbol_idx": float(self.sym_idx),
                    "preset_mode":       float(self.mode),
                },
            },
        }

    # ── Gerenciamento de ordens ─────────────────────────────────────────

    def _handle_order(self, order: Optional[OrderInfo],
                      side: str) -> Optional[OrderInfo]:
        """Processa uma ordem ativa: fills, cancelamento por tempo."""
        if order is None:
            return None

        status, delta, pnl, avg_p = self.binance.check_and_process(order)

        if delta > 1e-12:
            sign = "+" if pnl >= 0 else ""
            self._log(
                f"✓ FILL {side} {delta:.6f} {self.symbol[:3]} "
                f"@ ${avg_p:,.4f}  PnL:{sign}{pnl:.4f} USDT"
            )
            self.logger.log("FILL", {
                "symbol":          self.symbol,
                "side":            side,
                "order_id":        order.order_id,
                "price":           order.price,       # preço limite da ordem
                "qty_base":        delta,
                "qty_quote":       delta * avg_p,     # custo/receita real
                "fill_price":      avg_p,             # preço real de execução
                "fill_qty_base":   delta,
                "trade_pnl":       pnl,
                "vb_equity":       self.vbank.equity,
                "vb_quote_bal":    self.vbank.quote_balance,
                "vb_base_bal":     self.vbank.base_balance,
                "vb_realized_pnl": self.vbank.realized_pnl,
                "vb_total_pnl":    self.vbank.total_pnl,
                "mid_price":       self._last_mid,
                "cycle":           self.cycle,
                "mode_label":      self.mode_label,
            })

        if status in ("FILLED", "CANCELED", "EXPIRED", "NOT_FOUND"):
            # Zera reservas residuais do lado desta ordem.
            # Para BUY: a reserva original era qty*order.price; on_fill liberou
            # qty_filled*avg_p (pode diferir por price improvement). Forçamos
            # quote_reserved a zero para esta ordem zerando a parcela restante.
            unfilled_base = max(0.0, order.qty - order.filled_qty)
            if order.side == "BUY":
                # Quantidade não preenchida em USDT (ao preço limite original)
                unfilled_quote = unfilled_base * order.price
                if unfilled_quote > 1e-8:
                    self.vbank.quote_reserved = max(
                        0.0, self.vbank.quote_reserved - unfilled_quote
                    )
            else:
                if unfilled_base > 1e-12:
                    self.vbank.base_reserved = max(
                        0.0, self.vbank.base_reserved - unfilled_base
                    )
            return None

        # Cancela ordem se passou do tempo de resting
        age = time.time() - order.placed_at
        if age >= self._RESTING:
            # cancel_order já libera as reservas (inclusive em caso de falha de API)
            cancelled = self.binance.cancel_order(order)
            if order.filled_qty > 1e-12:
                self._log(
                    f"↩ CANCEL PARCIAL {side}  "
                    f"filled={order.filled_qty:.6f}/{order.qty:.6f}"
                )
                self.logger.log("PARTIAL_FILL_CANCEL", {
                    "symbol":        self.symbol,
                    "side":          side,
                    "order_id":      order.order_id,
                    "fill_qty_base": order.filled_qty,
                    "trade_pnl":     pnl,
                    "vb_equity":     self.vbank.equity,
                    "vb_total_pnl":  self.vbank.total_pnl,
                    "cycle":         self.cycle,
                })
            else:
                self.logger.log("ORDER_CANCEL", {
                    "symbol":   self.symbol,
                    "side":     side,
                    "order_id": order.order_id,
                    "price":    order.price,
                    "qty_base": order.qty,
                    "notes":    "" if cancelled else "api_cancel_failed_reserves_freed",
                    "cycle":    self.cycle,
                })
            return None

        return order

    def _place_orders(self, proposal: dict, mid: float) -> None:
        """
        Coloca novas ordens BID e ASK com base na proposta.

        Suporte a operações assimétricas de MK — o plugin pode retornar:
          bid_spread_bps / ask_spread_bps   (spread independente por lado)
          bid_size_quote  / ask_size_quote  (lote independente por lado)
          only_bid  (True → não posta ASK neste ciclo)
          only_ask  (True → não posta BID neste ciclo)
        Se ausentes, usa os valores simétricos spread_bps / order_size_quote.
        """
        spread_bps = float(proposal.get("spread_bps", 0.0))
        size_usdt  = float(proposal.get("order_size_quote", 10.5))

        # Assimétrico: spread e lote independentes por lado
        bid_spread = float(proposal.get("bid_spread_bps", spread_bps))
        ask_spread = float(proposal.get("ask_spread_bps", spread_bps))
        bid_size   = float(proposal.get("bid_size_quote",  size_usdt))
        ask_size   = float(proposal.get("ask_size_quote",  size_usdt))

        # Flags de inibição lateral
        only_bid = bool(proposal.get("only_bid", False))
        only_ask = bool(proposal.get("only_ask", False))

        bid_price = mid * (1.0 - bid_spread / 20_000.0)
        ask_price = mid * (1.0 + ask_spread / 20_000.0)

        # BID (compra — usa quote)
        if self.binance.bid_order is None and not only_ask:
            order = self.binance.place_order("BUY", bid_price, bid_size)
            if order:
                self.binance.bid_order = order
                asym_tag = f"  bid_spread={bid_spread:.1f}bps" if bid_spread != spread_bps else ""
                self._log(
                    f"→ BID  ${bid_size:.2f} @ ${bid_price:,.4f}  "
                    f"spread={bid_spread:.1f}bps{asym_tag}"
                )
                self.logger.log("ORDER_PLACED", {
                    "symbol":       self.symbol,
                    "side":         "BUY",
                    "order_id":     order.order_id,
                    "price":        bid_price,
                    "qty_base":     order.qty,
                    "qty_quote":    bid_size,
                    "vb_equity":    self.vbank.equity,
                    "vb_total_pnl": self.vbank.total_pnl,
                    "spread_bps":   bid_spread,
                    "mid_price":    mid,
                    "cycle":        self.cycle,
                    "mode_label":   self.mode_label,
                })

        # ASK (venda — usa base)
        if self.binance.ask_order is None and not only_bid:
            # Verifica antecipadamente se há base para dar log informativo
            ask_qty_raw = ask_size / ask_price if ask_price > 0 else 0.0
            ask_qty     = math.floor(ask_qty_raw / self.binance.step_size) * self.binance.step_size \
                          if self.binance.step_size > 0 else ask_qty_raw
            if ask_qty > 0 and self.vbank.available_base < ask_qty:
                # Log apenas a cada 5 ciclos para não poluir o dashboard
                if self.cycle % 5 == 1:
                    self._log(
                        f"⚠ ASK bloqueado: base={self.vbank.available_base:.6f} "
                        f"< necessário={ask_qty:.6f} {self.symbol[:3]}"
                    )
            else:
                order = self.binance.place_order("SELL", ask_price, ask_size)
                if order:
                    self.binance.ask_order = order
                    asym_tag = f"  ask_spread={ask_spread:.1f}bps" if ask_spread != spread_bps else ""
                    self._log(
                        f"← ASK  ${ask_size:.2f} @ ${ask_price:,.4f}  "
                        f"spread={ask_spread:.1f}bps{asym_tag}"
                    )
                    self.logger.log("ORDER_PLACED", {
                        "symbol":       self.symbol,
                        "side":         "SELL",
                        "order_id":     order.order_id,
                        "price":        ask_price,
                        "qty_base":     order.qty,
                        "qty_quote":    ask_size,
                        "vb_equity":    self.vbank.equity,
                        "vb_total_pnl": self.vbank.total_pnl,
                        "spread_bps":   ask_spread,
                        "mid_price":    mid,
                        "cycle":        self.cycle,
                        "mode_label":   self.mode_label,
                    })

    # ── Dashboard ───────────────────────────────────────────────────────

    def _display(self) -> None:
        proposal  = self._last_proposal
        mid       = self._last_mid
        atr_pct   = self._last_atr
        adx       = self._last_adx
        liq       = self._last_liq
        meta      = proposal.get("meta", {})
        skip      = bool(meta.get("skip", False))
        skip_why  = str(meta.get("skip_reason", ""))
        skew      = str(meta.get("skew_status", meta.get("skew", "—")))
        plocked   = bool(meta.get("profit_lock", False))
        s_bps     = float(proposal.get("spread_bps", 0.0))
        s_usdt    = float(proposal.get("order_size_quote", 0.0))
        # Assimétrico: mostra bid/ask separado quando o plugin define valores diferentes
        bid_bps   = float(proposal.get("bid_spread_bps", s_bps))
        ask_bps   = float(proposal.get("ask_spread_bps", s_bps))
        bid_usdt  = float(proposal.get("bid_size_quote",  s_usdt))
        ask_usdt  = float(proposal.get("ask_size_quote",  s_usdt))
        is_asym   = (bid_bps != ask_bps or bid_usdt != ask_usdt)

        pnl_total = self.vbank.total_pnl
        elapsed   = (datetime.utcnow() - self.started_at).total_seconds()
        h, rem    = divmod(int(elapsed), 3600)
        mn, s_    = divmod(rem, 60)
        net_label = (R + "⚠ LIVE — DINHEIRO REAL ⚠" + RST) if self.live \
                    else (Y + "TESTNET — Simulado" + RST)
        now_str   = datetime.utcnow().strftime("%Y-%m-%d  %H:%M:%S UTC")

        _clr()
        print(_box_top())
        print(_row(
            BG + "MICRO-BANCA v9 — BINANCE MARKET MAKING ENGINE" + RST
            + "   " + net_label
        ))
        print(_row(
            DIM + f"Sessão: {now_str}  |  Ciclo: {self.cycle}  |  "
            f"Tempo: {h:02d}:{mn:02d}:{s_:02d}" + RST
        ))
        print(_box_sep())

        # Banca virtual
        print(_row(BG + "BANCA VIRTUAL" + RST))
        print(_row(
            f"{G}Inicial:{RST} ${self.vbank.initial_quote:.2f}  "
            f"{G}Equity:{RST} ${self.vbank.equity:.4f}  "
            f"{G}PnL Total:{RST} {_pnl_str(pnl_total)}"
        ))
        print(_row(
            f"{G}Quote livre:{RST} ${self.vbank.quote_balance:.4f}  "
            f"{G}Base:{RST} {self.vbank.base_balance:.8f} {self.symbol[:3]}  "
            f"{G}PnL Real.:{RST} ${self.vbank.realized_pnl:.4f}"
        ))
        print(_row(
            f"{G}Reserva Quote:{RST} ${self.vbank.quote_reserved:.4f}  "
            f"{G}Reserva Base:{RST} {self.vbank.base_reserved:.8f}  "
            f"{G}Custo Médio:{RST} ${self.vbank.avg_cost_base:.4f}"
        ))
        print(_box_sep())

        # Mercado
        print(_row(BG + "MERCADO" + RST))
        print(_row(
            f"{G}Mid:{RST} ${mid:,.4f}  "
            f"{G}ATR:{RST} {atr_pct * 100:.3f}%  "
            f"{G}ADX:{RST} {adx:.1f}  "
            f"{G}Liq:{RST} ${liq:,.0f} USDT"
        ))
        if is_asym:
            print(_row(
                f"{G}BID:{RST} {bid_bps:.1f}bps ${bid_usdt:.2f}  "
                f"{G}ASK:{RST} {ask_bps:.1f}bps ${ask_usdt:.2f}  "
                f"{Y}[ASSIMÉTRICO]{RST}  "
                f"{G}Skew:{RST} {skew}  "
                f"{G}Ativo:{RST} {self.symbol}"
            ))
        else:
            print(_row(
                f"{G}Spread Bot:{RST} {s_bps:.1f} bps  "
                f"{G}Lote:{RST} ${s_usdt:.2f}  "
                f"{G}Skew:{RST} {skew}  "
                f"{G}Modo:{RST} {self.mode_label}  "
                f"{G}Ativo:{RST} {self.symbol}"
            ))
        if skip:
            clr_park = R if "STOP_LOSS" in skip_why else Y
            print(_row(clr_park + f"⚠ PARKING [{self._parking_cycles} ciclos]: {skip_why}" + RST))
        if plocked:
            print(_row(BG + "★ PROFIT LOCK ATIVO — exposição reduzida automaticamente" + RST))
        print(_box_sep())

        # Ordens ativas
        print(_row(BG + "ORDENS ATIVAS" + RST))
        bid = self.binance.bid_order
        ask = self.binance.ask_order
        if bid:
            age_b = time.time() - bid.placed_at
            print(_row(
                f"{G}BID:{RST} ${bid.price:,.4f}  "
                f"Qty: {bid.qty:.6f}  "
                f"Fill: {bid.filled_qty:.6f}/{bid.qty:.6f}  "
                f"Idade: {age_b:.0f}s  "
                f"ID: {bid.order_id}"
            ))
        else:
            print(_row(DIM + "BID: —" + RST))
        if ask:
            age_a = time.time() - ask.placed_at
            print(_row(
                f"{G}ASK:{RST} ${ask.price:,.4f}  "
                f"Qty: {ask.qty:.6f}  "
                f"Fill: {ask.filled_qty:.6f}/{ask.qty:.6f}  "
                f"Idade: {age_a:.0f}s  "
                f"ID: {ask.order_id}"
            ))
        else:
            print(_row(DIM + "ASK: —" + RST))
        print(_box_sep())

        # Estatísticas da sessão
        print(_row(BG + "ESTATÍSTICAS DA SESSÃO" + RST))
        print(_row(
            f"{G}Ciclos:{RST} {self.cycle}  "
            f"{G}Fills totais:{RST} {self.vbank.total_fills}  "
            f"{G}Compras:{RST} {self.vbank.buy_fills}  "
            f"{G}Vendas:{RST} {self.vbank.sell_fills}"
        ))
        print(_row(
            f"{G}PnL Realizado:{RST} ${self.vbank.realized_pnl:.4f} USDT  "
            f"{G}PnL não real.:{RST} ${self.vbank.base_balance * mid - self.vbank.base_balance * self.vbank.avg_cost_base:.4f} USDT"
        ))
        print(_box_sep())

        # Log recente
        print(_row(BG + "LOG RECENTE" + RST))
        lines = self._log_lines[-self._LOG_LINES:]
        if lines:
            for ln in lines:
                print(_row(DIM + ln + RST))
        else:
            print(_row(DIM + "Aguardando eventos..." + RST))
        print(_box_bot())
        cycles_info = (
            f"{self.cycle}/{self._MAX_CYCLES}"
            if self._MAX_CYCLES > 0 else str(self.cycle)
        )
        print(
            G + f"  Ciclo: {cycles_info}  |  Intervalo: {self._INTERVAL:.0f}s  |  "
            f"Resting: {self._RESTING:.0f}s  |  Ctrl+C → menu" + RST,
            flush=True,
        )

    # ── Loop principal ──────────────────────────────────────────────────

    def run(self) -> None:
        lim = f"  |  Limite: {self._MAX_CYCLES} ciclos" if self._MAX_CYCLES > 0 else ""
        self._log(f"Motor iniciado — {self.symbol}  Modo: {self.mode_label}{lim}")

        while self.running:
            self.cycle += 1
            t_start = time.time()

            # ── Checagem de ciclos máximos ───────────────────────────────
            if self._MAX_CYCLES > 0 and self.cycle > self._MAX_CYCLES:
                # Desfaz o incremento antecipado: o ciclo N+1 nunca chegou a executar
                self.cycle = self._MAX_CYCLES
                self._log(
                    f"★ Sessão concluída — {self._MAX_CYCLES} ciclos executados."
                )
                self._display()        # exibe estado final antes de encerrar
                self.shutdown()        # cancela ordens, salva auditoria, exibe resumo
                break

            try:
                # 1. Dados de mercado
                mid, atr_pct, adx, liq, mkt_warn = self.binance.get_market_data()
                if mid <= 0:
                    # Sem cotação válida — ciclo ignorado (não contabilizado)
                    self.cycle -= 1
                    self._log("⚠ Mid=0: sem cotação válida, ciclo ignorado.")
                    time.sleep(self._INTERVAL)
                    continue
                self._last_mid = mid
                self._last_atr = atr_pct
                self._last_adx = adx
                self._last_liq = liq
                self.vbank.update_mid(mid)
                if mkt_warn:
                    self._log(f"⚠ {mkt_warn}")

                # 2. Verifica ordens existentes
                self.binance.bid_order = self._handle_order(
                    self.binance.bid_order, "BUY"
                )
                self.binance.ask_order = self._handle_order(
                    self.binance.ask_order, "SELL"
                )

                # 3. Contexto para propose()
                ctx = self._build_ctx(mid, atr_pct, adx, liq)

                # 4. Proposta da estratégia — LÓGICA 100% INALTERADA
                proposal = self._strat.propose(ctx)
                self._last_proposal = proposal

                meta = proposal.get("meta", {})
                skip = bool(meta.get("skip", False))

                if skip:
                    self._parking_cycles += 1
                    # Em parking: cancela ordens ativas
                    if self.binance.bid_order:
                        self.binance.cancel_order(self.binance.bid_order)
                        self.binance.bid_order = None
                    if self.binance.ask_order:
                        self.binance.cancel_order(self.binance.ask_order)
                        self.binance.ask_order = None
                    if self._parking_cycles % 10 == 1:
                        reason = meta.get("skip_reason", "")
                        self._log(f"⏸ PARKING {self._parking_cycles} ciclos: {reason}")
                else:
                    self._parking_cycles = 0
                    # 5. Coloca novas ordens
                    self._place_orders(proposal, mid)

                # 6. Log de ciclo (CSV/JSON)
                self.logger.log("CYCLE", {
                    "symbol":          self.symbol,
                    "side":            "",
                    "cycle":           self.cycle,
                    "mid_price":       mid,
                    "atr_pct":         atr_pct,
                    "adx":             adx,
                    "spread_bps":      proposal.get("spread_bps", 0),
                    "skew":            meta.get("skew", ""),
                    "vb_equity":       self.vbank.equity,
                    "vb_quote_bal":    self.vbank.quote_balance,
                    "vb_base_bal":     self.vbank.base_balance,
                    "vb_realized_pnl": self.vbank.realized_pnl,
                    "vb_total_pnl":    self.vbank.total_pnl,
                    "mode_label":      self.mode_label,
                    "notes":           meta.get("skip_reason", ""),
                })

                # Snapshot JSON a cada 10 ciclos
                if self.cycle % 10 == 0:
                    self.logger.save_json()

            except Exception as exc:
                self._log(f"ERRO ciclo {self.cycle}: {exc}")

            # 7. Renderiza dashboard
            try:
                self._display()
            except Exception:
                pass

            # 8. Aguarda próximo ciclo
            sleep_sec = max(0.1, self._INTERVAL - (time.time() - t_start))
            time.sleep(sleep_sec)

    # ── Encerramento ─────────────────────────────────────────────────────

    def shutdown(self) -> None:
        if self._shutdown_called:
            return
        self._shutdown_called = True
        self.running = False
        print(f"\n{Y}Encerrando... cancelando todas as ordens abertas...{RST}")
        cancelled = self.binance.cancel_all()
        # Zera referências locais — evita que código residual tente reprocessar
        # ordens que cancel_all já removeu da exchange
        self.binance.bid_order = None
        self.binance.ask_order = None
        print(f"{G}Ordens canceladas: {cancelled}{RST}")

        self.logger.save_json()
        print(f"\n{G}Arquivos de auditoria salvos:{RST}")
        print(f"  CSV:  {self.logger.csv_path}")
        print(f"  JSON: {self.logger.json_path}")

        pnl = self.vbank.total_pnl
        sign = "+" if pnl >= 0 else ""
        pcolor = BG if pnl >= 0 else R

        print(f"\n{BG}{'═' * 56}")
        print("  RESUMO FINAL DA SESSÃO")
        print(f"{'═' * 56}{RST}")
        print(f"  {G}Ciclos executados :{RST} {self.cycle}")
        print(f"  {G}Tempo de sessão   :{RST} {(datetime.utcnow() - self.started_at)}")
        print(f"  {G}Fills totais      :{RST} {self.vbank.total_fills}"
              f"  (compras: {self.vbank.buy_fills}  /  vendas: {self.vbank.sell_fills})")
        print(f"  {G}PnL realizado     :{RST} ${self.vbank.realized_pnl:.4f} USDT")
        print(f"  {G}Equity final      :{RST} ${self.vbank.equity:.4f} USDT")
        print(f"  {G}PnL total (MTM)   :{RST} {pcolor}{sign}${pnl:.4f} USDT{RST}")
        print(f"  {G}Banca inicial     :{RST} ${self.vbank.initial_quote:.2f} USDT")
        retorno = (pnl / self.vbank.initial_quote * 100) if self.vbank.initial_quote > 0 else 0.0
        print(f"  {G}Retorno %         :{RST} {pcolor}{sign}{retorno:.3f}%{RST}")
        print(f"{BG}{'═' * 56}{RST}")


# ═══════════════════════════════════════════════════════════════════════════
# STARTUP WIZARD
# ═══════════════════════════════════════════════════════════════════════════

# ── Gestão de arquivos de auditoria ─────────────────────────────────────

_AUDIT_DIR = _APP_DIR   # mesmo diretório do script/.exe


def _list_audit_files(audit_dir: str) -> List[str]:
    """Lista audit_*.csv e audit_*.json no diretório, ordenados por nome."""
    result: List[str] = []
    try:
        for name in sorted(os.listdir(audit_dir)):
            if name.startswith("audit_") and name.endswith((".csv", ".json")):
                result.append(os.path.join(audit_dir, name))
    except OSError:
        pass
    return result


def _clear_session_files(audit_dir: str) -> int:
    """
    Remove todos os arquivos audit_* da sessão anterior.
    Retorna o número de arquivos excluídos.
    """
    deleted = 0
    for f in _list_audit_files(audit_dir):
        try:
            os.remove(f)
            deleted += 1
        except OSError:
            pass
    return deleted


def _create_audit_zip(audit_dir: str) -> Optional[str]:
    """Empacota todos os arquivos audit_* num ZIP com timestamp. Retorna o caminho."""
    files = _list_audit_files(audit_dir)
    if not files:
        print(Y + "Nenhum arquivo de auditoria para empacotar." + RST)
        return None
    ts       = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    zip_path = os.path.join(audit_dir, f"audit_bundle_{ts}.zip")
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in files:
                zf.write(f, os.path.basename(f))
        total_kb = sum(os.path.getsize(f) for f in files) / 1024
        print(G + f"ZIP criado: {zip_path}  ({total_kb:.1f} KB total)" + RST)
        return zip_path
    except Exception as exc:
        print(R + f"Erro ao criar ZIP: {exc}" + RST)
        return None


class _AuditHTTPHandler(http.server.BaseHTTPRequestHandler):
    """
    Handler HTTP mínimo que serve apenas arquivos audit_* como download.
    Expõe uma página índice HTML e permite baixar cada arquivo individualmente.
    """
    _audit_dir: str = "."

    def log_message(self, fmt: str, *args: Any) -> None:
        pass   # silencia log do httpd no terminal

    def do_GET(self) -> None:
        name = self.path.lstrip("/")

        if not name:
            # Página índice
            files = _list_audit_files(self._audit_dir)
            items = "".join(
                f'<li><a href="/{os.path.basename(f)}">{os.path.basename(f)}</a>'
                f" &nbsp;<span style='color:#555'>({os.path.getsize(f)/1024:.1f} KB)</span></li>"
                for f in files
            )
            body = (
                "<!DOCTYPE html><html><head><meta charset='utf-8'>"
                "<style>body{font-family:monospace;background:#111;color:#0f0;padding:24px}"
                "h2{color:#1f1}a{color:#0f0}li{margin:8px 0}"
                "footer{color:#444;font-size:.85em;margin-top:24px}</style></head><body>"
                "<h2>&#x1F4C1; MK-Engine &mdash; Arquivos de Auditoria</h2>"
                f"<ul>{items}</ul>"
                "<footer>Pressione Enter no terminal do servidor para encerrar.</footer>"
                "</body></html>"
            ).encode("utf-8")
            self._reply(200, "text/html; charset=utf-8", body)
            return

        # Bloqueia qualquer acesso que não seja audit_*
        if not name.startswith("audit_") or "/" in name or "\\" in name:
            self._reply(403, "text/plain", b"Forbidden")
            return

        full = os.path.join(self._audit_dir, name)
        if not os.path.isfile(full):
            self._reply(404, "text/plain", b"Not found")
            return

        ct = "text/csv" if name.endswith(".csv") else "application/json"
        with open(full, "rb") as fh:
            data = fh.read()
        self._reply(200, ct, data,
                    [("Content-Disposition", f'attachment; filename="{name}"')])

    def _reply(self, code: int, ct: str, body: bytes,
               extra: Optional[List[Tuple[str, str]]] = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or []):
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


def _serve_audit_http(audit_dir: str, port: int = 8765) -> None:
    """
    Inicia um servidor HTTP leve para download de arquivos de auditoria.
    Bloqueia até o usuário pressionar Enter (ou ocorrer EOF em modo não-interativo).
    Tenta portas sequenciais se a requisitada estiver ocupada.
    """
    if not _list_audit_files(audit_dir):
        print(Y + "Nenhum arquivo de auditoria para servir." + RST)
        return

    # IP local para exibição (sem conectar de fato)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
    except Exception:
        local_ip = "0.0.0.0"

    class _Handler(_AuditHTTPHandler):
        _audit_dir = audit_dir

    httpd: Optional[socketserver.TCPServer] = None
    for p in range(port, port + 20):
        try:
            socketserver.TCPServer.allow_reuse_address = True
            httpd = socketserver.TCPServer(("", p), _Handler)
            port  = p
            break
        except OSError:
            continue

    if httpd is None:
        print(R + f"Não foi possível abrir nenhuma porta no range {port}–{port+19}." + RST)
        return

    print()
    print(BG + f"  Servidor HTTP — porta {port}  " + RST)
    print(G  + f"  http://{local_ip}:{port}/")
    print(DIM + "  Se acessar remotamente, use o IP público do VPS.")
    print(Y  + "  Pressione Enter para encerrar o servidor." + RST)
    print()

    srv = threading.Thread(target=httpd.serve_forever, daemon=True)
    srv.start()
    try:
        input()
    except (EOFError, KeyboardInterrupt):
        pass
    httpd.shutdown()
    print(G + "Servidor HTTP encerrado." + RST + "\n")


def _audit_download_menu(audit_dir: str) -> None:
    """
    Exibe menu de download/exportação de arquivos de auditoria.
    Chamado ANTES de limpar arquivos da sessão anterior.
    Silencioso se não houver arquivos.
    """
    files = _list_audit_files(audit_dir)
    if not files:
        return

    total_kb = sum(os.path.getsize(f) / 1024 for f in files)
    print()
    print(BG + "ARQUIVOS DA SESSÃO ANTERIOR" + RST)
    for f in files:
        kb = os.path.getsize(f) / 1024
        print(f"  {G}{os.path.basename(f)}{RST}  ({kb:.1f} KB)")
    print(f"  {DIM}Total: {total_kb:.1f} KB{RST}")
    print()
    print(f"  {G}1{RST}  Baixar via HTTP  (recomendado para VPS)")
    print(f"  {G}2{RST}  Criar pacote ZIP")
    print(f"  {G}3{RST}  Mostrar caminho para SCP")
    print(f"  {G}Enter{RST}  Pular  (arquivos serão excluídos ao iniciar nova sessão)")
    print()

    raw = input(G + "Opção: " + RST).strip()
    print()

    if raw == "1":
        raw_port = input(G + "Porta HTTP [padrão 8765]: " + RST).strip()
        try:
            port = int(raw_port) if raw_port else 8765
        except ValueError:
            port = 8765
        _serve_audit_http(audit_dir, port)

    elif raw == "2":
        _create_audit_zip(audit_dir)
        input(G + "Pressione Enter para continuar..." + RST)
        print()

    elif raw == "3":
        abs_dir = os.path.abspath(audit_dir)
        print(G + f"Diretório: {abs_dir}" + RST)
        print(DIM + "Comando SCP (execute no seu computador local):" + RST)
        print(W  + f'  scp usuario@IP_VPS:"{abs_dir}/audit_*" .' + RST)
        print()
        input(G + "Pressione Enter para continuar..." + RST)
        print()


# ── Seleção dinâmica de plugin ───────────────────────────────────────────

def _load_plugin(path: str) -> types.ModuleType:
    """Carrega um arquivo .py como módulo usando importlib."""
    path = os.path.abspath(path)
    mod_name = os.path.splitext(os.path.basename(path))[0]
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Não foi possível criar spec para {path}")
    mod = importlib.util.module_from_spec(spec)
    # Adiciona o diretório do plugin ao sys.path para que ele resolva imports relativos
    plugin_dir = os.path.dirname(path)
    if plugin_dir not in sys.path:
        sys.path.insert(0, plugin_dir)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _scan_plugins(search_dir: str) -> List[str]:
    """
    Varre search_dir em busca de arquivos .py que contenham 'def propose('.
    Retorna lista de caminhos absolutos ordenados por nome.
    Exclui o próprio motor para não aparecer na lista.
    """
    engine_file = os.path.join(_APP_DIR, "binance_live_engine.py")
    found: List[str] = []
    try:
        for name in sorted(os.listdir(search_dir)):
            if not name.endswith(".py"):
                continue
            full = os.path.join(search_dir, name)
            if os.path.abspath(full) == engine_file:
                continue
            try:
                with open(full, "r", encoding="utf-8", errors="replace") as f:
                    if "def propose(" in f.read():
                        found.append(full)
            except OSError:
                pass
    except OSError:
        pass
    return found


def _select_plugin() -> types.ModuleType:
    """
    Wizard interativo de seleção de plugin.

    1. Varre o diretório do motor em busca de plugins válidos.
    2. Lista numerado no terminal.
    3. Permite digitar o número, o caminho direto, ou buscar em outra pasta.
    4. Valida que o módulo carregado exporta propose() callable.
    """
    engine_dir = _APP_DIR

    while True:
        search_dir = engine_dir
        plugins    = _scan_plugins(search_dir)

        _clr()
        print(BG + "PLUGIN DE ESTRATÉGIA" + RST)
        print(G + f"Pasta pesquisada: {search_dir}" + RST)
        print()

        if plugins:
            print(G + "Plugins encontrados (arquivos com def propose()):" + RST)
            for i, p in enumerate(plugins):
                try:
                    rel = os.path.relpath(p, search_dir)
                except ValueError:
                    rel = p   # drives diferentes no Windows — usa caminho absoluto
                try:
                    # Tenta extrair STRATEGY_DISPLAY_NAME sem executar o módulo completo
                    with open(p, "r", encoding="utf-8", errors="replace") as f:
                        src = f.read()
                    m = re.search(r'STRATEGY_DISPLAY_NAME\s*=\s*["\']([^"\']+)["\']', src)
                    tag = f"  ({m.group(1)})" if m else ""
                except OSError:
                    tag = ""
                print(f"  {G}{i}{RST}  {rel}{DIM}{tag}{RST}")
        else:
            print(Y + "Nenhum plugin encontrado nesta pasta." + RST)

        print()
        print(DIM + "  Digite o número, um caminho completo para o .py, " + RST)
        print(DIM + "  ou 'p' para pesquisar em outra pasta." + RST)
        print()

        raw = input(G + "Plugin [0]: " + RST).strip()

        # Pesquisar em outra pasta
        if raw.lower() == "p":
            nova = input(G + "Caminho da pasta: " + RST).strip()
            if not os.path.isdir(nova):
                print(R + "Pasta não encontrada. Tente novamente." + RST + "\n")
                continue   # mantém engine_dir inalterado, relista a pasta atual
            engine_dir = os.path.abspath(nova)
            continue

        # Caminho direto para um .py
        if raw.endswith(".py") or os.sep in raw or "/" in raw:
            candidate = raw if os.path.isabs(raw) else os.path.join(engine_dir, raw)
            if not os.path.isfile(candidate):
                print(R + f"Arquivo não encontrado: {candidate}" + RST + "\n")
                continue
            path = candidate

        # Número da lista
        else:
            idx_raw = raw if raw else "0"
            try:
                idx = int(idx_raw)
            except ValueError:
                print(Y + "Entrada inválida. Digite um número ou 'p'." + RST + "\n")
                continue
            if not plugins or not (0 <= idx < len(plugins)):
                print(Y + f"Escolha entre 0 e {len(plugins) - 1}." + RST + "\n")
                continue
            path = plugins[idx]

        # Carrega e valida
        try:
            display_path = os.path.relpath(path, engine_dir)
        except ValueError:
            display_path = path
        print()
        print(G + f"Carregando: {display_path}" + RST, end="", flush=True)
        try:
            mod = _load_plugin(path)
        except Exception as exc:
            print(R + f"\nERRO ao carregar plugin: {exc}" + RST + "\n")
            continue

        if not callable(getattr(mod, "propose", None)):
            print(R + "\nERRO: módulo não exporta propose() callable." + RST + "\n")
            continue

        name = str(getattr(mod, "STRATEGY_DISPLAY_NAME", os.path.basename(path)))
        print(G + f"  OK  —  {name}" + RST)
        print()
        return mod


def _banner(live: bool) -> None:
    _clr()
    width = BOX_W
    print(BG + "╔" + "═" * (width - 2) + "╗")
    print("║" + " " * (width - 2) + "║")
    title = "MICRO-BANCA v9 — BINANCE MARKET MAKING ENGINE"
    sub   = "Desenvolvido por MK  |  Versão 9.0  |  Binance Direct API"
    print("║" + title.center(width - 2) + "║")
    print("║" + sub.center(width - 2) + "║")
    print("║" + " " * (width - 2) + "║")
    if live:
        warn = "⚠  MODO LIVE — OPERAÇÕES COM DINHEIRO REAL  ⚠"
        print("║" + R + warn.center(width - 2) + BG + "║")
    else:
        note = "●  MODO TESTNET — Operações simuladas em ambiente seguro"
        print("║" + Y + note.center(width - 2) + BG + "║")
    print("║" + " " * (width - 2) + "║")
    print("╚" + "═" * (width - 2) + "╝" + RST)
    print()


def _ask_credentials(live: bool, strat: types.ModuleType) -> Tuple[str, str]:
    if live:
        print(R + "ATENÇÃO: Modo LIVE ativo. Use chaves com permissão apenas de TRADING.")
        print("Nunca use chaves com permissão de saque." + RST)
        print()
    else:
        print(Y + "Modo TESTNET. Chaves: https://testnet.binance.vision" + RST)
        print()

    api_key    = input(G + "API Key    : " + RST).strip()
    api_secret = getpass.getpass(G + "API Secret : " + RST).strip()
    print()
    return api_key, api_secret


def _ask_config(strat: types.ModuleType) -> Tuple[float, int, int]:
    print(BG + "BANCA VIRTUAL" + RST)
    print(G + "Define o capital máximo operado pelo bot (em USDT).")
    print("Em modo Live, mesmo que seu saldo real seja maior, o bot")
    print("opera apenas com este valor. O PnL real é proporcional." + RST)
    print()

    while True:
        try:
            raw = input(G + "Banca virtual USDT [padrão 100]: " + RST).strip()
            vb  = float(raw) if raw else 100.0
            if vb > 0:
                break
            print(Y + "Valor deve ser positivo." + RST)
        except ValueError:
            print(Y + "Digite um número. Ex: 100" + RST)

    print()
    sym_index: dict = getattr(strat, "_SYMBOL_INDEX", {})
    valid_keys = sorted(sym_index.keys()) if sym_index else []

    print(BG + "ATIVO" + RST)
    if sym_index:
        for idx in valid_keys:
            print(f"  {G}{idx}{RST} = {sym_index[idx]}")
    else:
        print(DIM + "  (plugin não define _SYMBOL_INDEX — será usado o symbol padrão BTCUSDT)" + RST)

    prompt_range = f"[{valid_keys[0]}-{valid_keys[-1]}]" if valid_keys else ""
    while True:
        try:
            raw = input(G + f"Ativo {prompt_range} [padrão 0]: " + RST).strip()
            sym_idx = int(raw) if raw else 0
            # Valida contra as chaves reais — rejeita índices não mapeados
            if not sym_index or sym_idx in sym_index:
                break
            print(Y + f"Índice inválido. Escolha um dos valores: {valid_keys}" + RST)
        except ValueError:
            print(Y + "Digite um número inteiro." + RST)

    print()
    mode = 0
    return vb, sym_idx, mode


def _test_connection(client: Client, symbol: str) -> bool:
    print(G + "Testando conexão..." + RST, end="", flush=True)
    try:
        client.ping()
        print(G + " OK" + RST)

        print(G + f"Verificando símbolo {symbol}..." + RST, end="", flush=True)
        ticker = client.get_symbol_ticker(symbol=symbol)
        price  = float(ticker["price"])
        print(G + f" OK  |  ${price:,.4f}" + RST)

        print(G + "Verificando saldo USDT..." + RST, end="", flush=True)
        acct  = client.get_account()
        usdt  = next(
            (float(b["free"]) for b in acct["balances"] if b["asset"] == "USDT"),
            0.0,
        )
        print(G + f" OK  |  ${usdt:.4f} USDT disponível" + RST)
        return True

    except BinanceAPIException as exc:
        print(R + f"\nERRO API Binance [{exc.code}]: {exc.message}" + RST)
        return False
    except Exception as exc:
        print(R + f"\nERRO: {exc}" + RST)
        return False


# ═══════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════

def main() -> None:
    live = LIVE_MODE
    _banner(live)

    # Credenciais guardadas entre sessões para evitar redigitar no VPS
    _cached_creds: Optional[Tuple[str, str]] = None

    # Referência ao engine ativo (usada pelo handler de sinal)
    _active_engine: Optional[TradingEngine] = None   # type: ignore[name-defined]

    def _on_signal(sig, frame) -> None:  # não encerra o processo — apenas para o engine
        if _active_engine is not None:
            _active_engine.shutdown()

    signal.signal(signal.SIGINT, _on_signal)
    try:
        signal.signal(signal.SIGTERM, _on_signal)
    except (OSError, AttributeError):
        pass

    # ═══════════════════════════════════════════════════════════════════════
    #  LOOP PRINCIPAL — cada iteração = uma sessão de trading
    # ═══════════════════════════════════════════════════════════════════════
    while True:
        try:
            # ── 1. Seleciona plugin ───────────────────────────────────────
            strat = _select_plugin()

            # ── 2. Auditoria da sessão anterior ───────────────────────────
            #  Oferece download ANTES de apagar, para não perder dados
            _audit_download_menu(_AUDIT_DIR)
            n_removed = _clear_session_files(_AUDIT_DIR)
            if n_removed:
                print(G + f"  {n_removed} arquivo(s) da sessão anterior removidos." + RST)
                time.sleep(0.8)

            # ── 3. Credenciais (reutiliza se disponível) ──────────────────
            if _cached_creds:
                try:
                    reuse = input(
                        G + "Reutilizar credenciais da sessão anterior? [S/n]: " + RST
                    ).strip().lower()
                except (EOFError, KeyboardInterrupt):
                    print()
                    raise KeyboardInterrupt

                if reuse in ("n", "nao", "não", "no"):
                    api_key, api_secret = _ask_credentials(live, strat)
                else:
                    api_key, api_secret = _cached_creds
            else:
                api_key, api_secret = _ask_credentials(live, strat)

            if not api_key or not api_secret:
                print(R + "Chaves inválidas. Voltando ao menu." + RST)
                time.sleep(1)
                continue

            _cached_creds = (api_key, api_secret)

            # ── 4. Configuração da banca e ativo ──────────────────────────
            vb_initial, sym_idx, mode = _ask_config(strat)
            sym_index = getattr(strat, "_SYMBOL_INDEX", {})
            symbol    = sym_index.get(sym_idx, "BTCUSDT")

            print()
            client = Client(api_key, api_secret, testnet=not live)

            if not _test_connection(client, symbol):
                print(R + "\nFalha na conexão. Verifique as chaves e tente novamente." + RST)
                time.sleep(2)
                continue

            mode_label   = str(getattr(strat, "STRATEGY_DISPLAY_NAME", "PLUGIN")).upper()
            _defaults    = getattr(strat, "TESTNET_SOAK_DEFAULTS", {})
            interval_sec = float(_defaults.get("interval_sec", 8.0))
            resting_sec  = float(_defaults.get("resting_order_min_age_sec", 16.0))
            max_cycles   = int(_defaults.get("max_cycles", 0))

            print()
            print(BG + "═" * 50)
            print("  CONFIGURAÇÃO CONFIRMADA")
            print("═" * 50 + RST)
            print(f"  {G}Plugin     :{RST} {strat.__name__}")
            print(f"  {G}Estratégia :{RST} {mode_label}")
            print(f"  {G}Rede       :{RST} {'LIVE (dinheiro real)' if live else 'TESTNET'}")
            print(f"  {G}Ativo      :{RST} {symbol}")
            print(f"  {G}Banca virt.:{RST} ${vb_initial:.2f} USDT")
            print(f"  {G}Intervalo  :{RST} {interval_sec:.0f}s por ciclo")
            print(f"  {G}Resting min:{RST} {resting_sec:.0f}s antes de cancelar")
            if max_cycles:
                print(f"  {G}Soak       :{RST} {max_cycles} ciclos (~{max_cycles*interval_sec/60:.1f} min)")
            else:
                print(f"  {G}Soak       :{RST} ilimitado (Ctrl+C para parar)")
            print(BG + "═" * 50 + RST)
            print()

            try:
                confirm = input(G + "Iniciar o motor? [S/n]: " + RST).strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                raise KeyboardInterrupt

            if confirm in ("n", "nao", "não", "no"):
                print(Y + "Operação cancelada — voltando ao menu." + RST)
                time.sleep(0.5)
                continue

            # ── 5. Inicialização dos componentes ──────────────────────────
            session_id = f"{strat.__name__}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"

            vbank = VirtualBank(
                initial_quote = vb_initial,
                quote_balance = vb_initial,
                base_balance  = 0.0,
            )

            binance_eng = BinanceEngine(client=client, vbank=vbank, symbol=symbol)
            logger      = AuditLogger(session_id=session_id, out_dir=_AUDIT_DIR)

            engine = TradingEngine(
                binance  = binance_eng,
                vbank    = vbank,
                logger   = logger,
                symbol   = symbol,
                sym_idx  = sym_idx,
                mode     = mode,
                live     = live,
                strat    = strat,
            )

            # Aponta o handler de sinal para o engine desta sessão
            _active_engine = engine  # type: ignore[assignment]

            logger.log("SESSION_START", {
                "symbol":      symbol,
                "mode_label":  mode_label,
                "notes":       f"live={live}  vb_initial={vb_initial}  plugin={strat.__name__}",
                "cycle":       0,
            })

            print()
            print(G + "Motor iniciado. Pressione Ctrl+C para encerrar com segurança." + RST)
            time.sleep(1.5)

            # ── 6. Executa ────────────────────────────────────────────────
            engine.run()

            _active_engine = None

            # ── 7. Pós-sessão ─────────────────────────────────────────────
            print()
            print(BG + "  ★ Sessão encerrada." + RST)
            try:
                go_next = input(
                    G + "Iniciar nova sessão? [S/n]: " + RST
                ).strip().lower()
            except (EOFError, KeyboardInterrupt):
                # Em VPS sem terminal interativo, continua automaticamente
                print(Y + "\n[sem terminal — iniciando nova sessão automaticamente]" + RST)
                time.sleep(3)
                continue

            if go_next in ("n", "nao", "não", "no"):
                # Oferece download final antes de sair
                _audit_download_menu(_AUDIT_DIR)
                print(G + "Encerrando o motor. Até logo." + RST)
                break
            # Senão: loop volta ao topo → novo _select_plugin()

        except KeyboardInterrupt:
            # Ctrl+C no menu (fora do engine) → encerra o programa
            _active_engine = None
            print(f"\n{Y}Encerrado pelo usuário.{RST}")
            _audit_download_menu(_AUDIT_DIR)
            break


if __name__ == "__main__":
    main()
