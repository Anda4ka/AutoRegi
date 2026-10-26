"""Google Form generator (/gform, variant A) — parse, mapping gates, build, runtime.

No network: fetch/parse run on fixture HTML, submit on a fake session.
"""

from __future__ import annotations

import json
import sys

import pytest

from bot import gform

URL = "https://docs.google.com/forms/d/e/1FAIpQLSdTest123456789/viewform"


def _fb_html(fields_spec, *, email=0, login=0):
    """Build a viewform page around a synthetic FB_PUBLIC_LOAD_DATA_.

    Real shape (measured on a live form, 2026-09-10): data[1][1] =
    [[poster, title, null, type, [[entry, options, required], ...]], ...];
    the POST field name is entry.<entry>.
    """
    items = []
    for entry, title, options, required, qtype in fields_spec:
        opts = [[o] for o in options] or None
        items.append([900000000 + entry % 1000, title, None, qtype,
                      [[entry, opts, 1 if required else 0]]])
    data = [
        None,
        [None, items, None, None, None, None, None, None, "Test WL Form", 75,
         [None, email, None, 2, None, None, 1]],
    ]
    # login marker lives at top-level index 18
    while len(data) < 19:
        data.append(None)
    if login:
        data[18] = 1
    return (
        "<html><head><script>var FB_PUBLIC_LOAD_DATA_ = "
        + json.dumps(data)
        + ";</script></head><body></body></html>"
    )


FIELDS = [
    (111111111, "Your EVM / wallet address", [], True, 0),
    (222222222, "Your Twitter / X username", [], True, 0),
    (333333333, "Did you complete all tasks?", ["Yes", "No"], True, 2),
    (444444444, "Anything else?", [], False, 0),
]


@pytest.fixture()
def form():
    return gform.parse_form(URL, _fb_html(FIELDS))


def test_parse_fields(form):
    assert form["title"] == "Test WL Form"
    assert [f["entry"] for f in form["fields"]] == [
        "entry.111111111", "entry.222222222", "entry.333333333", "entry.444444444",
    ]
    radio = form["fields"][2]
    assert radio["options"] == ["Yes", "No"]
    assert radio["required"] is True
    assert form["fields"][3]["required"] is False


def test_refusals():
    # verified-email collection marker
    with pytest.raises(gform.GformError, match="email"):
        gform.parse_form(URL, _fb_html(FIELDS, email=1))
    # login marker [18]==1
    with pytest.raises(gform.GformError, match="вход"):
        gform.parse_form(URL, _fb_html(FIELDS, login=1))
    # captcha loader script on the page
    html = _fb_html(FIELDS).replace(
        "<head>", "<head><script src='https://www.google.com/recaptcha/api.js'></script>"
    )
    with pytest.raises(gform.GformError, match="капча"):
        gform.parse_form(URL, html)
    # not a google forms url
    with pytest.raises(gform.GformError):
        gform.parse_form("https://example.com/form", _fb_html(FIELDS))


def test_suggest_sources(form):
    assert gform.suggest_source(form["fields"][0]) == "wallet"
    assert gform.suggest_source(form["fields"][1]) == "handle"
    assert gform.suggest_source(form["fields"][2]) == "option:Yes"


def test_suggest_never_puts_text_source_on_choice_field():
    # "Retweet and comment wallet here" matched the comment hint and the
    # first version suggested comment_url for a radio field — a per-row 400
    # that also left the UI's option value empty and the create button dead.
    field = {"title": "Retweet and comment wallet here *",
             "options": ["Option 1"], "required": True}
    assert gform.suggest_source(field) == "option:Option 1"


def test_mapping_ok(form):
    mapping = gform.parse_mapping(
        "1=wallet\n2=handle\n3=option:Yes\n", form
    )
    assert mapping == {
        "entry.111111111": "wallet",
        "entry.222222222": "handle",
        "entry.333333333": "option:Yes",
    }


