"""Per-user registration data files (handles + wallets), like /proxy.

Storage: USER_DATA_DIR / <tg_id>.txt
Line formats (one account per line):
  handle|0xWallet
  handle,0xWallet
Optional extra URL columns ignored on store (regenerated per project).
"""

from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

from bot import config
from bot.http_client import is_valid_wallet, normalize_twitter
from bot.i18n import t

HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")


@dataclass
class DataRow:
    handle: str
    wallet: str  # lowercase 0x…


@dataclass
class DataParseError:
    lineno: int
    message: str


@dataclass
class DataParseResult:
    rows: list[DataRow] = field(default_factory=list)
    errors: list[DataParseError] = field(default_factory=list)
    raw_lines: int = 0

    @property
    def ok(self) -> bool:
        return bool(self.rows) and not self.errors


def user_data_path(user_id: int) -> Path:
    raw = os.getenv("USER_DATA_DIR", "").strip()
    base = Path(raw).expanduser() if raw else config.USER_DATA_DIR
    if not base.is_absolute():
        base = config.BASE_DIR / base
    return base / f"{int(user_id)}.txt"


def has_user_data(user_id: int) -> bool:
    return bool(load_user_data_rows(user_id))


# Parsed rows, keyed by the file's identity rather than by a clock: a read is
# reused only while the file is byte-for-byte the one that was parsed.
#
# This is not a micro-optimisation. Rendering one project screen calls
# `has_user_data` and `count_user_data`, and both parse the whole file; the
# mini app asks about every project on every poll, so a 695-line list was being
# parsed 42 times per screen refresh — ~70 ms of pure re-reading, four seconds
# apart, per open screen. The bot's own keyboards pay the same toll.
_rows_cache: dict[str, tuple[tuple[int, int], list[DataRow]]] = {}
_rows_lock = threading.Lock()
# Bounded by users, not by bytes, so the worst case is worth stating: 64 users
# × 1000 rows is a few megabytes of small objects. A larger ceiling would trade
# real memory for a hit rate that only matters to people polling right now.
_CACHE_MAX = 64


def load_user_data_rows(user_id: int) -> list[DataRow]:
    path = user_data_path(user_id)
    try:
        stat = path.stat()
    except OSError:
        # Missing or unreadable: forget any cached copy, so a deleted file
        # cannot keep answering.
        with _rows_lock:
            _rows_cache.pop(str(path), None)
        return []
    if not path.is_file():
        return []

    # mtime_ns *and* size: a rewrite that lands in the same nanosecond would
    # have to also match the old length to slip past both.
    stamp = (stat.st_mtime_ns, stat.st_size)
    key = str(path)
    with _rows_lock:
        hit = _rows_cache.get(key)
        if hit is not None and hit[0] == stamp:
            return list(hit[1])

    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return []
    parsed = parse_data_text(text)
    rows = list(parsed.rows) if parsed.ok else []

    with _rows_lock:
        if len(_rows_cache) >= _CACHE_MAX:
            _rows_cache.clear()
        _rows_cache[key] = (stamp, rows)
    return list(rows)


def invalidate_rows_cache(user_id: int | None = None) -> None:
    """Drop the parsed copy — belt to the stat key's braces after a write."""
    with _rows_lock:
        if user_id is None:
            _rows_cache.clear()
        else:
            _rows_cache.pop(str(user_data_path(user_id)), None)


def count_user_data(user_id: int) -> int:
    return len(load_user_data_rows(user_id))


def clear_user_data(user_id: int) -> bool:
    path = user_data_path(user_id)
    if path.is_file():
        path.unlink()
        invalidate_rows_cache(user_id)
        return True
    invalidate_rows_cache(user_id)
    return False


