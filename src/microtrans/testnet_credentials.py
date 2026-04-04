"""Carrega BINANCE_TESTNET_* a partir de ficheiros .env (evita duplicar lógica no CLI e em BinanceSigned)."""

from __future__ import annotations

import os
from pathlib import Path


def project_root_from_package() -> Path:
    return Path(__file__).resolve().parents[2]


def _read_env_file_text(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "utf-16", "utf-16-le"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _parse_env_file(content: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in content.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key:
            out[key] = val
    return out


def load_testnet_credentials_from_files() -> None:
    """
    Preenche os.environ a partir de ficheiros se KEY/SECRET estiverem ausentes ou vazias.
    Não substitui valores já definidos e não vazios no ambiente.
    """
    root = project_root_from_package()
    candidates: list[Path] = []
    for env_name in ("BINANCE_TESTNET_ENV_FILE", "MICROTRANS_BINANCE_TESTNET_ENV_FILE"):
        p = os.environ.get(env_name, "").strip()
        if p:
            candidates.append(Path(p).expanduser())
    candidates.extend([root / ".env.testnet", root / "config" / ".env.testnet"])
    seen: set[Path] = set()
    allowed_keys = {
        "BINANCE_TESTNET_API_KEY",
        "BINANCE_TESTNET_API_SECRET",
        "MT_TELEGRAM_ENABLED",
        "MT_TELEGRAM_BOT_TOKEN",
        "MT_TELEGRAM_CHAT_ID",
        "MT_TELEGRAM_THREAD_ID",
        "MT_TELEGRAM_PREFIX",
        "MT_TELEGRAM_SEND_REPORT_FILES",
        "MT_TELEGRAM_HEADER_IMAGE",
    }
    for path in candidates:
        try:
            path = path.resolve()
        except OSError:
            continue
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        try:
            data = _parse_env_file(_read_env_file_text(path))
        except OSError:
            continue
        for k, v in data.items():
            if k not in allowed_keys or not v:
                continue
            cur = os.environ.get(k, "")
            if isinstance(cur, str) and cur.strip():
                continue
            os.environ[k] = v
