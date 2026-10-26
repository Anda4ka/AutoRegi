"""Own-proxy live check: cooldown, report formatting, no credentials leak."""

from __future__ import annotations

import sys
import types as pytypes

import pytest


@pytest.fixture()
def mod():
    sys.modules.pop("bot.proxy_check", None)
    import bot.proxy_check as proxy_check

    proxy_check._last_run.clear()
    return proxy_check


def test_cooldown_one_per_window(mod):
    ok, wait = mod.may_run(1)
    assert ok and wait == 0
    ok, wait = mod.may_run(1)
    assert not ok and 0 < wait <= mod.COOLDOWN_SEC
    # A different user is not blocked by someone else's check.
    assert mod.may_run(2)[0]


def test_report_masks_credentials_and_counts(mod):
    results = [
        {"label": "1.2.3.4:8080", "ok": True, "ip": "5.6.7.8", "ms": 321, "error": ""},
        {"label": "9.9.9.9:777", "ok": False, "ip": "", "ms": 5000, "error": "ProxyError"},
    ]
    text = mod.format_report(results, total=342)
    assert "живых 1 из 2" in text
    assert "первые 2 из 342" in text
    assert "1.2.3.4:8080" in text and "5.6.7.8" in text
    assert "ProxyError" in text
    # No url with credentials may ever reach the report.
    assert "@" not in text and "http" not in text


def test_report_no_prefix_when_whole_file_checked(mod):
    results = [{"label": "1.2.3.4:8080", "ok": True, "ip": "5.6.7.8", "ms": 10, "error": ""}]
    text = mod.format_report(results, total=1)
    assert "из 1" in text
    assert "строк файла" not in text


def test_label_strips_userinfo(mod):
    label = mod._label("http://user:secret@1.2.3.4:8080")
    assert label == "1.2.3.4:8080"
    assert "secret" not in label


def test_run_check_limit_and_mock(monkeypatch, mod):
    urls = [f"http://u:p@10.0.0.{i}:8000" for i in range(5)]

    class FakeResponse(pytypes.SimpleNamespace):
        status_code = 200
        text = "fl=abc\nip=8.8.8.8\n"

    monkeypatch.setattr(
        "requests.Session.get",
        lambda self, url, proxies=None, timeout=None: FakeResponse(),
    )
    results = mod.run_check(urls, limit=3)
    assert len(results) == 3
    assert all(r["ok"] and r["ip"] == "8.8.8.8" for r in results)
