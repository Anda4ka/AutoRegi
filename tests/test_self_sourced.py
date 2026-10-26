"""Core support for projects that mint their own rows.

Covers the row source, the tier floor and the delivery hook — the three pieces
that let a project generate wallets server-side instead of reading /data.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot import db as bot_db
from bot import limits as bot_limits
from bot import submit_queue as queue
from bot.registry import Project

bot_db.init_db()

UID = 991001


def _ok(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _project(**kw) -> Project:
    """A minimal self-sourced project; callers override what they test."""
    calls: list[dict] = []

    def generate_rows(*, user_id: int, count: int) -> list[dict]:
        calls.append({"user_id": user_id, "count": count})
        return [{"address": f"0x{i:040x}", "handle": f"h{i}"} for i in range(count)]

    defaults = {
        "id": "selfsrc",
        "name": "Self Sourced",
        "ok_limit": 100,
        "order": 1,
        "description": "",
        "help_html": lambda: "help",
        "parse": lambda _t: None,
        "validate": lambda _f: None,
        "submit": lambda *_a, **_k: {"status": "OK", "detail": ""},
        "self_sourced": True,
        "generate_rows": generate_rows,
        "batch_size": 100,
    }
    defaults.update(kw)
    p = Project(**defaults)
    p._calls = calls  # type: ignore[attr-defined]
    return p


def _set_limit(value: int, uid: int = UID) -> None:
    """Pin this user's effective OK limit, the way /set_vip_limit does."""
    bot_db.set_user_limit_override(uid, value, admin_id=1)
    bot_limits.invalidate(uid)


# ---------------------------------------------------------------- row source

def test_rows_generated_without_data_file() -> None:
    """No /data upload anywhere — the project supplies every row itself."""
    _set_limit(100)
    project = _project()
    rows, errors, dup = queue.prepare_job_rows(UID, project)
    _ok(errors == [], f"unexpected errors: {errors}")
    _ok(len(rows) == 100, f"expected 100 rows, got {len(rows)}")
    _ok(dup == 0, "self-sourced rows must not report dedup skips")
    _ok(project._calls == [{"user_id": UID, "count": 100}], "generate_rows args")


def test_batch_is_all_or_nothing() -> None:
    """A partial batch would burn slots for an incomplete run — refuse it."""
    _set_limit(50)
    project = _project()
    rows, errors, _ = queue.prepare_job_rows(UID, project)
    _ok(rows == [], "no rows may be produced below a full batch")
    _ok(len(errors) == 1 and "50" in errors[0], f"expected slot error, got {errors}")
    _ok(project._calls == [], "generate_rows must not run when slots are short")


def test_no_slots_left() -> None:
    """Slots already spent on this project — nothing left to mint against."""
    uid = UID + 1
    bot_db.ensure_user(uid, "tester", "Tester")
    _set_limit(1, uid)
    project = _project(batch_size=1)
    bot_db.record_submission(uid, project.id, "h", "0x" + "1" * 40, "OK", "")
    bot_limits.invalidate(uid)
    rows, errors, _ = queue.prepare_job_rows(uid, project)
    _ok(rows == [], "no slots means no rows")
    _ok(len(errors) == 1, f"expected one error, got {errors}")
    _ok(project._calls == [], "generate_rows must not run without slots")


def test_batch_size_zero_fills_slots() -> None:
    """batch_size = 0 keeps the classic 'as many as fit' behaviour."""
    _set_limit(7)
    project = _project(batch_size=0)
    rows, errors, _ = queue.prepare_job_rows(UID, project)
    _ok(errors == [] and len(rows) == 7, f"expected 7 rows, got {len(rows)} {errors}")


def test_generator_failure_is_reported_not_raised() -> None:
    """A broken generator must surface as a user error, not kill the handler."""
    _set_limit(100)

    def boom(**_kw):
        raise RuntimeError("vault offline")

    project = _project(generate_rows=boom)
    rows, errors, _ = queue.prepare_job_rows(UID, project)
    _ok(rows == [], "failed generation yields no rows")
    _ok(len(errors) == 1 and "vault offline" in errors[0], f"got {errors}")


