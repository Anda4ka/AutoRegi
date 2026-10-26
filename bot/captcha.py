"""Turnstile tokens for projects whose route actually verifies one.

Some whitelist forms render a Turnstile widget but never verify the token
server-side (one removed project did not until 2026-08-14), which used to tempt adapters
into sending an empty string. They must not: a form can accept a blank token and
drop those rows from the final list later. This module exists so every widget is
solved for real, and for the day a site starts checking, as that one did:

    POST /api/submit with captchaToken:""  →  403 {"error":"Verification failed"}

Usage from a project adapter::

    token, detail = captcha.solve_turnstile(PAGE_URL, TURNSTILE_SITEKEY)
    if not token:
        return {"status": "FAIL", "detail": f"captcha: {detail}"}

**Two providers, one call.** Capsolver and 2Captcha both speak the same
``createTask`` / ``getTaskResult`` envelope, so a second provider costs a table
row rather than a second client. `solve_turnstile` walks them in order and the
first token wins. That is not redundancy for its own sake: a solver account runs
out of money mid-run, and the failure that follows is indistinguishable from the
site refusing the row (2026-08-21, `etherbubu`: a $0.0004 Capsolver balance
stopped a run before a single request reached the form). A provider that answers
"no balance" or "bad key" is **remembered** for `DEAD_PROVIDER_TTL` seconds so
every later row goes straight to the one that works, and re-checked after that
so a top-up is picked up without a restart.

Order comes from ``CAPTCHA_PROVIDER_ORDER`` (comma-separated names); unknown and
unconfigured names are dropped.

Three deliberate choices:

* **Proxyless tasks.** Measured against a live Turnstile: a token solved on the provider's
  own IP is accepted when the submit goes through our proxy, so the site does
  not bind the token to the solver's address. Proxyless is several times cheaper
  and has no proxy to go stale mid-solve. A site that does bind the token needs
  the proxied task type — pass ``proxy_url`` and this switches.
* **A token is one-shot.** Cloudflare burns it on first verify, so a retry after
  a rejected submit must solve again; adapters must not cache one across rows.
* **A dead provider must not mask a live one's transient failure.** The detail
  returned names every provider only when *all* of them failed permanently —
  otherwise `is_captcha_solver_permanent_error` would read one account's empty
  balance and stop a row the other account could still have filed.

Every call is self-contained (no shared session), so row threads can call
it concurrently. No API key ever reaches a log line or an error detail.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from urllib.parse import urlparse

import requests

from bot import config
from bot.http_client import log

# Measured against a live Turnstile: ~4.4 s end to end. The cap is generous because a slow
# solve still beats a failed row, but it is bounded so a stuck task cannot pin
# a queue worker for minutes.
SOLVE_TIMEOUT = 120.0
POLL_INTERVAL = 2.0
HTTP_TIMEOUT = 30

# How long a provider that answered "no balance" / "bad key" is stepped over.
# Long enough that a run does not pay a round-trip per row to be told again,
# short enough that a top-up is noticed without restarting the bot.
DEAD_PROVIDER_TTL = 900.0


@dataclass(frozen=True)
class Provider:
    """One solver account. The envelope is shared; the dialect is not."""

    name: str
    base_url: str
    env_name: str
    task_proxyless: str
    task_proxied: str
    # Capsolver nests action/cdata under `metadata` and takes the proxy as one
    # URL string; 2Captcha puts them at the top level and splits the proxy into
    # five fields. Everything else about the two APIs is identical.
    dialect: str
    # reCAPTCHA v2 task names in the same dialect (Netlify Forms class). The
    # solved token arrives as solution.gRecaptchaResponse, not solution.token.
    recaptcha_task_proxyless: str = ""
    recaptcha_task_proxied: str = ""


PROVIDERS: tuple[Provider, ...] = (
    Provider(
        name="capsolver",
        base_url="https://api.capsolver.com",
        env_name="CAPSOLVER_API_KEY",
        task_proxyless="AntiTurnstileTaskProxyLess",
        task_proxied="AntiTurnstileTask",
        dialect="capsolver",
        recaptcha_task_proxyless="ReCaptchaV2TaskProxyLess",
        recaptcha_task_proxied="ReCaptchaV2Task",
    ),
    Provider(
        name="2captcha",
        base_url="https://api.2captcha.com",
        env_name="TWOCAPTCHA_API_KEY",
        task_proxyless="TurnstileTaskProxyless",
        task_proxied="TurnstileTask",
        dialect="twocaptcha",
        recaptcha_task_proxyless="RecaptchaV2TaskProxyless",
        recaptcha_task_proxied="RecaptchaV2Task",
    ),
    Provider(
        name="anticaptcha",
        base_url="https://api.anti-captcha.com",
        env_name="ANTICAPTCHA_API_KEY",
        task_proxyless="TurnstileTaskProxyless",
        task_proxied="TurnstileTask",
        # Same createTask/getTaskResult envelope; the Turnstile extras go at
        # the top level and the cData field keeps the provider's own casing.
        dialect="anticaptcha",
        recaptcha_task_proxyless="RecaptchaV2TaskProxyless",
        recaptcha_task_proxied="RecaptchaV2Task",
    ),
)

_BY_NAME = {p.name: p for p in PROVIDERS}

_dead_lock = threading.Lock()
# slot -> (deadline_monotonic, reason). The slot is the provider name plus the
# last 4 characters of the key, so one user's emptied account never steps over
# the operator's account of the same provider (and no full key is ever stored).
_dead: dict[str, tuple[float, str]] = {}


def key_for(provider: Provider) -> str:
    return str(getattr(config, provider.env_name, "") or "").strip()


def _slot(provider: Provider, client_key: str) -> str:
    return f"{provider.name}:{(client_key or '?')[-4:]}"


def user_account(user_id: int) -> tuple[Provider, str] | None:
    """This user's own solver account, when they uploaded one.

    The caller decides what it means: for ``solve_*`` it replaces the
    operator's accounts entirely (own key only, never a fallback).
    """
    from bot import user_captcha

    got = user_captcha.get_key(user_id)
    if got is None:
        return None
    provider = _BY_NAME.get(got[0])
    if provider is None:
        return None
    return provider, got[1]


def user_accounts(user_id: int) -> list[tuple[Provider, str]]:
    """All of this user's own solver accounts, in stored order.

    A user may keep one key per provider (up to ``user_captcha.MAX_KEYS``);
    the solve walks them as a chain — first live key wins, an account that
    answers «no balance» is stepped over exactly like the operator's own.
    """
    from bot import user_captcha

    out: list[tuple[Provider, str]] = []
    for name, key in user_captcha.get_keys(user_id):
        provider = _BY_NAME.get(name)
        if provider is not None and key:
            out.append((provider, key))
    return out


def configured_providers() -> list[Provider]:
    """Providers with a key, in the operator's order."""
    order = [
        name.strip().lower()
        for name in str(getattr(config, "CAPTCHA_PROVIDER_ORDER", "") or "").split(",")
        if name.strip()
    ]
    ordered = [_BY_NAME[n] for n in order if n in _BY_NAME]
    # Anything the order forgot still counts — a key in `.env` is intent.
    ordered += [p for p in PROVIDERS if p not in ordered]
    return [p for p in ordered if key_for(p)]


