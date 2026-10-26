"""Smoke tests for i18n + http_client after CLI removal."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="i18n_http_test_"))
_DB = _TMP / "test.db"
os.environ["BOT_DB"] = str(_DB)
os.environ["BOT_TOKEN"] = "test:token"

for mod in list(sys.modules):
    if mod == "bot" or mod.startswith("bot.") or mod == "handlers" or mod.startswith("handlers."):
        del sys.modules[mod]

from bot import config  # noqa: E402
from bot import db as bot_db  # noqa: E402
from bot import i18n  # noqa: E402
from bot.http_client import (  # noqa: E402
    is_gas_lock_message,
    is_valid_wallet,
    load_proxies,
    normalize_twitter,
)

config.DB_PATH = _DB
bot_db.DB_PATH = _DB  # type: ignore[attr-defined]


def _assert(cond: bool, msg: str = "") -> None:
    if not cond:
        raise AssertionError(msg or "assert failed")


def test_i18n_keys() -> None:
    _assert(i18n.t("welcome.pick", "ru") == "Выбери проект:")
    _assert("project" in i18n.t("welcome.pick", "en").lower())
    _assert("Accepted" in i18n.t("result.ok", "en", name="P", handle="h", wallet="0x", left=1, limit=10))
    reason = i18n.friendly_bad_reason("duplicate wallet already registered", "ru")
    _assert("whitelist" in reason.lower() or "уже" in reason.lower(), reason)
    _assert(i18n.short_wallet("0x" + "a" * 40).startswith("0xaaaa"))
    # The key hand-off refusal must read as "wait", not as an error, in both
    # languages — it is shown while the user's own job is still running.
    for lang in ("ru", "en"):
        busy = i18n.t("toast.keys_busy", lang)
        _assert(busy != "toast.keys_busy", f"missing toast.keys_busy [{lang}]")
        _assert("keys" in busy.lower() or "ключ" in busy.lower(), busy)


def test_lang_db() -> None:
    bot_db.init_db()
    bot_db.ensure_user(42, "u", "U")
    _assert(bot_db.get_lang(42) == "ru")
    bot_db.set_lang(42, "en")
    _assert(bot_db.get_lang(42) == "en")
    bot_db.set_lang(42, "RU")
    _assert(bot_db.get_lang(42) == "ru")


def test_http_client_basics() -> None:
    _assert(is_valid_wallet("0x" + "ab" * 20))
    _assert(not is_valid_wallet("0x" + "ab" * 32))  # 64 hex = tx hash
    _assert(normalize_twitter("@Hello_World") == "Hello_World")
    _assert(is_gas_lock_message("Exception: Lock timeout after 30 seconds"))
    # empty missing file
    p = _TMP / "empty_proxy.txt"
    p.write_text("")
    _assert(load_proxies(str(p)) == [])


def test_accept_encoding_never_exceeds_what_we_can_decode() -> None:
    """Advertising br/zstd without a decoder returns unreadable bytes."""
    from bot import http_client as hc

    decodable = set(hc.DECODABLE_ENCODINGS)
    _assert("gzip" in decodable and "deflate" in decodable, "gzip/deflate are built in")
    for variant in hc.ACCEPT_ENCODINGS:
        offered = {part.strip() for part in variant.split(",") if part.strip()}
        _assert(
            offered <= decodable,
            f"offered {offered - decodable} without a decoder ({variant})",
        )
    _assert(hc.DEFAULT_ACCEPT_ENCODING in hc.ACCEPT_ENCODINGS, "default is one of them")

    # Every fingerprint, not just the random pool, must obey this.
    for _ in range(40):
        headers, _ua = hc.random_fingerprint("https://example.com", "https://example.com/")
        enc = headers.get("Accept-Encoding", "")
        offered = {part.strip() for part in enc.split(",") if part.strip()}
        _assert(offered <= decodable, f"fingerprint offered {offered - decodable}")


def test_sec_fetch_site_matches_the_actual_target() -> None:
    """Claiming cross-site while sending the site's own Origin is a lie some
    backends reject (ZTH Academy: ORIGIN_NOT_ALLOWED)."""
    from bot import http_client as hc

    _assert(hc._same_origin("https://a.io", "https://a.io/api/x"), "same host+scheme")
    _assert(not hc._same_origin("https://a.io", "https://api.a.io/x"), "subdomain differs")
    _assert(not hc._same_origin("https://a.io", "http://a.io/x"), "scheme differs")
    _assert(not hc._same_origin("https://a.io", None), "no target, no claim")
    _assert(not hc._same_origin(None, "https://a.io/x"), "no origin, no claim")

    same, cross, plain = set(), set(), set()
    for _ in range(60):
        h, _ua = hc.random_fingerprint("https://a.io", "https://a.io/", url="https://a.io/api/x")
        same.add(h.get("sec-fetch-site", "<absent>"))
        h2, _ = hc.random_fingerprint("https://a.io", "https://a.io/", url="https://gas.google.com/x")
        cross.add(h2.get("sec-fetch-site", "<absent>"))
        h3, _ = hc.random_fingerprint("https://a.io", "https://a.io/")
        plain.add(h3.get("sec-fetch-site", "<absent>"))
    _assert(same <= {"same-origin", "<absent>"}, f"same-origin target: {same}")
    _assert(cross <= {"cross-site", "<absent>"}, f"cross-site target: {cross}")
    _assert(plain <= {"cross-site", "<absent>"}, f"no url given keeps the old default: {plain}")


def test_own_origin_projects_declare_same_origin() -> None:
    """Every adapter that posts to its own site must pass url= (see ZTH)."""
    from bot import http_client as hc

    cases = {
        # a site that posts to api.example-coin.fun from form.example-coin.fun — cross-site
        # on purpose. Same-origin adapters are gone with the last season.
    }
    import importlib

    for name, (mod_name, origin_attr, url_attr) in cases.items():
        mod = importlib.import_module(mod_name)
        origin, url = getattr(mod, origin_attr), getattr(mod, url_attr)
        _assert(hc._same_origin(origin, url), f"{name}: {origin} vs {url} should be same-origin")


def test_brotli_decoder_is_installed() -> None:
    """requirements.txt pins it; a missing decoder silently breaks every project."""
    from urllib3.response import HAS_ZSTD

    from bot import http_client as hc

    try:
        import brotli  # noqa: F401

        have_br = True
    except ImportError:
        try:
            import brotlicffi  # noqa: F401

            have_br = True
        except ImportError:
            have_br = False
    _assert(have_br, "brotli is a hard dependency (see requirements.txt)")
    _assert("br" in hc.DECODABLE_ENCODINGS, "…and must be advertised once present")
    _assert(
        ("zstd" in hc.DECODABLE_ENCODINGS) == bool(HAS_ZSTD),
        "zstd is offered only when urllib3 can actually decode it",
    )


class _FakeResponse:
    def __init__(self, text: str, status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code


class _RecordingSession:
    """Captures what an adapter would put on the wire."""

    calls: list[dict] = []

    def __init__(self) -> None:
        self.headers: dict = {}

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def _record(self, verb, url, kwargs):
        merged = dict(self.headers)
        merged.update(kwargs.get("headers") or {})
        _RecordingSession.calls.append(
            {"verb": verb, "url": url, "params": kwargs.get("params") or {},
             "headers": merged}
        )

    def get(self, url, **kwargs):
        self._record("get", url, kwargs)
        return _FakeResponse('<input type="hidden" name="fbzx" value="123">')

    def post(self, url, **kwargs):
        self._record("post", url, kwargs)
        return _FakeResponse("Your response has been recorded")


def test_google_forms_adapters_pin_english() -> None:
    """Forms localizes its confirmation page by Accept-Language, and the random
    fingerprint picks a non-English locale ~40% of the time. An adapter that
    lets that through reads "Deine Antwort wurde erfasst" as a failure and then
    re-submits the (already recorded) response on every retry."""
    import importlib

    from bot import http_client as hc

    non_english = [a for a in hc.ACCEPT_LANGUAGES if not a.lower().startswith("en")]
    _assert(non_english, "the fingerprint pool still carries non-English locales")

    for mod_name, entry in (
    ):
        mod = importlib.import_module(mod_name)
        markers = [m.lower() for m in mod.SUCCESS_MARKERS]
        _assert(
            all(m.isascii() for m in markers),
            f"{mod_name}: success markers are English, so the request must be too",
        )
        real_session = mod.requests.Session
        _RecordingSession.calls = []
        mod.requests.Session = _RecordingSession
        try:
            # Enough attempts that a leaked random locale would show up.
            for _ in range(25):
                result = mod.submit(entry, ["http://user:pw@proxy.test:8080"], 0,
                                    comment_url="https://x.com/a/status/1",
                                    timezone="UTC+0", retries=1)
                _assert(result["status"] == "OK", f"{mod_name}: {result}")
        finally:
            mod.requests.Session = real_session

        _assert(_RecordingSession.calls, f"{mod_name} sent nothing")
        for call in _RecordingSession.calls:
            lang = call["headers"].get("Accept-Language", "")
            _assert(
                lang.lower().startswith("en"),
                f"{mod_name} {call['verb']} leaked Accept-Language {lang!r}",
            )
            _assert(
                call["params"].get("hl") == "en",
                f"{mod_name} {call['verb']} did not pass hl=en",
            )


def test_projects_no_cli_imports() -> None:
    import re

    from bot.registry import load_projects, list_projects

    load_projects()
    ids = {p.id for p in list_projects()}
    # Enabled set (AGENTS.md / PROJECTS.md) — not hardcoded in tgbot
    for need in (
        "example",
    ):
        _assert(need in ids, f"missing enabled project {need}")
    # The disabled starter must not appear in the registry list.
    for dis in ("_template",):
        _assert(dis not in ids, f"should be disabled/removed: {dis}")
    # Removed projects leave nothing behind on disk. Every folder that is left
    # must be a live one: the tree carries no disabled leftovers any more.
    projects_dir = ROOT / "projects"
    on_disk = {
        d.name for d in projects_dir.iterdir()
        if d.is_dir() and not d.name.startswith(("_", ".")) and (d / "project.toml").is_file()
    }
    # A folder outside the registry is allowed only when its toml says
    # `enabled = false` on purpose — a project whose site died but whose adapter
    # is worth keeping. What must never happen is a silent leftover: a folder
    # the registry skips for any other reason.
    for extra in sorted(on_disk - ids):
        toml = (projects_dir / extra / "project.toml").read_text(encoding="utf-8")
        _assert(
            re.search(r"^enabled\s*=\s*false", toml, re.M) is not None,
            f"folder {extra} is not in the registry and is not explicitly disabled",
        )
    _assert(not (ids - on_disk), f"registry lists a project with no folder: {ids - on_disk}")

    from bot.http_client import is_valid_wallet

    _assert(is_valid_wallet("0x" + "1" * 40), "40-hex wallet valid")
    # A 64-char tx hash must not pass as a wallet anywhere (AGENTS pitfalls).
    _assert(not is_valid_wallet("0x" + "a" * 64), "tx hash must not pass")


def main() -> None:
    tests = [
        test_i18n_keys,
        test_lang_db,
        test_http_client_basics,
        test_accept_encoding_never_exceeds_what_we_can_decode,
        test_brotli_decoder_is_installed,
        test_sec_fetch_site_matches_the_actual_target,
        test_own_origin_projects_declare_same_origin,
        test_google_forms_adapters_pin_english,
        test_projects_no_cli_imports,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
    if failed:
        print(f"FAILED {failed}/{len(tests)}")
        sys.exit(1)
    print(f"ALL {len(tests)} PASSED")
    sys.exit(0)


if __name__ == "__main__":
    main()
