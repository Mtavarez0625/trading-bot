"""
Telegram alert service for the trading bot.

Reads from environment:
  TELEGRAM_BOT_TOKEN       — bot token from @BotFather
  TELEGRAM_CHAT_ID         — numeric chat / channel ID
  TELEGRAM_ALERTS_ENABLED  — "true" to enable, anything else disables

send_telegram_alert() never raises — all failures are logged as warnings
so the bot continues running even when Telegram is misconfigured or unreachable.
"""

import logging
import os
from typing import Optional

import requests as _http

logger = logging.getLogger(__name__)

# Severity → emoji displayed before the alert title
_EMOJI = {
    "info":    "ℹ️",
    "warning": "⚠️",
    "error":   "🚨",
    "success": "✅",
}


def _enabled() -> bool:
    return os.getenv("TELEGRAM_ALERTS_ENABLED", "false").lower() == "true"


def _token() -> str:
    return os.getenv("TELEGRAM_BOT_TOKEN", "").strip()


def _chat_id() -> str:
    return os.getenv("TELEGRAM_CHAT_ID", "").strip()


def send_telegram_alert(
    title: str,
    message: str,
    severity: str = "info",
) -> bool:
    """
    Send a Telegram message. Returns True on success, False on any failure.
    Silently skips when TELEGRAM_ALERTS_ENABLED is not "true" or env vars are missing.
    """
    if not _enabled():
        return False

    token   = _token()
    chat_id = _chat_id()

    if not token or not chat_id:
        logger.warning(
            "[telegram] TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set — alert skipped"
        )
        return False

    emoji = _EMOJI.get(severity, "")
    # Escape Markdown: * wraps the title for bold
    text  = f"{emoji} *{title}*\n{message}"

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id":    chat_id,
        "text":       text,
        "parse_mode": "Markdown",
    }
    try:
        resp = _http.post(url, json=payload, timeout=5)
        if resp.status_code == 200:
            return True
        logger.warning(
            f"[telegram] Unexpected status {resp.status_code}: {resp.text[:200]}"
        )
        return False
    except Exception as exc:
        logger.warning(f"[telegram] Failed to send alert '{title}': {exc}")
        return False
