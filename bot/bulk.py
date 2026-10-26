"""Bulk .txt import for VIP users (universal column format).

Universal line format (one record per line):
  handle|wallet[|field3][|field4]...

Separator: ``|`` preferred. Comma ``,`` allowed only when the line has no ``|``
(so URLs stay intact). Empty lines and ``#`` comments are skipped.

Each project declares ``bulk_columns``: ordered (key, required, label).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from bot import config
from bot.i18n import t
from bot.ux import esc

# (field_key, required, human label for errors)
BulkColumn = tuple[str, bool, str]


@dataclass
class LineResult:
    lineno: int
    raw: str
    fields: dict | None = None
    error: str | None = None  # plain text / HTML-safe fragments already escaped by formatter


@dataclass
class FileParseResult:
    lines: list[LineResult] = field(default_factory=list)
    skipped_blank: int = 0
    file_error: str | None = None

    @property
    def valid(self) -> list[LineResult]:
        return [x for x in self.lines if x.fields is not None and not x.error]

    @property
    def invalid(self) -> list[LineResult]:
        return [x for x in self.lines if x.error]


def split_bulk_line(line: str) -> list[str]:
    """Split one data line into columns (strip, drop empties only at ends via strip each)."""
    s = (line or "").strip()
    if not s:
        return []
    if "|" in s:
        parts = [p.strip() for p in s.split("|")]
    else:
        parts = [p.strip() for p in s.split(",")]
    # Keep internal empty tokens as errors (wrong column count); trim only whitespace
    return parts


def parts_to_fields(
    parts: list[str],
    columns: Sequence[BulkColumn],
    *,
    lang: str = "ru",
) -> tuple[dict | None, str | None]:
    """Map split parts → fields dict using project bulk_columns schema."""
    if not columns:
        return None, t("bulk.error.no_schema", lang)
    if not parts:
        return None, t("bulk.error.empty_line", lang)

    required = [(k, lab) for k, req, lab in columns if req]
    min_n = len(required)
    max_n = len(columns)

    if len(parts) < min_n:
        missing_labels = [lab for _, lab in required[len(parts) :]]
        need = " | ".join(lab for _, _, lab in columns if True)
        return (
            None,
            t(
                "bulk.error.too_few",
                lang,
                min=min_n,
                got=len(parts),
                missing=", ".join(missing_labels) or "—",
                order=esc(need),
            ),
        )

    # AUD-010: reject extra columns (do not silent-truncate).
    # Projects that accept free-form multi-URL lines should use module.parse
    # fallback in parse_bulk_text, not positional truncation.
    if len(parts) > max_n:
        need = " | ".join(lab for _, _, lab in columns)
        return (
            None,
            t(
                "bulk.error.extra",
                lang,
                got=len(parts),
                max=max_n,
                order=esc(need),
            ),
        )

    # Reject empty required cells
    fields: dict[str, Any] = {}
    for i, (key, req, label) in enumerate(columns):
        if i >= len(parts):
            if req:
                return None, t(
                    "bulk.error.missing",
                    lang,
                    label=esc(label),
                    col=i + 1,
                )
            continue
        val = parts[i].strip()
        if not val:
            if req:
                return (
                    None,
                    t(
                        "bulk.error.empty",
                        lang,
                        label=esc(label),
                        col=i + 1,
                    ),
                )
            continue
        # strip @ on handle-like keys
        if key in ("handle", "twitter"):
            val = val.lstrip("@")
        fields[key] = val

    return fields, None


def decode_txt_bytes(
    data: bytes,
    *,
    max_bytes: int | None = None,
    lang: str = "ru",
) -> tuple[str | None, str | None]:
    """Decode file bytes → text. Returns (text, error).

    max_bytes: tier cap (VIP may pass a higher limit). Default: config.BULK_MAX_BYTES.
    """
    if not data:
        return None, t("bulk.error.file_empty", lang)
    cap = int(max_bytes) if max_bytes is not None else int(config.BULK_MAX_BYTES)
    if cap < 1:
        cap = int(config.BULK_MAX_BYTES)
    if len(data) > cap:
        kb = max(1, cap // 1024)
        return None, t("bulk.error.too_big", lang, kb=kb)
    for enc in ("utf-8-sig", "utf-8", "cp1251", "latin-1"):
        try:
            return data.decode(enc), None
        except UnicodeDecodeError:
            continue
    return None, t("bulk.error.encoding", lang)


def parse_bulk_text(
    text: str,
    *,
    columns: Sequence[BulkColumn],
    validate: Callable[[dict], str | None],
    max_lines: int | None = None,
    line_parse: Callable[[str], dict | None] | None = None,
    allow_line_parse: bool = False,
    lang: str = "ru",
) -> FileParseResult:
    """Parse full file text into per-line results (does not submit).

    Positional bulk_columns first (authoritative). Optional ``line_parse``
    fallback (module.parse) only when ``allow_line_parse=True`` (AUD-011).
    """
    out = FileParseResult()
    max_lines = max_lines if max_lines is not None else config.BULK_MAX_LINES
    raw_lines = (text or "").splitlines()
    data_line_count = 0

    for i, raw in enumerate(raw_lines, start=1):
        stripped = raw.strip()
        if not stripped:
            out.skipped_blank += 1
            continue
        if stripped.startswith("#"):
            out.skipped_blank += 1
            continue

        data_line_count += 1
        if data_line_count > max_lines:
            out.lines.append(
                LineResult(
                    lineno=i,
                    raw=stripped[:200],
                    error=(
                        t("bulk.error.line_limit", lang, max=max_lines)
                    ),
                )
            )
            # mark rest similarly once
            remaining = sum(
                1
                for r in raw_lines[i:]
                if r.strip() and not r.strip().startswith("#")
            )
            if remaining > 1:
                out.lines.append(
                    LineResult(
                        lineno=i + 1,
                        raw="…",
                        error=t("bulk.error.lines_skipped", lang, n=remaining - 1),
                    )
                )
            break

        parts = split_bulk_line(stripped)
        fields, err = parts_to_fields(parts, columns, lang=lang)
        v_err: str | None = None
        if fields is not None and not err:
            v_err = validate(fields)

        # Opt-in free-form fallback (legacy / multi-URL datalink projects)
        if allow_line_parse and (err or v_err) and line_parse is not None:
            try:
                parsed = line_parse(stripped)
            except Exception:
                parsed = None
            if isinstance(parsed, dict) and parsed:
                v2 = validate(parsed)
                if not v2:
                    fields, err, v_err = parsed, None, None

        if err:
            out.lines.append(LineResult(lineno=i, raw=stripped[:200], error=err))
            continue
        if v_err:
            out.lines.append(
                LineResult(
                    lineno=i,
                    raw=stripped[:200],
                    error=t("bulk.error.validation", lang, error=v_err),
                )
            )
            continue
        assert fields is not None
        out.lines.append(LineResult(lineno=i, raw=stripped[:200], fields=fields))

    if data_line_count == 0 and not out.file_error:
        out.file_error = t("bulk.error.no_data", lang)
    return out


def bulk_format_help(
    columns: Sequence[BulkColumn],
    project_name: str,
    *,
    lang: str = "ru",
) -> str:
    """One-line VIP hint for project screen (format already shown above)."""
    del columns, project_name  # same format as text; no extra schema dump
    return t("bulk.help", lang)


def format_line_errors(
    invalid: list[LineResult],
    *,
    limit: int = 25,
    lang: str = "ru",
) -> str:
    if not invalid:
        return ""
    chunks = [t("bulk.errors_file_title", lang), ""]
    for item in invalid[:limit]:
        preview = esc(item.raw[:80] + ("…" if len(item.raw) > 80 else ""))
        chunks.append(
            t("bulk.error_line", lang, line=item.lineno, preview=preview)
        )
        chunks.append(f"  → {esc(item.error)}")
    if len(invalid) > limit:
        chunks.append(t("bulk.more_errors", lang, n=len(invalid) - limit))
    return "\n".join(chunks)


def chunk_html_messages(text: str, limit: int = 3900) -> list[str]:
    """Split long HTML reports for Telegram 4096 limit (rough, by lines)."""
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    buf: list[str] = []
    size = 0
    for line in text.split("\n"):
        add = len(line) + 1
        if buf and size + add > limit:
            parts.append("\n".join(buf))
            buf = [line]
            size = add
        else:
            buf.append(line)
            size += add
    if buf:
        parts.append("\n".join(buf))
    return parts
