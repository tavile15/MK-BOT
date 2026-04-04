from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Iterable


def setup_logging(
    log_dir: str | Path | None = None,
    level: int = logging.INFO,
) -> tuple[logging.Logger, logging.Logger]:
    """Dois loggers nomeados: `filter` e `agent` (+ root para o restante)."""
    root = logging.getLogger()
    if not root.handlers:
        root.setLevel(level)
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(
            logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
        )
        root.addHandler(console)

    if log_dir:
        path = Path(log_dir)
        path.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(path / "microtrans.log", encoding="utf-8")
        fh.setFormatter(
            logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
        )
        root.addHandler(fh)

    flog = logging.getLogger("filter")
    alog = logging.getLogger("agent")
    _fmt = logging.Formatter("%(asctime)s | %(name)s | %(message)s")
    for lg in (flog, alog):
        lg.setLevel(level)
        # Sem propagar ao root: cada logger `filter`/`agent` deve ter no máximo um handler ativo
        # (evita linha duplicada quando Streamlit reanexa handler mas `propagate` voltava True).
        lg.propagate = False
        if not lg.handlers:
            ch = logging.StreamHandler(sys.stdout)
            ch.setFormatter(_fmt)
            lg.addHandler(ch)
    return flog, alog


def log_dict(logger: logging.Logger, prefix: str, data: dict) -> None:
    parts: Iterable[str] = (f"{k}={v}" for k, v in sorted(data.items()))
    logger.info("%s | %s", prefix, " ".join(parts))
