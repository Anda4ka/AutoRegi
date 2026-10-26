"""Suggest→draft→probe pipeline: gates first, network never."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import config, suggest_pipeline as sp  # noqa: E402
from bot import db as bot_db  # noqa: E402

bot_db.init_db()


# --- pure helpers ---------------------------------------------------------------

def test_extract_url_first_link_only() -> None:
    body = "глянь https://example.xyz/apply, там форма"
    assert sp.extract_url(body) == "https://example.xyz/apply"
    assert sp.extract_url("без ссылки") == ""


def test_extract_url_bare_domain_gets_https() -> None:
    # Suggestion #61 was refused as «нет URL» because the user typed the
    # domain without a scheme. A bare domain with a known TLD is a link.
    assert sp.extract_url("Some Project\n\nsomeproject.xyz") == "https://someproject.xyz"
    assert sp.extract_url("www.example.com/path") == "https://www.example.com/path"
    # Not links: Cyrillic sentences, handles, e-mails
    assert sp.extract_url("Привет. Хочу в проект.") == ""
    assert sp.extract_url("мой логин vasya.petrov") == ""
    assert sp.extract_url("пишите на mail@example.com") == ""


def test_project_id_for_collisions_and_sanitising(tmp_path: Path) -> None:
    # a taken id or an existing folder must refuse, never silently overwrite
    assert sp.project_id_for("https://demoh.example/", {"demoh"}) == ""
    assert sp.project_id_for("https://zz-probe.test/whitelist", set()) == "zzprobe"
    # leading-domain dashes drop out; what is left must still be [a-z0-9_]{2,20}
    assert sp.project_id_for("https://xn--123.biz/", set()) == "xn123"
    assert sp.project_id_for("https://a.io/", set()) == ""  # too short after sanitising


def test_check_quotes_substring_and_squash() -> None:
    sources = {"page": "fetch( \"/api/apply\", {method:\"POST\"})"}
    ok = sp.check_quotes(['fetch("/api/apply", {method:"POST"})'], sources)
    assert ok, "quoted text with different whitespace must still pass"
    assert sp.check_quotes(["этой строки на странице нет"], sources) == []
    assert sp.check_quotes(["short"], sources) == []  # <12 chars is not evidence


# --- quote gate on the LLM verdict ----------------------------------------------

def _patch_llm(monkeypatch: pytest.MonkeyPatch, payload: str) -> None:
    from bot import llm_client

    monkeypatch.setattr(llm_client, "has_llm", lambda: True)
    monkeypatch.setattr(llm_client, "chat_completion", lambda **_: payload)


def test_llm_verdict_accepts_quoted(monkeypatch: pytest.MonkeyPatch) -> None:
    sources = {"p": 'await fetch("/api/whitelist",{method:"POST"})'}
    _patch_llm(monkeypatch, json.dumps({
        "verdict": "form",
        "reason": "страница шлёт POST на /api/whitelist",
        "quotes": ['fetch("/api/whitelist",{method:"POST"})'],
    }))
    v = sp.llm_verdict(sources, {})
    assert v["verdict"] == "form"
    assert v["quotes_ok"] == 1


def test_llm_verdict_fabricated_quote_becomes_unclear(monkeypatch: pytest.MonkeyPatch) -> None:
    sources = {"p": "ничего похожего здесь нет"}
    _patch_llm(monkeypatch, json.dumps({
        "verdict": "form",
        "reason": "верю на слово",
        "quotes": ["POST /api/whitelist принимает хендл и кошелёк"],
    }))
    v = sp.llm_verdict(sources, {})
    # A claim with no substring behind it must never read as evidence.
    assert v["verdict"] == "unclear"
    assert v["quotes_ok"] == 0


def test_llm_verdict_unparseable_is_unclear(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_llm(monkeypatch, "модель ушла в рассуждения без JSON")
    v = sp.llm_verdict({}, {})
    assert v["verdict"] == "unclear"


def test_llm_verdict_skipped_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from bot import llm_client

    monkeypatch.setattr(llm_client, "has_llm", lambda: False)
    v = sp.llm_verdict({}, {})
    assert v["verdict"] == "skipped" and not v["configured"]


def _fb_page(marker18: object, email: object = 0) -> str:
    import json as _json

    data = [[], None, None, None, None, None, None, None, "Form", None, [None, email]]
    while len(data) <= 18:
        data.append(None)
    data[18] = marker18
    return "var FB_PUBLIC_LOAD_DATA_ = " + _json.dumps(data) + ";"


class _Resp401:
    status_code = 401
    text = ""


def test_google_form_login_marker_confirmed_by_401(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import requests

    posted: list[str] = []
    monkeypatch.setattr(
        requests, "post",
        lambda url, **kw: posted.append(url) or _Resp401(),
    )
    reason = sp.google_form_requires_login(
        "https://docs.google.com/forms/d/e/X/viewform",
        {"f": _fb_page(marker18=1)},
    )
    assert reason and "401" in reason
    assert posted and posted[0].endswith("/formResponse")


def test_google_form_marker_lies_probe_answers_like_working(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import requests

    class _Resp200:
        status_code = 200
        text = ""

    monkeypatch.setattr(requests, "post", lambda url, **kw: _Resp200())
    reason = sp.google_form_requires_login(
        "https://docs.google.com/forms/d/e/X/viewform",
        {"f": _fb_page(marker18=1)},
    )
    # A marker is a suspicion; a 2xx answer outranks it.
    assert reason is None


def test_google_form_working_markers_no_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import requests

    called = []
    monkeypatch.setattr(requests, "post",
                        lambda url, **kw: called.append(url) or _Resp401())
    reason = sp.google_form_requires_login(
        "https://docs.google.com/forms/d/e/X/viewform",
        {"f": _fb_page(marker18=0, email=0)},
    )
    assert reason is None
    assert not called, "no marker ⇒ no POST: discovery must not create rows"


# --- end-to-end with every network edge stubbed ---------------------------------

class _FakeScaffold:
    def __init__(self, tmp: Path, sources: dict[str, str]) -> None:
        self._tmp = tmp
        self._sources = sources

    def gather(self, url: str):
        found = {"endpoints": {"own_api_relative": [{
            "url": "https://zzprobetest.example/api/whitelist",
            "found_in": url, "near": "",
        }]}, "captcha": {}, "fields": [], "form_entries": []}
        return found, self._sources, []

    def best_kind(self, found: dict) -> str:
        return "own_api_relative"

    def init_project(self, project_id: str, url: str, found: dict):
        folder = self._tmp / "projects" / project_id
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "__init__.py").touch()
        (folder / "project.toml").write_text("id = ...\n", encoding="utf-8")
        return folder, ["дописать руками"]


class _FakeProject:
    id = "zzprobetest"
    name = "Zzprobetest"

    def parse(self, text: str):
        handle, wallet = text.split("|")
        return {"handle": handle, "wallet": wallet}

    def validate(self, fields: dict):
        return None

    def submit(self, fields, **_):
        return {"status": "OK", "detail": "{\"ok\":true}",
                "handle": fields["handle"], "wallet": fields["wallet"]}


def test_process_suggestion_tested_end_to_end(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sug = {"id": 9001, "user_id": 42, "username": "tester",
           "first_name": "T", "body": "возьмите https://zzprobetest.example/whitelist"}

    monkeypatch.setattr(sp, "ROOT", tmp_path)
    monkeypatch.setattr(sp, "probe_rows", lambda: 3)
    monkeypatch.setattr(sp, "_probe_accounts",
                        lambda n: [(f"h{i}", f"0x{'a':0>39}{i}") for i in range(n)])
    fake = _FakeScaffold(tmp_path, {"page": "fetch(\"/api/whitelist\")"})
    monkeypatch.setattr(sp, "load_scaffold", lambda: fake)
    monkeypatch.setattr(sp, "load_project_folder", lambda pid: _FakeProject())
    monkeypatch.setattr(sp, "llm_verdict",
                        lambda s, e: {"verdict": "form", "reason": "ок",
                                      "quotes_ok": 1, "quotes_total": 1,
                                      "configured": True})
    import bot.http_client as hc

    monkeypatch.setattr(hc, "load_proxies", lambda _p: ["http://u:p@1.2.3.4:8080"])
    monkeypatch.setattr(sp, "load_proxies", hc.load_proxies)
    from bot import db as bot_db

    recorded: list[tuple] = []
    monkeypatch.setattr(sp.bot_db, "record_submission",
                        lambda *a, **k: recorded.append(a) or None)

    sent: list[tuple[int, str]] = []
    sp.process_suggestion(sug, lambda aid, text: sent.append((aid, text)))

    # test_i18n_http purges bot.* at import; the pipeline holds the original
    # bot.db module, so ask through it — a fresh `from bot import db` here
    # would point at that file's temp DB and read a different database.
    with sp.bot_db.db() as conn:
        row = conn.execute(
            "SELECT status, report FROM suggest_pipeline_runs WHERE suggestion_id=?",
            (9001,),
        ).fetchone()
    assert row and row[0] == "tested", row[1] if row else "no row"
    assert "ПРОБА ПРОШЛА" in row[1]
    assert recorded, "settled probe rows must be recorded for dedup"
    assert sent and config.ADMIN_IDS or not sent  # dossier only if admins exist


def test_x_post_url_refused_before_recon(tmp_path: Path) -> None:
    # suggestion #56 live: an x.com status killed the sweep with an error
    # dossier; a post on X must read as a refusal, not a crash.
    sug = {"id": 9056, "user_id": 1, "username": None, "first_name": "R",
           "body": "https://x.com/PixelRealmsNfts/status/2095954929064308771"}
    import bot.suggest_pipeline as spx

    spx.process_suggestion(sug, lambda *a: None)
    with spx.bot_db.db() as conn:
        row = conn.execute(
            "SELECT status, report FROM suggest_pipeline_runs WHERE suggestion_id=?",
            (9056,),
        ).fetchone()
    assert row and row[0] == "refused"
    assert "X" in row[1]


def test_known_domain_refused_before_recon(tmp_path: Path) -> None:
    # The duplicate gate must fire before gather: an already-served project
    # must not cost a scout sweep (suggestions #58/#59 paid it, 2026-09-08).
    sug = {"id": 9058, "user_id": 1, "username": None, "first_name": "T",
               "body": "https://example.com"}
    import bot.suggest_pipeline as spx

    spx.process_suggestion(sug, lambda *a: None)
    with spx.bot_db.db() as conn:
        row = conn.execute(
            "SELECT status, report FROM suggest_pipeline_runs WHERE suggestion_id=?",
            (9058,),
        ).fetchone()
    assert row and row[0] == "refused"
    assert "уже в боте" in row[1]


def test_process_suggestion_captcha_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sug = {"id": 9002, "user_id": 42, "username": None, "first_name": "T",
           "body": "https://zzprobetest.example/apply"}
    monkeypatch.setattr(sp, "ROOT", tmp_path)

    class _CapScaffold(_FakeScaffold):
        def gather(self, url: str):
            found = {"endpoints": {}, "captcha": {"turnstile": {"key": "0x4AAA…"}},
                     "fields": [], "form_entries": []}
            return found, {"p": "x"}, []

    monkeypatch.setattr(sp, "load_scaffold", lambda: _CapScaffold(tmp_path, {}))
    sp.process_suggestion(sug, lambda *a: None)
    with sp.bot_db.db() as conn:
        row = conn.execute(
            "SELECT status, report FROM suggest_pipeline_runs WHERE suggestion_id=?",
            (9002,),
        ).fetchone()
    assert row and row[0] == "refused"
    assert "капча" in row[1]
