from __future__ import annotations

import argparse
import logging
import threading
import time
from typing import Any

from .backtest import run_backtest
from .binance_public import BinancePublic
from .config_loader import load_config
from .engine import MarketMakingEngine
from .logging_config import setup_logging
from .symbols import split_pair
from .virtual_wallet import VirtualWallet


def _live_loop(
    symbol: str,
    poll: float,
    stop_event: threading.Event,
    cfg: dict[str, Any],
) -> None:
    log = logging.getLogger("filter")
    base, quote_a = split_pair(symbol)
    bcfg = cfg["backtest"]
    wallet = VirtualWallet(
        symbol=symbol.upper(),
        quote_asset=quote_a,
        base_asset=base,
        quote_balance=float(bcfg["initial_quote"]),
        base_balance=0.0,
        fee_bps=float(bcfg["fee_bps"]),
    )
    eng = MarketMakingEngine(symbol, wallet, cfg=cfg)
    while not stop_event.is_set():
        try:
            out = eng.tick()
            log.info(
                "LIVE tick | apt=%s | mid=%s | patrimonio=%s",
                out.get("filter_apt"),
                out.get("mid"),
                (out.get("wallet") or {}).get("patrimonio_quote"),
            )
        except Exception as e:
            log.exception("LIVE error: %s", e)
        stop_event.wait(poll)


def _backtest_thread(symbol: str, cfg: dict[str, Any]) -> None:
    log = logging.getLogger("agent")
    res = run_backtest(symbol, cfg=cfg)
    log.info("BACKTEST done | %s", res)


def main() -> None:
    p = argparse.ArgumentParser(description="Microtrans — live + backtest paralelo (demo).")
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument("--poll", type=float, default=15.0, help="Intervalo live (s)")
    p.add_argument("--mode", choices=("live", "backtest", "both"), default="both")
    p.add_argument("--log-dir", default="logs")
    args = p.parse_args()

    setup_logging(log_dir=args.log_dir)
    cfg = load_config()

    stop = threading.Event()

    threads: list[threading.Thread] = []
    if args.mode in ("live", "both"):
        t = threading.Thread(
            target=_live_loop,
            args=(args.symbol, args.poll, stop, cfg),
            daemon=True,
        )
        threads.append(t)
    if args.mode in ("backtest", "both"):
        t = threading.Thread(
            target=_backtest_thread,
            args=(args.symbol, cfg),
            daemon=True,
        )
        threads.append(t)

    for t in threads:
        t.start()

    try:
        if args.mode == "both":
            while True:
                time.sleep(1.0)
        elif args.mode == "backtest":
            for t in threads:
                t.join()
        else:
            while True:
                time.sleep(1.0)
    except KeyboardInterrupt:
        stop.set()
        for t in threads:
            t.join(timeout=5.0)


if __name__ == "__main__":
    main()
