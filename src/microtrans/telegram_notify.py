from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests


def _env_bool(name: str, default: bool = False) -> bool:
    raw = str(os.environ.get(name, "")).strip().lower()
    if not raw:
        return bool(default)
    return raw in {"1", "true", "on", "yes", "y", "sim"}


@dataclass
class TelegramConfig:
    enabled: bool
    bot_token: str
    chat_id: str
    thread_id: int | None = None
    message_prefix: str = ""
    send_report_files: bool = True
    header_image: str = ""

    @staticmethod
    def from_env() -> "TelegramConfig":
        thread_raw = str(os.environ.get("MT_TELEGRAM_THREAD_ID", "")).strip()
        thread_id: int | None = None
        if thread_raw:
            try:
                thread_id = int(thread_raw)
            except Exception:
                thread_id = None
        return TelegramConfig(
            enabled=_env_bool("MT_TELEGRAM_ENABLED", default=False),
            bot_token=str(os.environ.get("MT_TELEGRAM_BOT_TOKEN", "")).strip(),
            chat_id=str(os.environ.get("MT_TELEGRAM_CHAT_ID", "")).strip(),
            thread_id=thread_id,
            message_prefix=str(os.environ.get("MT_TELEGRAM_PREFIX", "")).strip(),
            send_report_files=_env_bool("MT_TELEGRAM_SEND_REPORT_FILES", default=True),
            header_image=str(os.environ.get("MT_TELEGRAM_HEADER_IMAGE", "")).strip(),
        )

    def can_send(self) -> bool:
        return bool(self.enabled and self.bot_token and self.chat_id)


def _send_via_curl(*, cfg: TelegramConfig, final_text: str, timeout_sec: float) -> dict[str, Any]:
    cmd = [
        "curl",
        "-sS",
        "--max-time",
        str(max(5.0, float(timeout_sec))),
        "-X",
        "POST",
        f"https://api.telegram.org/bot{cfg.bot_token}/sendMessage",
        "-d",
        f"chat_id={cfg.chat_id}",
        "-d",
        f"text={final_text}",
    ]
    if cfg.thread_id is not None:
        cmd.extend(["-d", f"message_thread_id={int(cfg.thread_id)}"])
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, check=False)
        out = (p.stdout or "").strip()
        err = (p.stderr or "").strip()
        data: dict[str, Any] = {}
        if out:
            try:
                data = json.loads(out)
            except Exception:
                data = {"raw": out[:400]}
        if p.returncode != 0:
            return {
                "ok": False,
                "status": "curl_error",
                "returncode": int(p.returncode),
                "stderr": err[:400],
                "response": data,
            }
        if isinstance(data, dict) and bool(data.get("ok")):
            return {"ok": True, "status": "sent_via_curl", "response": data}
        return {"ok": False, "status": "curl_bad_response", "response": data, "stderr": err[:400]}
    except Exception as e:
        return {"ok": False, "status": "curl_exception", "error": str(e)}


def _send_document_via_curl(
    *,
    cfg: TelegramConfig,
    file_path: Path,
    caption: str,
    timeout_sec: float,
) -> dict[str, Any]:
    cmd = [
        "curl",
        "-sS",
        "--max-time",
        str(max(5.0, float(timeout_sec))),
        "-X",
        "POST",
        f"https://api.telegram.org/bot{cfg.bot_token}/sendDocument",
        "-F",
        f"chat_id={cfg.chat_id}",
        "-F",
        f"caption={caption}",
        "-F",
        f"document=@{str(file_path)}",
    ]
    if cfg.thread_id is not None:
        cmd.extend(["-F", f"message_thread_id={int(cfg.thread_id)}"])
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, check=False)
        out = (p.stdout or "").strip()
        err = (p.stderr or "").strip()
        data: dict[str, Any] = {}
        if out:
            try:
                data = json.loads(out)
            except Exception:
                data = {"raw": out[:400]}
        if p.returncode != 0:
            return {
                "ok": False,
                "status": "curl_error",
                "returncode": int(p.returncode),
                "stderr": err[:400],
                "response": data,
            }
        if isinstance(data, dict) and bool(data.get("ok")):
            return {"ok": True, "status": "sent_via_curl", "response": data}
        return {"ok": False, "status": "curl_bad_response", "response": data, "stderr": err[:400]}
    except Exception as e:
        return {"ok": False, "status": "curl_exception", "error": str(e)}


def _send_photo_via_curl(
    *,
    cfg: TelegramConfig,
    file_path: Path,
    caption: str,
    timeout_sec: float,
) -> dict[str, Any]:
    cmd = [
        "curl",
        "-sS",
        "--max-time",
        str(max(5.0, float(timeout_sec))),
        "-X",
        "POST",
        f"https://api.telegram.org/bot{cfg.bot_token}/sendPhoto",
        "-F",
        f"chat_id={cfg.chat_id}",
        "-F",
        f"caption={caption}",
        "-F",
        f"photo=@{str(file_path)}",
    ]
    if cfg.thread_id is not None:
        cmd.extend(["-F", f"message_thread_id={int(cfg.thread_id)}"])
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, check=False)
        out = (p.stdout or "").strip()
        err = (p.stderr or "").strip()
        data: dict[str, Any] = {}
        if out:
            try:
                data = json.loads(out)
            except Exception:
                data = {"raw": out[:400]}
        if p.returncode != 0:
            return {
                "ok": False,
                "status": "curl_error",
                "returncode": int(p.returncode),
                "stderr": err[:400],
                "response": data,
            }
        if isinstance(data, dict) and bool(data.get("ok")):
            return {"ok": True, "status": "sent_via_curl", "response": data}
        return {"ok": False, "status": "curl_bad_response", "response": data, "stderr": err[:400]}
    except Exception as e:
        return {"ok": False, "status": "curl_exception", "error": str(e)}


