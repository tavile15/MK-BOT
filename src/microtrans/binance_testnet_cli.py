"""
CLI de smoke / diagnóstico para Spot Testnet (REST assinado).

Credenciais (por ordem de prioridade):
1) Variáveis de ambiente BINANCE_TESTNET_API_KEY / BINANCE_TESTNET_API_SECRET
2) Ficheiro apontado por BINANCE_TESTNET_ENV_FILE ou MICROTRANS_BINANCE_TESTNET_ENV_FILE
3) Ficheiro ".env.testnet" na raiz do projeto ou "config/.env.testnet"

  copy .env.testnet.example .env.testnet
  set PYTHONPATH=src
  python -m microtrans.binance_testnet_cli account
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .binance_public import BinancePublic
from .binance_signed import BinanceSigned
from .config_loader import load_config
from .testnet_credentials import load_testnet_credentials_from_files, project_root_from_package
from .testnet_order_smoke import run_limit_buy_place_and_cancel


def _project_root() -> Path:
    return project_root_from_package()


def _make_signed_client() -> BinanceSigned:
    return BinanceSigned.from_env(require_testnet=True)


def _client_public_testnet() -> BinancePublic:
    cfg = load_config()
    ex = cfg.get("execution") or {}
    base = str(ex.get("testnet_base_url") or "https://testnet.binance.vision")
    return BinancePublic(base_url=base)


def cmd_account(_: argparse.Namespace) -> int:
    c = _make_signed_client()
    acc = c.account()
    # Resumo legível (sem despejar tudo)
    bals = acc.get("balances") or []
    non_zero = [b for b in bals if float(b.get("free", 0) or 0) > 0 or float(b.get("locked", 0) or 0) > 0]
    print(json.dumps({"makerCommission": acc.get("makerCommission"), "takerCommission": acc.get("takerCommission"), "canTrade": acc.get("canTrade"), "balances_non_zero": non_zero[:40], "balances_total": len(bals)}, indent=2, ensure_ascii=False))
    return 0


def cmd_open_orders(args: argparse.Namespace) -> int:
    c = _make_signed_client()
    oo = c.open_orders(args.symbol)
    print(json.dumps(oo, indent=2, ensure_ascii=False))
    return 0


def cmd_ping(_: argparse.Namespace) -> int:
    p = _client_public_testnet()
    p.ping()
    print("testnet public ping: ok")
    return 0


def cmd_book(args: argparse.Namespace) -> int:
    p = _client_public_testnet()
    d = p.depth(args.symbol, limit=min(args.limit, 100))
    print(json.dumps({"lastUpdateId": d.get("lastUpdateId"), "bids_head": (d.get("bids") or [])[:5], "asks_head": (d.get("asks") or [])[:5]}, indent=2, ensure_ascii=False))
    return 0


def _mask_secret(s: str) -> str:
    if not s.strip():
        return "(vazio)"
    t = s.strip()
    return f"comprimento={len(t)} início={t[:4]}…"


def cmd_order_smoke(args: argparse.Namespace) -> int:
    pub = _client_public_testnet()
    signed = _make_signed_client()
    out = run_limit_buy_place_and_cancel(
        args.symbol,
        pub=pub,
        signed=signed,
        ticks_below_best_bid=int(args.ticks_below),
    )
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0 if out.get("ok") else 1


def cmd_doctor(_: argparse.Namespace) -> int:
    root = _project_root()
    f_root = root / ".env.testnet"
    f_cfg = root / "config" / ".env.testnet"
    load_testnet_credentials_from_files()
    key = os.environ.get("BINANCE_TESTNET_API_KEY", "")
    sec = os.environ.get("BINANCE_TESTNET_API_SECRET", "")
    print(
        json.dumps(
            {
                "project_root": str(root),
                "ficheiro_esperado": str(f_root),
                "existe_na_raiz": f_root.is_file(),
                "existe_em_config": f_cfg.is_file(),
                "BINANCE_TESTNET_API_KEY": _mask_secret(key),
                "BINANCE_TESTNET_API_SECRET": _mask_secret(sec),
                "nota": "Não use o caminho de exemplo F:\\caminho\\para\\.env.testnet — use o caminho real ou corra sem --env-file se o ficheiro estiver na raiz do projeto.",
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Binance Spot Testnet — utilitários REST (público + assinado).")
    p.add_argument(
        "--env-file",
        default=None,
        help='Ficheiro KEY=VALUE (ex.: ".env.testnet" ou caminho completo). NÃO use o texto fictício "F:\\caminho\\para\\...".',
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", help="Mostra raiz do projeto, se .env.testnet existe e se as chaves foram carregadas (sem revelar segredos)")

    sub.add_parser("ping", help="GET público /api/v3/ping no testnet")

    sub.add_parser("account", help="GET assinado /api/v3/account (requer env com keys)")

    po = sub.add_parser("open-orders", help="Ordens abertas (opcional símbolo)")
    po.add_argument("--symbol", default=None, help="Ex.: BTCUSDT")

    pb = sub.add_parser("book", help="Livro público testnet (top N níveis)")
    pb.add_argument("--symbol", default="BTCUSDT")
    pb.add_argument("--limit", type=int, default=20)

    osm = sub.add_parser(
        "order-smoke",
        help="P4t: BUY LIMIT abaixo do bid + cancel (valida new_order/cancel na testnet)",
    )
    osm.add_argument("--symbol", default="BTCUSDT", help="Par spot testnet, ex. BTCUSDT")
    osm.add_argument(
        "--ticks-below",
        type=int,
        default=20,
        dest="ticks_below",
        help="Nº de ticks (tickSize) abaixo do melhor bid (default 20)",
    )

    args = p.parse_args(argv)
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                try:
                    stream.reconfigure(encoding="utf-8")
                except Exception:
                    pass
    if getattr(args, "env_file", None):
        os.environ["BINANCE_TESTNET_ENV_FILE"] = str(Path(args.env_file).expanduser())
    try:
        if args.cmd == "ping":
            return cmd_ping(args)
        if args.cmd == "account":
            return cmd_account(args)
        if args.cmd == "open-orders":
            return cmd_open_orders(args)
        if args.cmd == "book":
            return cmd_book(args)
        if args.cmd == "doctor":
            return cmd_doctor(args)
        if args.cmd == "order-smoke":
            return cmd_order_smoke(args)
    except OSError as e:
        msg = str(e)
        if "BINANCE_TESTNET_API" in msg:
            root = _project_root()
            msg += (
                f"\n\nCrie {root / '.env.testnet'} (veja .env.testnet.example) "
                "ou defina as variáveis nesta sessão PowerShell:\n"
                '  $env:BINANCE_TESTNET_API_KEY = "..."\n'
                '  $env:BINANCE_TESTNET_API_SECRET = "..."\n\n'
                "Diagnóstico: python -m microtrans.binance_testnet_cli doctor\n"
                "Se usou --env-file, o caminho tem de ser REAL (ex.: .\\.env.testnet), não F:\\\\caminho\\\\para\\\\..."
            )
        print(msg, file=sys.stderr)
        return 2
    except Exception as e:
        print(f"Erro: {e}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