def is_configured() -> bool:
    return bool(configured_providers())


def _mark_dead(slot: str, reason: str) -> None:
    with _dead_lock:
        _dead[slot] = (time.monotonic() + DEAD_PROVIDER_TTL, reason)
    log(f"[captcha] {slot} stepped over for {DEAD_PROVIDER_TTL:.0f}s: {reason}")


def _dead_reason(slot: str) -> str:
    """Why this account is being stepped over, or "" when it is usable."""
    with _dead_lock:
        entry = _dead.get(slot)
        if not entry:
            return ""
        deadline, reason = entry
        if time.monotonic() >= deadline:
            _dead.pop(slot, None)
            return ""
        return reason


def reset_dead_providers() -> None:
    """Forget every cooldown — for tests and for `/reload` after a top-up."""
    with _dead_lock:
        _dead.clear()


def _error_detail(provider: Provider, data: dict) -> str:
    """Human-ish reason from a solver envelope, never including the key."""
    code = str(data.get("errorCode") or "").strip()
    desc = str(data.get("errorDescription") or "").strip()
    if code and desc:
        body = f"{code}: {desc}"
    else:
        body = code or desc or "unknown solver error"
    return f"{provider.name}: {body}"[:300]


def _proxy_fields(provider: Provider, proxy_url: str) -> dict:
    """Proxy in the shape this provider wants it, or {} if it cannot be read."""
    if provider.dialect == "capsolver":
        return {"proxy": proxy_url}
    parsed = urlparse(proxy_url if "://" in proxy_url else f"http://{proxy_url}")
    if not parsed.hostname or not parsed.port:
        return {}
    fields = {
        "proxyType": (parsed.scheme or "http").lower(),
        "proxyAddress": parsed.hostname,
        "proxyPort": int(parsed.port),
    }
    if parsed.username:
        fields["proxyLogin"] = parsed.username
    if parsed.password:
        fields["proxyPassword"] = parsed.password
    return fields


