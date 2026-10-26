"""Repo-wide guard that no adapter may ship a blank captcha.

The project-specific tests that used to share this file went with their
adapters when those forms closed (RobinHood Smokers 2026-08-19, Blastoons
2026-09-01). The echo-302 / sign-in-redirect / GAS HTML-error lessons live
on in the adapter history. What stays here is the guard, which is about
every adapter rather than any one of them.
"""

from __future__ import annotations


def test_no_adapter_sends_an_empty_captcha_field() -> None:
    """Guard for the whole repo: no project may ship a blank captcha again."""
    import pathlib
    import re

    offenders = []
    for path in pathlib.Path("projects").glob("*/api.py"):
        body = path.read_text(encoding="utf-8")
        for m in re.finditer(
            r'"(turnstile|captchaToken|captcha_token|g-recaptcha-response|h-captcha-response)"\s*:\s*""',
            body,
        ):
            offenders.append(f"{path}: {m.group(0)}")
    assert not offenders, "empty captcha token in: " + "; ".join(offenders)