def test_generator_returning_nothing_is_reported() -> None:
    _set_limit(100)
    project = _project(generate_rows=lambda **_k: [])
    rows, errors, _ = queue.prepare_job_rows(UID, project)
    _ok(rows == [] and len(errors) == 1, f"got {rows} {errors}")


def test_extra_rows_are_trimmed_to_batch() -> None:
    """The project cannot hand out more slots than the user actually has."""
    _set_limit(100)
    project = _project(
        generate_rows=lambda *, user_id, count: [
            {"address": f"0x{i:040x}"} for i in range(count + 25)
        ]
    )
    rows, errors, _ = queue.prepare_job_rows(UID, project)
    _ok(len(rows) == 100, f"expected trim to 100, got {len(rows)}")
    _ok(errors == [], f"unexpected errors: {errors}")


# ----------------------------------------------------------------- tier gate

def test_tier_floor_hides_and_shows_project() -> None:
    gated = _project(min_ok_limit=100)
    plain = _project(min_ok_limit=0)

    _set_limit(10)
    _ok(not bot_limits.project_allowed(gated, UID), "free tier must not see it")
    _ok(bot_limits.project_allowed(plain, UID), "ungated project stays visible")
    _ok(
        bot_limits.visible_projects([gated, plain], UID) == [plain],
        "visible_projects must drop the gated one",
    )

    _set_limit(100)
    _ok(bot_limits.project_allowed(gated, UID), "tier 100 reaches the floor")
    _ok(
        len(bot_limits.visible_projects([gated, plain], UID)) == 2,
        "both visible at tier 100",
    )


def test_tier_floor_denies_anonymous() -> None:
    """No user id means no proof of tier — a gated project stays hidden."""
    _ok(not bot_limits.project_allowed(_project(min_ok_limit=100), None), "anon denied")
    _ok(bot_limits.project_allowed(_project(min_ok_limit=0), None), "ungated allowed")


# ------------------------------------------------------------ registry rules

def _raw_project(**kw) -> dict:
    """The dict shape a module's get_project() returns."""
    raw = {
        "id": "selfsrc",
        "name": "Self Sourced",
        "help_html": lambda: "help",
        "parse": lambda _t: None,
        "validate": lambda _f: None,
        "submit": lambda *_a, **_k: {"status": "OK"},
    }
    raw.update(kw)
    return raw


def test_registry_rejects_self_sourced_without_generator() -> None:
    """A silently empty job is worse than a loud config error at load time."""
    from bot import registry

    folder = Path("/tmp/selfsrc")
    try:
        registry._coerce_project(
            _raw_project(self_sourced=True), {}, folder
        )
    except ValueError as exc:
        _ok("generate_rows" in str(exc), f"unexpected message: {exc}")
    else:
        raise AssertionError("self_sourced without generate_rows must not load")


def test_registry_reads_new_fields_from_toml() -> None:
    """project.toml is the operator-facing switch for these."""
    from bot import registry

    project = registry._coerce_project(
        _raw_project(generate_rows=lambda **_k: []),
        {"self_sourced": True, "batch_size": 100, "min_ok_limit": 100},
        Path("/tmp/selfsrc"),
    )
    _ok(project.self_sourced is True, "self_sourced from toml")
    _ok(project.batch_size == 100, f"batch_size {project.batch_size}")
    _ok(project.min_ok_limit == 100, f"min_ok_limit {project.min_ok_limit}")


def test_registry_defaults_keep_classic_projects_unchanged() -> None:
    from bot import registry

    project = registry._coerce_project(_raw_project(), {}, Path("/tmp/selfsrc"))
    _ok(project.self_sourced is False, "classic project is not self-sourced")
    _ok(project.batch_size == 0, "no batch by default")
    _ok(project.min_ok_limit == 0, "no tier floor by default")
    _ok(project.generate_rows is None and project.queue_deliver is None, "no hooks")