def _build_task(
    provider: Provider, website_url: str, website_key: str, proxy_url: str, action: str, cdata: str
) -> dict:
    proxied = bool(proxy_url)
    task: dict = {
        "type": provider.task_proxied if proxied else provider.task_proxyless,
        "websiteURL": website_url,
        "websiteKey": website_key,
    }
    if proxied:
        fields = _proxy_fields(provider, proxy_url)
        if fields:
            task.update(fields)
        else:
            # An unreadable proxy is not a reason to send a proxied task with no
            # proxy in it: fall back to the type that needs none.
            task["type"] = provider.task_proxyless
    if provider.dialect == "capsolver":
        metadata = {}
        if action:
            metadata["action"] = action
        if cdata:
            metadata["cdata"] = cdata
        if metadata:
            task["metadata"] = metadata
    elif provider.dialect == "anticaptcha":
        if action:
            task["action"] = action
        if cdata:
            task["cData"] = cdata
    else:
        if action:
            task["action"] = action
        if cdata:
            task["data"] = cdata
    return task


def _build_recaptcha_v2_task(
    provider: Provider, website_url: str, website_key: str, proxy_url: str
) -> dict:
    """reCAPTCHA v2 (checkbox, the Netlify Forms class). No action/cdata."""
    proxied = bool(proxy_url)
    task: dict = {
        "type": (
            (provider.recaptcha_task_proxied or provider.recaptcha_task_proxyless)
            if proxied
            else provider.recaptcha_task_proxyless
        ),
        "websiteURL": website_url,
        "websiteKey": website_key,
    }
    if not task["type"]:
        return {}
    if proxied:
        fields = _proxy_fields(provider, proxy_url)
        if fields:
            task.update(fields)
        else:
            task["type"] = provider.recaptcha_task_proxyless
    return task


def balances() -> list[tuple[str, float | None, str]]:
    """``(provider, balance, detail)`` for every configured account."""
    out: list[tuple[str, float | None, str]] = []
    for provider in configured_providers():
        try:
            data = requests.post(
                f"{provider.base_url}/getBalance",
                json={"clientKey": key_for(provider)},
                timeout=HTTP_TIMEOUT,
            ).json()
        except Exception as exc:  # noqa: BLE001 — network/JSON both mean "no answer"
            out.append((provider.name, None, f"{provider.name} unreachable: {exc}"[:300]))
            continue
        if data.get("errorId"):
            out.append((provider.name, None, _error_detail(provider, data)))
            continue
        try:
            out.append((provider.name, float(data.get("balance") or 0.0), ""))
        except (TypeError, ValueError):
            out.append((provider.name, None, f"{provider.name}: unreadable balance"))
    return out