def send_text_message(*, text: str, cfg: TelegramConfig | None = None, timeout_sec: float = 15.0) -> dict[str, Any]:
    cfg0 = cfg or TelegramConfig.from_env()
    if not cfg0.enabled:
        return {"ok": False, "status": "disabled"}
    if not cfg0.bot_token or not cfg0.chat_id:
        return {"ok": False, "status": "missing_token_or_chat_id"}
    final_text = text
    if cfg0.message_prefix:
        final_text = f"{cfg0.message_prefix}\n{final_text}"
    url = f"https://api.telegram.org/bot{cfg0.bot_token}/sendMessage"
    payload: dict[str, Any] = {
        "chat_id": cfg0.chat_id,
        "text": final_text,
        "disable_web_page_preview": True,
    }
    if cfg0.thread_id is not None:
        payload["message_thread_id"] = int(cfg0.thread_id)
    try:
        resp = requests.post(url, json=payload, timeout=max(5.0, float(timeout_sec)))
        ok = bool(resp.ok)
        data: dict[str, Any]
        try:
            data = resp.json() if resp.text.strip() else {}
        except Exception:
            data = {"raw": resp.text[:400]}
        if not ok:
            return {
                "ok": False,
                "status": "http_error",
                "http_status": resp.status_code,
                "response": data,
            }
        return {"ok": True, "status": "sent", "response": data}
    except Exception as e:
        fallback = _send_via_curl(cfg=cfg0, final_text=final_text, timeout_sec=timeout_sec)
        if fallback.get("ok"):
            return fallback
        return {
            "ok": False,
            "status": "exception",
            "error": str(e),
            "fallback": fallback,
        }


def send_photo(
    *,
    file_path: str | Path,
    caption: str = "",
    cfg: TelegramConfig | None = None,
    timeout_sec: float = 25.0,
) -> dict[str, Any]:
    cfg0 = cfg or TelegramConfig.from_env()
    p = Path(file_path)
    if not p.exists() or not p.is_file():
        return {"ok": False, "status": "file_not_found", "path": str(p)}
    if not cfg0.enabled:
        return {"ok": False, "status": "disabled"}
    if not cfg0.bot_token or not cfg0.chat_id:
        return {"ok": False, "status": "missing_token_or_chat_id"}
    cap = caption
    if cfg0.message_prefix:
        cap = f"{cfg0.message_prefix} | {caption}".strip()
    url = f"https://api.telegram.org/bot{cfg0.bot_token}/sendPhoto"
    data: dict[str, Any] = {"chat_id": cfg0.chat_id, "caption": cap}
    if cfg0.thread_id is not None:
        data["message_thread_id"] = int(cfg0.thread_id)
    try:
        with p.open("rb") as fp:
            files = {"photo": (p.name, fp)}
            resp = requests.post(url, data=data, files=files, timeout=max(8.0, float(timeout_sec)))
        try:
            payload = resp.json() if resp.text.strip() else {}
        except Exception:
            payload = {"raw": resp.text[:400]}
        if resp.ok and bool(payload.get("ok", True)):
            return {"ok": True, "status": "sent", "response": payload}
        return {"ok": False, "status": "http_error", "http_status": resp.status_code, "response": payload}
    except Exception as e:
        fallback = _send_photo_via_curl(cfg=cfg0, file_path=p, caption=cap, timeout_sec=timeout_sec)
        if fallback.get("ok"):
            return fallback
        return {
            "ok": False,
            "status": "exception",
            "error": str(e),
            "fallback": fallback,
        }


def send_document(
    *,
    file_path: str | Path,
    caption: str = "",
    cfg: TelegramConfig | None = None,
    timeout_sec: float = 25.0,
) -> dict[str, Any]:
    cfg0 = cfg or TelegramConfig.from_env()
    p = Path(file_path)
    if not p.exists() or not p.is_file():
        return {"ok": False, "status": "file_not_found", "path": str(p)}
    if not cfg0.enabled:
        return {"ok": False, "status": "disabled"}
    if not cfg0.bot_token or not cfg0.chat_id:
        return {"ok": False, "status": "missing_token_or_chat_id"}
    cap = caption
    if cfg0.message_prefix:
        cap = f"{cfg0.message_prefix} | {caption}".strip()
    url = f"https://api.telegram.org/bot{cfg0.bot_token}/sendDocument"
    data: dict[str, Any] = {"chat_id": cfg0.chat_id, "caption": cap}
    if cfg0.thread_id is not None:
        data["message_thread_id"] = int(cfg0.thread_id)
    try:
        with p.open("rb") as fp:
            files = {"document": (p.name, fp)}
            resp = requests.post(url, data=data, files=files, timeout=max(8.0, float(timeout_sec)))
        try:
            payload = resp.json() if resp.text.strip() else {}
        except Exception:
            payload = {"raw": resp.text[:400]}
        if resp.ok and bool(payload.get("ok", True)):
            return {"ok": True, "status": "sent", "response": payload}
        return {"ok": False, "status": "http_error", "http_status": resp.status_code, "response": payload}
    except Exception as e:
        fallback = _send_document_via_curl(cfg=cfg0, file_path=p, caption=cap, timeout_sec=timeout_sec)
        if fallback.get("ok"):
            return fallback
        return {
            "ok": False,
            "status": "exception",
            "error": str(e),
            "fallback": fallback,
        }