def test_mapping_gates(form):
    # required radio left unmapped
    with pytest.raises(gform.GformError, match="не замаплено"):
        gform.parse_mapping("1=wallet\n2=handle\n", form)
    # wallet never mapped → dedup/slots impossible
    with pytest.raises(gform.GformError, match="wallet"):
        gform.parse_mapping("1=handle\n2=handle\n3=option:Yes\n", form)
    # option value that is not one of the field's own options
    with pytest.raises(gform.GformError, match="нет среди вариантов"):
        gform.parse_mapping("1=wallet\n2=handle\n3=option:Maybe\n", form)
    # unknown source
    with pytest.raises(gform.GformError, match="не знаю"):
        gform.parse_mapping("1=wallet\n2=magic\n3=option:Yes\n", form)
    # unparsable line
    with pytest.raises(gform.GformError, match="не понята"):
        gform.parse_mapping("wallet everywhere\n1=wallet\n2=handle\n3=option:Yes\n", form)


@pytest.fixture()
def built(tmp_path, monkeypatch, form):
    monkeypatch.setattr(gform, "ROOT", tmp_path)
    mapping = gform.parse_mapping("1=wallet\n2=handle\n3=option:Yes\n", form)
    pid, path = gform.build_project(form, mapping, 222222222,
                                    root=tmp_path / "user_projects")
    yield pid, path
    for key in list(sys.modules):
        if key.startswith("projects_dyn."):
            del sys.modules[key]


def test_build_project_loads(built):
    pid, path = built
    assert path.exists()
    meta = (path / "project.toml").read_text(encoding="utf-8")
    assert "enabled = true" in meta
    assert "only_user_ids = 222222222" in meta
    # import gate already passed inside build_project; load once more
    from bot.registry import load_project_folder

    project = load_project_folder(pid, projects_dir=path.parent)
    assert project is not None
    assert project.id == pid
    fields = project.parse("myhandle|0x" + "ab" * 20)
    assert fields["handle"] == "myhandle"
    assert fields["wallet"].startswith("0x")
    assert project.validate(fields) is None
    # optional url column derived from the mapping (no quote/comment here)
    assert [c[0] for c in project.bulk_columns] == ["handle", "wallet"]


class _FakeResponse:
    def __init__(self, status_code=200, text="", url=""):
        self.status_code = status_code
        self.text = text
        self.url = url


class _FakeSession:
    def __init__(self, resp):
        self.resp = resp
        self.calls = []

    def post(self, url, **kw):
        self.calls.append((url, kw))
        return self.resp

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_submit_ok(monkeypatch, built):
    pid, path = built
    form_def = json.loads((path / "form.json").read_text(encoding="utf-8"))
    session = _FakeSession(_FakeResponse(200, "Your response has been recorded"))
    monkeypatch.setattr(gform.requests, "Session", lambda: session)
    res = gform.submit(
        form_def,
        {"handle": "myhandle", "wallet": "0x" + "ab" * 20},
        proxies=["http://u:p@1.2.3.4:8080"], proxy_idx=0,
        retries=1, min_delay=0, max_delay=0,
    )
    assert res["status"] == "OK"
    url, kw = session.calls[0]
    assert url.endswith("/formResponse")
    body = kw["data"]
    assert body["entry.111111111"].startswith("0x")
    assert body["entry.222222222"] == "myhandle"
    assert body["entry.333333333"] == "Yes"
    assert "entry.444444444" not in body
    assert body["fvv"] == "1"


def test_submit_closed(monkeypatch, built):
    pid, path = built
    form_def = json.loads((path / "form.json").read_text(encoding="utf-8"))
    session = _FakeSession(
        _FakeResponse(200, "This form is no longer accepting responses",
                      url="https://docs.google.com/forms/closedform")
    )
    monkeypatch.setattr(gform.requests, "Session", lambda: session)
    res = gform.submit(
        form_def,
        {"handle": "h", "wallet": "0x" + "ab" * 20},
        proxies=["http://u:p@1.2.3.4:8080"], proxy_idx=0,
        retries=1, min_delay=0, max_delay=0,
    )
    assert res["status"] == "BAD_RESP"
    assert "closed" in res["detail"]