def balance() -> tuple[float | None, str]:
    """Total balance across configured accounts, or ``(None, reason)``."""
    rows = balances()
    if not rows:
        return None, "no captcha solver key is set"
    usable = [value for _name, value, _detail in rows if value is not None]
    if not usable:
        return None, "; ".join(detail for _n, _v, detail in rows if detail)[:300]
    return sum(usable), "; ".join(d for _n, _v, d in rows if d)[:300]


def _submit_and_poll(
    provider: Provider,
    task: dict,
    *,
    timeout: float,
    poll_interval: float,
    client_key: str,
    label: str,
) -> tuple[str, str]:
    """createTask → poll getTaskResult → ``(token, detail)``.

    The solved value may arrive as ``solution.token`` (Turnstile) or
    ``solution.gRecaptchaResponse`` (reCAPTCHA) depending on the task type.
    """
    started = time.monotonic()
    try:
        created = requests.post(
            f"{provider.base_url}/createTask",
            json={"clientKey": client_key, "task": task},
            timeout=HTTP_TIMEOUT,
        ).json()
    except Exception as exc:  # noqa: BLE001
        return "", f"{provider.name} createTask failed: {exc}"[:300]
    if created.get("errorId"):
        return "", _error_detail(provider, created)
    task_id = str(created.get("taskId") or "").strip()
    if not task_id:
        return "", f"{provider.name} returned no taskId"

    while time.monotonic() - started < max(5.0, float(timeout)):
        time.sleep(max(0.5, float(poll_interval)))
        try:
            result = requests.post(
                f"{provider.base_url}/getTaskResult",
                json={"clientKey": client_key, "taskId": task_id},
                timeout=HTTP_TIMEOUT,
            ).json()
        except Exception as exc:  # noqa: BLE001 — one bad poll is not a failure
            log(f"[captcha] {provider.name} poll error task={task_id}: {exc}")
            continue
        if result.get("errorId"):
            return "", _error_detail(provider, result)
        if str(result.get("status") or "").lower() == "ready":
            solution = result.get("solution") or {}
            token = str(
                solution.get("token") or solution.get("gRecaptchaResponse") or ""
            ).strip()
            if not token:
                return "", f"{provider.name} returned an empty token"
            log(
                f"[captcha] {label} solved by {provider.name} in "
                f"{time.monotonic() - started:.1f}s key={str(task.get('websiteKey') or '')[:12]}…"
            )
            return token, ""
        # status "idle"/"processing" → keep waiting

    return "", f"{provider.name} timeout after {timeout:.0f}s"


def _solve_with(
    provider: Provider,
    website_url: str,
    website_key: str,
    *,
    timeout: float,
    poll_interval: float,
    proxy_url: str,
    action: str,
    cdata: str,
    client_key: str = "",
) -> tuple[str, str]:
    """One provider's attempt → ``(token, detail)``."""
    client_key = client_key or key_for(provider)
    task = _build_task(provider, website_url, website_key, proxy_url, action, cdata)
    return _submit_and_poll(
        provider,
        task,
        timeout=timeout,
        poll_interval=poll_interval,
        client_key=client_key,
        label="turnstile",
    )


def _solve_recaptcha_with(
    provider: Provider,
    website_url: str,
    website_key: str,
    *,
    timeout: float,
    poll_interval: float,
    proxy_url: str,
    client_key: str = "",
) -> tuple[str, str]:
    client_key = client_key or key_for(provider)
    task = _build_recaptcha_v2_task(provider, website_url, website_key, proxy_url)
    if not task:
        return "", f"{provider.name} has no reCAPTCHA v2 task type"
    return _submit_and_poll(
        provider,
        task,
        timeout=timeout,
        poll_interval=poll_interval,
        client_key=client_key,
        label="recaptcha-v2",
    )


