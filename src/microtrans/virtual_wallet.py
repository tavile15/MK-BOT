from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class TradeRecord:
    ts: datetime
    side: str
    price: float
    base_qty: float
    quote_qty: float
    fee_quote: float
    tag: str = ""


@dataclass
class VirtualWallet:
    """Carteira simulada (quote + base), com rastreio de taxas e PnL vs. patrimônio inicial."""

    symbol: str
    quote_asset: str
    base_asset: str
    quote_balance: float
    base_balance: float
    fee_bps: float = 10.0
    trades: list[TradeRecord] = field(default_factory=list)
    _initial_quote: float = field(init=False, repr=False)
    _initial_base: float = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_initial_quote", float(self.quote_balance))
        object.__setattr__(self, "_initial_base", float(self.base_balance))

    def equity_quote(self, mid: float) -> float:
        return self.quote_balance + self.base_balance * mid

    def baseline_equity_quote(self, mid: float) -> float:
        """Patrimônio de referência no mesmo preço (marcação ao mid atual)."""
        return self._initial_quote + self._initial_base * mid

    def pnl_quote(self, mid: float) -> float:
        """Ganho/perda vs. saldos iniciais, marcando o estoque base ao preço atual."""
        return self.equity_quote(mid) - self.baseline_equity_quote(mid)

    def total_fees_quote(self) -> float:
        return float(sum(t.fee_quote for t in self.trades))

    def volume_quote_turnover(self) -> float:
        return float(sum(t.quote_qty for t in self.trades))

    def trades_to_csv(self) -> str:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["ts_utc", "lado", "preco", "qtd_base", "quote", "taxa_quote", "tag"])
        for t in self.trades:
            w.writerow(
                [
                    t.ts.isoformat(),
                    t.side,
                    f"{t.price:.12g}",
                    f"{t.base_qty:.12g}",
                    f"{t.quote_qty:.12g}",
                    f"{t.fee_quote:.12g}",
                    t.tag,
                ]
            )
        return buf.getvalue()

    def buy(self, price: float, quote_amount: float, tag: str = "") -> TradeRecord | None:
        if quote_amount <= 0 or price <= 0:
            return None
        gross_base = quote_amount / price
        fee = quote_amount * (self.fee_bps / 10000.0)
        total = quote_amount + fee
        if total > self.quote_balance + 1e-12:
            return None
        self.quote_balance -= total
        self.base_balance += gross_base
        tr = TradeRecord(
            ts=datetime.now(timezone.utc),
            side="BUY",
            price=price,
            base_qty=gross_base,
            quote_qty=quote_amount,
            fee_quote=fee,
            tag=tag,
        )
        self.trades.append(tr)
        return tr

    def sell(self, price: float, base_qty: float, tag: str = "") -> TradeRecord | None:
        if base_qty <= 0 or price <= 0:
            return None
        if base_qty > self.base_balance + 1e-12:
            return None
        quote_gross = base_qty * price
        fee = quote_gross * (self.fee_bps / 10000.0)
        self.base_balance -= base_qty
        self.quote_balance += quote_gross - fee
        tr = TradeRecord(
            ts=datetime.now(timezone.utc),
            side="SELL",
            price=price,
            base_qty=base_qty,
            quote_qty=quote_gross,
            fee_quote=fee,
            tag=tag,
        )
        self.trades.append(tr)
        return tr

    def recent_trades(self, n: int = 20) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for t in self.trades[-n:]:
            out.append(
                {
                    "hora_utc": t.ts.strftime("%H:%M:%S"),
                    "lado": t.side,
                    "preco": round(t.price, 8),
                    "base": round(t.base_qty, 8),
                    "quote": round(t.quote_qty, 4),
                    "taxa_quote": round(t.fee_quote, 6),
                    "obs": t.tag,
                }
            )
        return out

    def summary(self, mid: float) -> dict[str, Any]:
        eq = self.equity_quote(mid)
        pnl = self.pnl_quote(mid)
        fees = self.total_fees_quote()
        n = len(self.trades)
        vol = self.volume_quote_turnover()
        return {
            "par": self.symbol,
            "quote_saldo": round(self.quote_balance, 6),
            "base_saldo": round(self.base_balance, 8),
            "ativo_quote": self.quote_asset,
            "ativo_base": self.base_asset,
            "preco_marcacao": round(mid, 6),
            "patrimonio_quote": round(eq, 6),
            "patrimonio_inicial_ref_quote": round(self.baseline_equity_quote(mid), 6),
            "pnl_vs_inicio_quote": round(pnl, 6),
            "taxas_pagas_total_quote": round(fees, 6),
            "volume_quote_negociado": round(vol, 6),
            "taxa_media_por_operacao": round(fees / n, 8) if n else 0.0,
            "n_operacoes": n,
        }

    def explain(self, mid: float) -> str:
        s = self.summary(mid)
        return (
            f"Carteira {self.symbol}: "
            f"{s['quote_saldo']:.4f} {self.quote_asset} + "
            f"{s['base_saldo']:.8f} {self.base_asset} "
            f"~ patrimônio {s['patrimonio_quote']:.4f} {self.quote_asset} "
            f"(PnL vs início: {s['pnl_vs_inicio_quote']:+.4f}, taxas: {s['taxas_pagas_total_quote']:.4f}, "
            f"operações: {s['n_operacoes']})"
        )