def test_submit_login_401(monkeypatch, built):
    pid, path = built
    form_def = json.loads((path / "form.json").read_text(encoding="utf-8"))
    session = _FakeSession(_FakeResponse(401, "unable to open the file"))
    monkeypatch.setattr(gform.requests, "Session", lambda: session)
    res = gform.submit(
        form_def,
        {"handle": "h", "wallet": "0x" + "ab" * 20},
        proxies=["http://u:p@1.2.3.4:8080"], proxy_idx=0,
        retries=1, min_delay=0, max_delay=0,
    )
    assert res["status"] == "BAD_RESP"
    assert "401" in res["detail"]


def test_submit_login_401_disables_project(monkeypatch, built):
    """A login-walled form must runtime-disable itself and cancel its jobs.

    gftestform burned 700 rows into the 401 answer before anyone noticed
    (2026-09-24) — the adapter now acts instead of waiting for an admin.
    """
    pid, path = built
    form_def = json.loads((path / "form.json").read_text(encoding="utf-8"))
    form_def["pid"] = pid
    session = _FakeSession(_FakeResponse(401, "unable to open the file"))
    monkeypatch.setattr(gform.requests, "Session", lambda: session)
    calls = {}
    monkeypatch.setattr(
        "bot.db.set_project_runtime_disabled",
        lambda p, on, admin_id=None: calls.setdefault("disable", (p, on)),
    )
    monkeypatch.setattr(
        "bot.submit_queue.cancel_jobs_for_project",
        lambda p: calls.setdefault("cancel", p),
    )
    res = gform.submit(
        form_def,
        {"handle": "h", "wallet": "0x" + "ab" * 20},
        proxies=["http://u:p@1.2.3.4:8080"], proxy_idx=0,
        retries=1, min_delay=0, max_delay=0,
    )
    assert res["status"] == "BAD_RESP"
    assert "вход в Google" in res["detail"]
    assert calls["disable"] == (pid, True)
    assert calls["cancel"] == pid


def test_submit_no_proxies(built):
    pid, path = built
    form_def = json.loads((path / "form.json").read_text(encoding="utf-8"))
    res = gform.submit(
        form_def,
        {"handle": "h", "wallet": "0x" + "ab" * 20},
        proxies=[], proxy_idx=0, retries=1, min_delay=0, max_delay=0,
    )
    assert res["status"] == "FAIL"


# --- user flow: state dir, pin, publish, delete ---------------------------


@pytest.fixture()
def user_root(tmp_path, monkeypatch):
    from bot import config

    monkeypatch.setattr(config, "USER_PROJECTS_DIR", tmp_path / "user_projects")
    monkeypatch.setattr(gform, "ROOT", tmp_path)
    yield tmp_path / "user_projects"
    for key in list(sys.modules):
        if key.startswith("projects_dyn."):
            del sys.modules[key]
    # The registry is process-global: reload after the temp dir is gone so a
    # project from this test does not leak into the next one's listing.
    # monkeypatch is undone by hand first — its own teardown runs after this
    # one, so a reload here would still see the temp dir and cache the ghost.
    from bot.registry import load_projects

    monkeypatch.undo()
    load_projects(force=True)


def _build_user_form(user_root, form, owner=777, **kw):
    mapping = gform.validate_mapping(
        {"entry.111111111": "wallet", "entry.222222222": "handle",
         "entry.333333333": "option:Yes"}, form)
    return gform.build_project(form, mapping, owner, **kw)


def test_user_form_is_private_and_pinned(user_root, form):
    pid, path = _build_user_form(user_root, form)
    assert path.parent == user_root
    toml = (path / "project.toml").read_text(encoding="utf-8")
    assert "enabled = true" in toml
    assert "only_user_ids = 777" in toml
    d = json.loads((path / "form.json").read_text(encoding="utf-8"))
    assert d["owner"] == 777 and d["public"] is False
    # deterministic id: same owner+form collides loudly, no second fresh limit
    with pytest.raises(gform.GformError, match="уже существует"):
        _build_user_form(user_root, form)
    assert gform.project_id_for(form, 778) != pid


def test_set_public_drops_pin(user_root, form):
    pid, path = _build_user_form(user_root, form)
    d = gform.set_public(pid, True)
    assert d["public"] is True
    toml = (path / "project.toml").read_text(encoding="utf-8")
    assert "only_user_ids" not in toml
    d = gform.set_public(pid, False)
    assert "only_user_ids = 777" in (path / "project.toml").read_text(encoding="utf-8")