def delete_user_data_row(user_id: int, row_number: int) -> bool:
    """Drop one 1-indexed row and rewrite the file as canonical `handle|wallet`.

    Comments and blank lines do not survive the rewrite: the file the user
    uploaded is replaced by exactly its parsed rows minus one. That is the
    same identity the queue prepares rows from, so what the user saw as
    «строка 5» is the row that disappears.
    """
    rows = load_user_data_rows(user_id)
    n = int(row_number)
    if n < 1 or n > len(rows):
        return False
    kept = rows[: n - 1] + rows[n:]
    path = user_data_path(user_id)
    if not path.is_file():
        return False
    body = "".join(f"{r.handle}|{r.wallet}\n" for r in kept)
    try:
        path.write_text(body, encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass
    except OSError:
        return False
    invalidate_rows_cache(user_id)
    return True


def _norm_handle(raw: str) -> str:
    h = normalize_twitter((raw or "").strip().lstrip("@"), max_len=15)
    if not h or not HANDLE_RE.fullmatch(h):
        return ""
    return h


def _norm_wallet(raw: str) -> str:
    w = (raw or "").strip()
    return w.lower() if is_valid_wallet(w) else ""


def parse_data_line(
    line: str,
    lineno: int = 1,
    *,
    lang: str = "ru",
) -> tuple[DataRow | None, DataParseError | None]:
    s = (line or "").strip().lstrip("\ufeff")
    if not s or s.startswith("#"):
        return None, None

    delimiter = "|" if "|" in s else "," if "," in s else ""
    if not delimiter:
        return None, DataParseError(
            lineno,
            t("data.error.format", lang),
        )
    parts = [p.strip() for p in s.split(delimiter)]
    if len(parts) < 2 or not parts[0] or not parts[1]:
        return None, DataParseError(
            lineno,
            t("data.error.required", lang),
        )

    handle = _norm_handle(parts[0])
    wallet = _norm_wallet(parts[1])
    if not handle:
        return None, DataParseError(
            lineno,
            t("data.error.handle", lang),
        )
    if not wallet:
        return None, DataParseError(lineno, t("data.error.wallet", lang))
    return DataRow(handle=handle, wallet=wallet), None


def parse_data_text(text: str, *, lang: str = "ru") -> DataParseResult:
    rows: list[DataRow] = []
    errors: list[DataParseError] = []
    raw_lines = 0
    seen_wallets: set[str] = set()
    max_lines = int(getattr(config, "USER_DATA_MAX_LINES", 1000) or 1000)

    for i, line in enumerate((text or "").splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        raw_lines += 1
        if raw_lines > max_lines:
            errors.append(
                DataParseError(i, t("data.error.too_many", lang, max=max_lines))
            )
            break
        row, err = parse_data_line(line, i, lang=lang)
        if err:
            errors.append(err)
            continue
        if not row:
            continue
        if row.wallet in seen_wallets:
            errors.append(
                DataParseError(
                    i,
                    t("data.error.duplicate", lang, wallet=row.wallet[:12]),
                )
            )
            continue
        seen_wallets.add(row.wallet)
        rows.append(row)

    if raw_lines == 0 and not errors:
        errors.append(DataParseError(0, t("data.error.no_rows", lang)))
    return DataParseResult(rows=rows, errors=errors, raw_lines=raw_lines)


def save_user_data_from_bytes(
    user_id: int,
    data: bytes,
    *,
    lang: str = "ru",
) -> tuple[bool, str, int]:
    if not data:
        return False, t("data.error.empty_file", lang), 0
    max_bytes = int(getattr(config, "USER_DATA_MAX_BYTES", 512 * 1024) or 512 * 1024)
    if len(data) > max_bytes:
        return False, t("data.error.too_big", lang, kb=max_bytes // 1024), 0

    text = None
    for enc in ("utf-8-sig", "utf-8", "cp1251", "latin-1"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        return False, t("data.error.encoding", lang), 0

    parsed = parse_data_text(text, lang=lang)
    if not parsed.ok:
        lines = [t("data.error.rejected", lang), ""]
        for e in parsed.errors[:25]:
            if e.lineno:
                lines.append(f"• стр. {e.lineno}: {e.message}")
            else:
                lines.append(f"• {e.message}")
        if len(parsed.errors) > 25:
            lines.append(t("data.error.more", lang, n=len(parsed.errors) - 25))
        return False, "\n".join(lines), 0

    path = user_data_path(user_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    out = [f"{r.handle}|{r.wallet}" for r in parsed.rows]
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    tmp.replace(path)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    invalidate_rows_cache(user_id)
    return True, t("data.saved", lang, n=len(parsed.rows)), len(parsed.rows)
