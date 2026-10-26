"""projects/_template/module.py — copy-paste starter for a new project.

1. Copy folder to projects/<id>/ (no leading _)
2. Edit project.toml (id must match folder name)
3. Implement parse / validate / submit (HTTP here or in api.py)
4. Restart bot or /reload_projects
"""

from __future__ import annotations

from bot.http_client import is_valid_wallet, random_fingerprint

# import requests  # when implementing submit()

ID = "example"
NAME = "Example"
OK_LIMIT = 30

# URL = "https://example.com/api/join"
# SITE_ORIGIN = "https://example.com"
# SITE_REFERER = "https://example.com/"


def help_html() -> str:
    return (
        "Format (one line):\n"
        "<code>handle,0xYourWallet...</code>\n\n"
        "Example:\n"
        "<code>myHandle,0x1234...abcd</code>"
    )


def parse(text: str) -> dict | None:
    text = (text or "").strip()
    if not text or text.startswith("/") or "," not in text:
        return None
    handle, wallet = text.split(",", 1)
    handle, wallet = handle.strip().lstrip("@"), wallet.strip()
    if not handle or not wallet:
        return None
    return {"handle": handle, "wallet": wallet}


def validate(fields: dict) -> str | None:
    wallet = fields.get("wallet", "")
    if not is_valid_wallet(wallet):
        return "Invalid wallet. Need <code>0x</code> + 40 hex chars."
    return None


def submit(
    fields: dict,
    *,
    proxies: list[str],
    proxy_idx: int,
    retries: int,
    retry_delay: float,
    min_delay: float,
    max_delay: float,
    **_extra,
) -> dict:
    """Return dict with at least status (OK|ALREADY|BAD_RESP|FAIL) and detail."""
    # Implement HTTP with bot.http_client helpers (random_fingerprint, etc.)
    _ = (proxies, proxy_idx, retries, retry_delay, min_delay, max_delay, random_fingerprint)
    return {
        "status": "FAIL",
        "detail": "not implemented",
        "handle": fields.get("handle"),
        "wallet": fields.get("wallet"),
    }


BULK_COLUMNS = [
    ("handle", True, "twitter handle"),
    ("wallet", True, "wallet 0x..."),
]


def get_project():
    return {
        "id": ID,
        "name": NAME,
        "ok_limit": OK_LIMIT,
        "help_html": help_html,
        "parse": parse,
        "validate": validate,
        "submit": submit,
        "bulk_columns": BULK_COLUMNS,
        # "allow_direct": False,  # always use proxies
        # "prefer_direct": False,
    }