def test_delete_form_ownership(user_root, form):
    pid, path = _build_user_form(user_root, form)
    assert [f["id"] for f in gform.user_forms(777)] == [pid]
    cancelled = []
    gform.delete_form(777, pid, cancel_jobs=lambda p: cancelled.append(p))
    assert not path.exists() and cancelled == [pid]
    # someone else's form is not deletable, and unknown ids refuse
    pid2, _ = _build_user_form(user_root, gform.parse_form(
        URL.replace("1FAIpQLSdTest123456789", "1FAIpQLSdOther987654321"),
        _fb_html(FIELDS)))
    with pytest.raises(gform.GformError, match="не твоя"):
        gform.delete_form(778, pid2)
    with pytest.raises(gform.GformError, match="не найдена"):
        gform.delete_form(777, "gfnope123")


def test_custom_name_and_link(user_root, form):
    pid, path = _build_user_form(
        user_root, form,
        name="Arcilio Free Mint", link="https://x.com/arcilio")
    toml = (path / "project.toml").read_text(encoding="utf-8")
    assert 'name = "Arcilio Free Mint"' in toml
    # An x.com link becomes the X button, not the site button
    assert 'twitter_url = "https://x.com/arcilio"' in toml
    d = json.loads((path / "form.json").read_text(encoding="utf-8"))
    assert d["name"] == "Arcilio Free Mint"
    # and a site link lands in link
    pid2, path2 = _build_user_form(
        user_root, gform.parse_form(
            URL.replace("1FAIpQLSdTest123456789", "1FAIpQLSdOther987654321"),
            _fb_html(FIELDS)),
        name='Weird "quotes"\nand lines', link="not a url")
    toml2 = (path2 / "project.toml").read_text(encoding="utf-8")
    # quotes/newlines stripped out of the toml string, not written raw
    assert 'name = "Weird quotesand lines"' in toml2
    # junk link is dropped: no site button, form URL is not passed off as one
    assert 'link = ""' in toml2


def test_registry_scans_user_dir(user_root, form):
    from bot import registry

    pid, _ = _build_user_form(user_root, form)
    loaded = registry.load_projects(force=True)
    assert pid in loaded
    assert loaded[pid].only_user_ids == (777,)


def test_validate_mapping_dict_gates(form):
    good = gform.validate_mapping(
        {"entry.111111111": "wallet", "entry.222222222": "handle",
         "entry.333333333": "option:Yes"}, form)
    assert good["entry.111111111"] == "wallet"
    with pytest.raises(gform.GformError, match="wallet"):
        gform.validate_mapping({"entry.222222222": "handle",
                                "entry.333333333": "option:Yes"}, form)
    with pytest.raises(gform.GformError, match="не знаю"):
        gform.validate_mapping({"entry.111111111": "wallet",
                                "entry.222222222": "magic"}, form)


def test_text_sources_rejected_on_choice_fields(form):
    # A radio/checkbox field takes only one of its own variants — a wallet
    # there is a guaranteed 400 per row (measured: 145/145 «Отклонены»).
    with pytest.raises(gform.GformError, match="поле с вариантами"):
        gform.validate_mapping({"entry.333333333": "wallet",
                                "entry.111111111": "wallet",
                                "entry.222222222": "handle"}, form)
    with pytest.raises(gform.GformError, match="поле с вариантами"):
        gform.validate_mapping({"entry.111111111": "wallet",
                                "entry.222222222": "handle",
                                "entry.333333333": "static:Done"}, form)


def test_form_without_text_fields_refuses(form):
    # Only choice fields → nowhere to put a wallet → one clear refusal at
    # mapping time instead of a project whose every row dies.
    choice_only = dict(form)
    choice_only["fields"] = [f for f in form["fields"] if f["options"]]
    with pytest.raises(gform.GformError, match="нет ни одного текстового"):
        gform.validate_mapping(
            {f["entry"]: f"option:{f['options'][0]}" for f in choice_only["fields"]},
            choice_only)