def test_project_concurrency_overrides_the_global_default() -> None:
    """A heavy project may insist on one job at a time without slowing others."""
    from bot import config as bot_config

    default = queue._project_parallel_limit()
    _ok(default == max(1, int(bot_config.PROJECT_CONCURRENCY)), f"default {default}")

    project = _project(id="pinned", project_concurrency=1)
    real = queue.get_project
    queue.get_project = lambda _pid, **_kw: project  # type: ignore[assignment]
    try:
        _ok(queue._project_parallel_limit("pinned") == 1, "project pin wins")
    finally:
        queue.get_project = real  # type: ignore[assignment]


def test_project_without_a_pin_keeps_the_global_default() -> None:
    project = _project(id="unpinned", project_concurrency=None)
    real = queue.get_project
    queue.get_project = lambda _pid, **_kw: project  # type: ignore[assignment]
    try:
        _ok(
            queue._project_parallel_limit("unpinned") == queue._project_parallel_limit(),
            "unpinned projects are unaffected",
        )
    finally:
        queue.get_project = real  # type: ignore[assignment]


def test_registry_reads_project_concurrency_from_toml() -> None:
    from bot import registry

    project = registry._coerce_project(
        _raw_project(), {"project_concurrency": 1}, Path("/tmp/selfsrc")
    )
    _ok(project.project_concurrency == 1, f"got {project.project_concurrency}")
    plain = registry._coerce_project(_raw_project(), {}, Path("/tmp/selfsrc"))
    _ok(plain.project_concurrency is None, "unset means the global default")


def test_registry_rejects_negative_sizes() -> None:
    from bot import registry

    for field, value in (("batch_size", -1), ("min_ok_limit", -5)):
        try:
            registry._coerce_project(
                _raw_project(**{field: value}), {}, Path("/tmp/selfsrc")
            )
        except ValueError as exc:
            _ok(field in str(exc), f"unexpected message: {exc}")
        else:
            raise AssertionError(f"{field}={value} must be rejected")


# -------------------------------------------------------------- deliver hook

def test_deliver_hook_receives_job_rows() -> None:
    seen: list[dict] = []

    project = _project(queue_deliver=lambda **kw: seen.append(kw))

    job = queue.QueueJob(
        id=4242,
        user_id=UID,
        project_id=project.id,
        chat_id=1,
        message_id=None,
        status="done",
        rows=[{"address": "0xabc"}],
    )

    real = queue.get_project
    queue.get_project = lambda _pid, **_kw: project  # type: ignore[assignment]
    try:
        queue._deliver_project_job(job)
    finally:
        queue.get_project = real  # type: ignore[assignment]

    _ok(len(seen) == 1, f"hook must fire once, got {len(seen)}")
    _ok(seen[0]["user_id"] == UID, "hook gets the owner")
    _ok(seen[0]["job_id"] == 4242, "hook gets the job id")
    _ok(seen[0]["status"] == "done", "hook gets the final status")
    _ok(seen[0]["rows"] == [{"address": "0xabc"}], "hook gets the rows")


def test_deliver_hook_failure_does_not_escape() -> None:
    """A broken delivery must not take the queue worker down with it."""
    project = _project(queue_deliver=lambda **_kw: 1 / 0)
    job = queue.QueueJob(
        id=4243,
        user_id=UID,
        project_id=project.id,
        chat_id=1,
        message_id=None,
        status="cancelled",
    )
    real = queue.get_project
    queue.get_project = lambda _pid, **_kw: project  # type: ignore[assignment]
    try:
        queue._deliver_project_job(job)  # must not raise
    finally:
        queue.get_project = real  # type: ignore[assignment]


def test_deliver_hook_absent_is_noop() -> None:
    project = _project(queue_deliver=None)
    job = queue.QueueJob(
        id=4244,
        user_id=UID,
        project_id=project.id,
        chat_id=1,
        message_id=None,
        status="done",
    )
    real = queue.get_project
    queue.get_project = lambda _pid, **_kw: project  # type: ignore[assignment]
    try:
        queue._deliver_project_job(job)
    finally:
        queue.get_project = real  # type: ignore[assignment]
