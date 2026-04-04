from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class StrategyParams:
    """Parâmetros gerados pelo agente (uma vez por ciclo)."""

    spread_bps: float
    order_size_quote: float
    max_inventory_base: float
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "spread_bps": self.spread_bps,
            "order_size_quote": self.order_size_quote,
            "max_inventory_base": self.max_inventory_base,
            "meta": self.meta,
        }
