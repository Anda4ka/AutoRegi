"""What the mini app is allowed to *do*, and who is allowed to provide it.

The queue does not live in the database. ``bot/submit_queue.py`` keeps it in
process memory (``_queue``, ``_jobs``) and its workers only ever pop from
there; the ``submit_jobs`` table is a record of what happened, not a work
list. A second process can insert a perfectly good ``queued`` row and the
running bot will never notice it — the job would sit untouched until a restart
reconciled it. That is why the read-only prototype could be its own process and
this cannot: **starting work from the web means the web server runs inside the
bot process** and calls the same functions the inline buttons call.

This module is the seam that keeps that from becoming a mess. ``webapp.server``
never imports ``tgbot``; it is handed an ``Actions`` object and asks it
questions. The bot builds the real one, bound to the live ``TeleBot`` and to
the gates in ``start_project_queue_job``. The standalone prototype gets
``ReadOnly``, which refuses every write with a reason.

The refusal matters more than it looks. A write path that half-works — accepts
the tap, writes a row, and produces a job nobody runs — is worse than one that
says "not here". So the default is refusal, and the capable implementation is
the one that has to be supplied.
"""

from __future__ import annotations

from typing import Any


class Denied(Exception):
    """A refusal the user should see, in their own language.

    Not an error: every gate in the bot (no slots, no data, form closed, wrong
    tier) reaches the user as a sentence, and the web must not turn those into
    a red 500. ``code`` is for the UI, ``message`` is for the human.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class Actions:
    """Everything the mini app may ask the bot to do.

    The base class is the read-only answer to all of it. Overriding a method
    is a deliberate decision to expose that capability, which is the point:
    nothing becomes possible from the web by accident.
    """

    #: False here makes the UI render its buttons as disabled with a reason,
    #: instead of offering taps that will all come back refused.
    writable = False

    #: Why writes are off, shown once at the top of the screen rather than
    #: repeated on every row.
    read_only_reason = "запуск из веба выключен"

    def may_write(self, user_id: int) -> bool:
        """Is *this* user allowed to change anything right now?

        Separate from ``writable`` on purpose. A staged rollout ("operator
        first, everyone later") is a per-user answer, not a per-server one.
        """
        del user_id
        return False

    def gate(self, user_id: int, lang: str = "ru") -> "Denied | None":
        """May this person use the app at all? ``None`` means yes.

        This is the check the web did not have. Every chat handler opens with
        a ban check and the channel gate; the web opened with a signature check
        and nothing else — and a signature answers *who*, never *whether*. So
        a banned account, or one that never joined the required channel, could
        open the mini app and start jobs, upload accounts and create payments,
        all of which the same account was refused two taps away in the chat.

        It sits above ``may_write`` rather than beside it because it is not
        about writing: a banned user has no business reading their queue back
        either, and the chat has always answered them with one sentence and
        nothing else.

        The base class is the read-only prototype, which has no bot to ask and
        can change nothing, so it lets everyone through.
        """
        del user_id, lang
        return None

    def projects(self, user_id: int, lang: str = "ru") -> list[dict[str, Any]]:
        """Every project this user may see, each with why it can or cannot run."""
        del user_id, lang
        return []

    def caps(self, user_id: int, lang: str = "ru",
             projects: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """What this user's tier unlocks, so the UI can stop offering the rest.

        A button that always refuses is worse than no button: it teaches the
        user that the screen lies. So "start everything" is only drawn when it
        would actually run, and the tier that would unlock it is named instead.

        ``projects`` is the list the caller has just built. Deciding "is there
        anything to start" means asking the same readiness question about every
        project, and asking it twice per screen refresh cost as much as the
        whole rest of the request.
        """
        del user_id, lang, projects
        return {"tier": "", "can_start_all": False, "start_all_reason": ""}

    def data(self, user_id: int, lang: str = "ru") -> dict[str, Any]:
        """What this user has uploaded: how much, and what the ceiling is."""
        del user_id, lang
        return {"count": 0, "max_lines": 0, "max_bytes": 0}

    def proxies(self, user_id: int, lang: str = "ru") -> dict[str, Any]:
        """Which proxies the submits would go through, and where they came from."""
        del user_id, lang
        return {"count": 0, "source": "none", "may_upload": False}

    def notice(self, user_id: int, lang: str = "ru") -> dict[str, Any]:
        """Optional ops banner (``code`` + plain ``text``). Empty = none."""
        del user_id, lang
        return {"code": "", "text": ""}

    def privatka(self, user_id: int, lang: str = "ru") -> dict[str, Any]:
        del user_id, lang
        return {"url": "", "win_banner": "", "basic_line": "", "branch": ""}

    def billing(self, user_id: int, lang: str = "ru") -> dict[str, Any]:
        """Plans, the active one, and any payment still waiting to be paid."""
        del user_id, lang
        return {"plans": [], "active": None, "open_payment": None, "sales_open": False}

    def report(self, user_id: int, lang: str = "ru") -> dict[str, Any]:
        """The last run's errors — all of them, not a page at a time.

        Paging exists in the chat because an inline message has a size limit.
        A page does not, so the web sends the list and lets it scroll.
        """
        del user_id, lang
        return {"errors": [], "summary": ""}

    def stats(self, user_id: int, days: int = 7, lang: str = "ru") -> dict[str, Any]:
        """The user's own history, one bucket per UTC day (7 or 30).

        Fetched on an explicit tap, never on the state poll — the poll runs
        every few seconds and this is a range scan over submissions.
        """
        del user_id, days, lang
        raise NotImplementedError

    def mints(self, user_id: int, lang: str = "ru") -> dict[str, Any]:
        """The mint calendar: confirmed facts only, soonest first."""
        del user_id, lang
        return {"rows": [], "may_edit": False}

    def gforms(self, user_id: int, lang: str = "ru") -> list[dict[str, Any]]:
        """The user's own generated Google-Form projects (constructor tab)."""
        del user_id, lang
        return []

    def gform_parse(self, user_id: int, url: str, lang: str = "ru") -> dict[str, Any]:
        raise Denied("read_only", self.read_only_reason)

    def gform_create(self, user_id: int, mapping: dict[str, Any],
                     lang: str = "ru", *, name: str = "",
                     link: str = "") -> dict[str, Any]:
        raise Denied("read_only", self.read_only_reason)

    def gform_delete(self, user_id: int, project_id: str, lang: str = "ru") -> dict[str, Any]:
        raise Denied("read_only", self.read_only_reason)

    def start(self, user_id: int, project_id: str, lang: str = "ru") -> dict[str, Any]:
        raise Denied("read_only", self.read_only_reason)

    def save_mint(self, user_id: int, payload: dict[str, Any],
                  lang: str = "ru") -> dict[str, Any]:
        raise Denied("read_only", self.read_only_reason)

    def clear_mint(self, user_id: int, project_id: str,
                   lang: str = "ru") -> dict[str, Any]:
        raise Denied("read_only", self.read_only_reason)

    def set_lang(self, user_id: int, lang_code: str) -> dict[str, Any]:
        raise Denied("read_only", self.read_only_reason)

    def set_captcha_key(
        self, user_id: int, provider: str, key: str, lang: str = "ru"
    ) -> dict[str, Any]:
        raise Denied("read_only", self.read_only_reason)

    def clear_captcha_key(self, user_id: int, lang: str = "ru") -> dict[str, Any]:
        raise Denied("read_only", self.read_only_reason)

    def set_auto_reg(self, user_id: int, on: bool, lang: str = "ru") -> dict[str, Any]:
        raise Denied("read_only", self.read_only_reason)

    def set_mint_remind(self, user_id: int, on: bool, lang: str = "ru") -> dict[str, Any]:
        raise Denied("read_only", self.read_only_reason)

    def user_setup(self, user_id: int) -> dict[str, Any]:
        """Captcha key mask + auto-registration flag for the «mine» tab."""
        return {"captcha_key": "", "auto_reg": False}

    def buy(self, user_id: int, plan_id: str, lang: str = "ru") -> dict[str, Any]:
        raise Denied("read_only", self.read_only_reason)

    def check_payment(self, user_id: int, payment_id: str, lang: str = "ru") -> dict[str, Any]:
        raise Denied("read_only", self.read_only_reason)

    def cancel_payment(self, user_id: int, payment_id: str, lang: str = "ru") -> dict[str, Any]:
        raise Denied("read_only", self.read_only_reason)

    def deliver(self, user_id: int, project_id: str, lang: str = "ru") -> dict[str, Any]:
        raise Denied("read_only", self.read_only_reason)

    def suggest(self, user_id: int, text: str, lang: str = "ru") -> dict[str, Any]:
        raise Denied("read_only", self.read_only_reason)

    def save_data(self, user_id: int, text: str, lang: str = "ru") -> dict[str, Any]:
        raise Denied("read_only", self.read_only_reason)

    def clear_data(self, user_id: int, lang: str = "ru") -> dict[str, Any]:
        raise Denied("read_only", self.read_only_reason)

    def delete_data_row(self, user_id: int, row: int,
                        lang: str = "ru") -> dict[str, Any]:
        raise Denied("read_only", self.read_only_reason)

    def export_results(self, user_id: int, project_id: str,
                       lang: str = "ru") -> dict[str, Any]:
        """The user's own OK/ALREADY rows for a project, as text.

        Read-only in nature, but the payload is the user's wallet list —
        the base class refuses even reads that carry state out, and an
        override is a deliberate decision to expose it.
        """
        raise Denied("read_only", self.read_only_reason)

    def save_proxies(self, user_id: int, text: str, lang: str = "ru") -> dict[str, Any]:
        raise Denied("read_only", self.read_only_reason)

    def clear_proxies(self, user_id: int, lang: str = "ru") -> dict[str, Any]:
        raise Denied("read_only", self.read_only_reason)

    def check_proxies(self, user_id: int, lang: str = "ru") -> dict[str, Any]:
        raise Denied("read_only", self.read_only_reason)

    def start_all(self, user_id: int, lang: str = "ru") -> dict[str, Any]:
        raise Denied("read_only", self.read_only_reason)

    def cancel(self, user_id: int, job_id: int, lang: str = "ru") -> dict[str, Any]:
        raise Denied("read_only", self.read_only_reason)

    def ensure_user(self, user_id: int, username: str = "",
                    first_name: str = "") -> None:
        """Make sure the bot knows this person before anything writes.

        A read-only server has nobody to remember, so the base does nothing.
        """
        del user_id, username, first_name

    def lang_of(self, user_id: int) -> str:
        """The language this user already reads the bot in."""
        del user_id
        return "ru"


class ReadOnly(Actions):
    """Explicit name for the base behaviour, for call sites that pass it."""