def _walk_providers(
    attempts: list[tuple[Provider, str]],
    solve_one,
    *,
    timeout: float,
    poll_interval: float,
) -> tuple[str, str]:
    """Shared provider walk: dead-account cooldown, permanent vs transient."""
    from bot.http_client import is_captcha_solver_permanent_error

    permanent: list[str] = []
    transient = ""
    tried = 0

    for provider, client_key in attempts:
        slot = _slot(provider, client_key)
        cooling = _dead_reason(slot)
        if cooling:
            permanent.append(cooling)
            continue

        tried += 1
        token, detail = solve_one(
            provider,
            client_key,
            timeout=timeout,
            poll_interval=poll_interval,
        )
        if token:
            return token, ""

        if is_captcha_solver_permanent_error(detail):
            # An empty balance or a dead key does not heal inside this run, and
            # asking again costs a round-trip per row to hear the same thing.
            _mark_dead(slot, detail)
            permanent.append(detail)
            continue

        transient = detail
        log(f"[captcha] {provider.name} failed, trying the next provider: {detail}")

    if transient:
        # A live account's transient failure is the honest answer: reporting a
        # dead account's "no balance" alongside it would stop a row that the
        # next attempt could still file.
        return "", transient
    if permanent:
        return "", "; ".join(permanent)[:300]
    return "", f"no usable captcha provider (tried {tried})"


def solve_turnstile(
    website_url: str,
    website_key: str,
    *,
    timeout: float = SOLVE_TIMEOUT,
    poll_interval: float = POLL_INTERVAL,
    proxy_url: str = "",
    action: str = "",
    cdata: str = "",
    user_id: int | None = None,
) -> tuple[str, str]:
    """Solve one Turnstile challenge → ``(token, detail)``.

    ``token`` is empty when every configured provider failed; ``detail`` then
    says why. The caller decides whether that is a retry (network, timeout) or a
    stop (no balance, bad key) — `bot/errors.py` already maps anything mentioning
    "captcha" / "capsolver" to one user-facing sentence, and
    `is_captcha_solver_permanent_error` reads the detail for the stop.

    ``user_id`` switches to that user's **own** solver account when they
    uploaded one (`/keys`). Own key means own balance only: the operator's
    accounts are never a fallback, and a dead own key fails the row loudly
    instead of quietly spending somebody else's money.
    """

    def solve_one(provider: Provider, client_key: str, *, timeout: float, poll_interval: float):
        return _solve_with(
            provider,
            website_url,
            website_key,
            timeout=timeout,
            poll_interval=poll_interval,
            proxy_url=proxy_url,
            action=action,
            cdata=cdata,
            client_key=client_key,
        )

    return _walk_providers(
        _attempts_for(user_id), solve_one, timeout=timeout, poll_interval=poll_interval
    )


def solve_recaptcha_v2(
    website_url: str,
    website_key: str,
    *,
    timeout: float = SOLVE_TIMEOUT,
    poll_interval: float = POLL_INTERVAL,
    proxy_url: str = "",
    user_id: int | None = None,
) -> tuple[str, str]:
    """Solve one reCAPTCHA v2 checkbox → ``(token, detail)``.

    Same contract as `solve_turnstile`: first provider wins, dead accounts are
    stepped over for `DEAD_PROVIDER_TTL`, ``user_id`` switches to the user's
    own solver key when they uploaded one. The token goes into the form field
    ``g-recaptcha-response`` (that is how Netlify Forms reads it).
    """

    def solve_one(provider: Provider, client_key: str, *, timeout: float, poll_interval: float):
        return _solve_recaptcha_with(
            provider,
            website_url,
            website_key,
            timeout=timeout,
            poll_interval=poll_interval,
            proxy_url=proxy_url,
            client_key=client_key,
        )

    return _walk_providers(
        _attempts_for(user_id), solve_one, timeout=timeout, poll_interval=poll_interval
    )


def _attempts_for(user_id: int | None) -> list[tuple[Provider, str]]:
    """Whose accounts to try: the user's own key chain, or the operator's list."""
    if user_id is not None:
        own = user_accounts(int(user_id))
        if own:
            return own
    return [(p, key_for(p)) for p in configured_providers()]
