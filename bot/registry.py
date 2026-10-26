"""Discover and load project modules from projects/<id>/."""

from __future__ import annotations

import importlib.util
import sys
import threading
try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, Callable

from bot.config import OK_LIMIT, PROJECTS_DIR, PROJECTS_STRICT


@dataclass
class Project:
    id: str
    name: str
    ok_limit: int
    order: int
    description: str
    help_html: Callable[[], str]
    parse: Callable[[str], dict | None]
    validate: Callable[[dict], str | None]
    submit: Callable[..., dict]
    format_result: Callable[..., str] | None = None
    path: Path | None = None
    # Optional public links (from project.toml: link, twitter_url)
    link: str = ""
    twitter_url: str = ""
    # Optional mint announcement shown on the project screen: date, phases,
    # supply, price — whatever the project itself has published. Plain text
    # (it is HTML-escaped before it reaches Telegram), RU with an optional EN
    # variant. `mint_url` adds a button next to X / site when the mint lives
    # on a page of its own (a countdown, a /mint route).
    mint_info: str = ""
    mint_info_en: str = ""
    mint_url: str = ""
    # If True, bot may submit with empty proxy list (direct HTTP). Never hardcode project ids in core.
    allow_direct: bool = False
    # Bot-only: skip shared/own proxies and use direct when allow_direct (GAS-friendly modules).
    prefer_direct: bool = False
    # Bot-only submit tuning (None = use global BOT_* from config).
    min_delay: float | None = None
    max_delay: float | None = None
    retries: int | None = None
    retry_delay: float | None = None
    # VIP bulk .txt: ordered (key, required, label). Empty = bulk disabled for project.
    bulk_columns: list[tuple[str, bool, str]] = field(default_factory=list)
    # Auto-generate N synthetic x.com/{handle}/status/{id} URLs for /data queue jobs.
    # None = derive from bulk_columns URL-like keys.
    auto_tweet_urls: int | None = None
    # AUD-011: allow module.parse fallback when bulk_columns positional fails.
    # Default False — bulk schema is authoritative unless project opts in.
    bulk_allow_line_parse: bool = False
    # Optional Phase C extras (safe defaults)
    version: str = ""
    health_url: str = ""
    health_method: str = "GET"
    # At least one named env var must be non-empty before this project is
    # operational (for example, one of several supported captcha providers).
    health_required_env_any: list[str] = field(default_factory=list)
    # Optional queue hooks for projects whose submit fields depend on prior rows.
    queue_prepare: Callable[..., None] | None = None
    queue_before_submit: Callable[..., None] | None = None
    queue_after_submit: Callable[..., None] | None = None
    queue_cleanup: Callable[..., None] | None = None
    queue_parallel: int | None = None
    # Same pin, but only for admin-granted VIPs (`bot.limits.is_vip`).
    # `queue_parallel` alone cannot say "one row for everybody, five for a
    # VIP": it is one number for every tier. This is the exception a
    # throttled project needs when the throttle is a precaution rather than
    # a measured limit — the few trusted accounts keep their threads while
    # everyone else goes single-file.
    queue_parallel_vip: int | None = None
    # Read a `health_url` on the alert loop's schedule and runtime-disable
    # the project the moment that probe says `closed` — no admin button.
    # Opt-in, and only an unambiguous verdict counts (see bot/alerts.py).
    auto_close_on_gate: bool = False
    # How many jobs of THIS project may run at once (None = PROJECT_CONCURRENCY).
    # A project whose rows are heavy can insist on one job at a time.
    project_concurrency: int | None = None
    # Ceiling on rows submitted in parallel inside ONE job for this project.
    # Unlike `queue_parallel` (which pins an exact number) this only ever
    # lowers the user's tier value — for endpoints that cannot take the tier's
    # thread count, typically a Google Apps Script sheet that serializes writes
    # behind a lock and answers "Lock timeout" when several land at once.
    max_job_parallel: int | None = None
    # Self-sourced projects mint their own rows (wallet + fields) instead of
    # reading the user's /data file — used where the site needs a signature and
    # the bot must generate the wallet itself.
    self_sourced: bool = False
    generate_rows: Callable[..., list[dict]] | None = None
    # Fixed batch per run; 0 = fill every free slot (classic behaviour).
    batch_size: int = 0
    # Hide the project from users whose tier limit is below this.
    min_ok_limit: int = 0
    # Telegram ids this project belongs to. Empty (the normal case) means the
    # project is public and the tier gates above decide alone; a non-empty list
    # pins it to exactly those users — a form the operator runs for himself or
    # for one person, not something the whole base should see in /start.
    only_user_ids: tuple[int, ...] = ()
    # Every row needs a paid captcha solve, so the project is offered to VIP
    # and paying users only (bot/limits.py: project_allowed). Set it in
    # project.toml, or leave it out: a project that names a solver key in
    # `health_required_env_any` is treated as captcha-backed automatically,
    # so forgetting the flag cannot leak solver balance to the free tier.
    requires_captcha: bool = False
    # Called once when a job stops running, to hand its artifact to the owner.
    queue_deliver: Callable[..., None] | None = None
    # Optional manual retry of that hand-off; its presence adds a button to the
    # project screen (a delivery that failed must not strand the artifact).
    queue_redeliver: Callable[..., int] | None = None


_projects: dict[str, Project] = {}
_loaded = False
_projects_lock = threading.RLock()


def _log(msg: str) -> None:
    print(f"[registry] {msg}", flush=True)


# A project that cannot run without one of these keys is paying a solver per
# row. Matching on the name, not on a hardcoded provider list, keeps a second
# provider (or a rename) from silently reopening the project to the free tier.
_CAPTCHA_KEY_MARKERS = ("CAPSOLVER", "CAPTCHA", "TURNSTILE", "HCAPTCHA", "RECAPTCHA")


def _names_a_captcha_key(env_names: list[str] | None) -> bool:
    """True when `health_required_env_any` names a captcha solver key."""
    for name in env_names or []:
        upper = str(name or "").upper()
        if any(marker in upper for marker in _CAPTCHA_KEY_MARKERS):
            return True
    return False


def _load_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as f:
        return tomllib.load(f)


def _import_module_file(project_id: str, module_path: Path) -> ModuleType:
    # Drop cached package modules so edits to api.py/module.py apply on reload.
    # module.py typically does `from projects.<id> import api` — without this,
    # /reload_projects re-execs module.py but keeps a stale api.py in sys.modules.
    for key in list(sys.modules):
        if key == f"projects.{project_id}" or key.startswith(f"projects.{project_id}."):
            del sys.modules[key]
        if key == f"projects_dyn.{project_id}" or key.startswith(
            f"projects_dyn.{project_id}."
        ):
            del sys.modules[key]

    name = f"projects_dyn.{project_id}.module"
    spec = importlib.util.spec_from_file_location(name, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {module_path}")
    mod = importlib.util.module_from_spec(spec)
    # Ensure parent package placeholder so relative imports could work later
    pkg_name = f"projects_dyn.{project_id}"
    if "projects_dyn" not in sys.modules:
        pkg = ModuleType("projects_dyn")
        pkg.__path__ = []  # type: ignore[attr-defined]
        sys.modules["projects_dyn"] = pkg
    if pkg_name not in sys.modules:
        sub = ModuleType(pkg_name)
        sub.__path__ = [str(module_path.parent)]  # type: ignore[attr-defined]
        sys.modules[pkg_name] = sub
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _coerce_project(raw: Any, meta: dict[str, Any], folder: Path) -> Project:
    """Build Project from get_project() return value (dict or object)."""
    if isinstance(raw, Project):
        return raw

    def _int_or(primary: Any, secondary: Any, default: int) -> int:
        # Use explicit None checks — `or` would drop legitimate 0 for ok_limit/order.
        if primary is not None and primary != "":
            return int(primary)
        if secondary is not None and secondary != "":
            return int(secondary)
        return int(default)

    def _bool_or(primary: Any, secondary: Any) -> bool:
        for val in (primary, secondary):
            if val is None or val == "":
                continue
            if isinstance(val, str):
                return val.strip().lower() in ("1", "true", "yes", "on")
            return bool(val)
        return False

    def _opt_float(val: Any) -> float | None:
        if val is None or val == "":
            return None
        return float(val)

    def _opt_int(val: Any) -> int | None:
        if val is None or val == "":
            return None
        return int(val)

    if isinstance(raw, dict):
        pid = str(raw.get("id") or meta.get("id") or folder.name)
        name = str(raw.get("name") or meta.get("name") or pid)
        ok_limit = _int_or(raw.get("ok_limit"), meta.get("ok_limit"), OK_LIMIT)
        order = _int_or(raw.get("order"), meta.get("order"), 100)
        description = str(raw.get("description") or meta.get("description") or "")
        link = str(raw.get("link") or meta.get("link") or "")
        twitter_url = str(
            raw.get("twitter_url") or meta.get("twitter_url") or ""
        )
        mint_info = str(raw.get("mint_info") or meta.get("mint_info") or "")
        mint_info_en = str(
            raw.get("mint_info_en") or meta.get("mint_info_en") or ""
        )
        mint_url = str(raw.get("mint_url") or meta.get("mint_url") or "")
        help_html = raw["help_html"]
        parse = raw["parse"]
        validate = raw["validate"]
        submit = raw["submit"]
        format_result = raw.get("format_result")
        allow_direct = bool(raw.get("allow_direct", False))
        prefer_direct = bool(raw.get("prefer_direct", False))
        min_delay = _opt_float(raw.get("min_delay"))
        max_delay = _opt_float(raw.get("max_delay"))
        retries = _opt_int(raw.get("retries"))
        retry_delay = _opt_float(raw.get("retry_delay"))
        bulk_columns = list(raw.get("bulk_columns") or [])
        auto_tweet_urls = raw.get("auto_tweet_urls", meta.get("auto_tweet_urls"))
        bulk_allow_line_parse = bool(
            raw.get(
                "bulk_allow_line_parse",
                meta.get("bulk_allow_line_parse", False),
            )
        )
        version = str(raw.get("version") or meta.get("version") or "")
        health_url = str(raw.get("health_url") or meta.get("health_url") or "")
        health_method = str(
            raw.get("health_method") or meta.get("health_method") or "GET"
        )
        health_required_env_any = list(
            raw.get("health_required_env_any")
            or meta.get("health_required_env_any")
            or []
        )
        queue_prepare = raw.get("queue_prepare")
        queue_before_submit = raw.get("queue_before_submit")
        queue_after_submit = raw.get("queue_after_submit")
        queue_cleanup = raw.get("queue_cleanup")
        queue_parallel = _opt_int(
            raw.get("queue_parallel", meta.get("queue_parallel"))
        )
        project_concurrency = _opt_int(
            raw.get("project_concurrency", meta.get("project_concurrency"))
        )
        queue_parallel_vip = _opt_int(
            raw.get("queue_parallel_vip", meta.get("queue_parallel_vip"))
        )
        auto_close_on_gate = _bool_or(
            raw.get("auto_close_on_gate"), meta.get("auto_close_on_gate")
        )
        max_job_parallel = _opt_int(
            raw.get("max_job_parallel", meta.get("max_job_parallel"))
        )
        self_sourced = bool(
            raw.get("self_sourced", meta.get("self_sourced", False))
        )
        generate_rows = raw.get("generate_rows")
        queue_deliver = raw.get("queue_deliver")
        queue_redeliver = raw.get("queue_redeliver")
        batch_size = _int_or(raw.get("batch_size"), meta.get("batch_size"), 0)
        min_ok_limit = _int_or(
            raw.get("min_ok_limit"), meta.get("min_ok_limit"), 0
        )
        requires_captcha = _bool_or(
            raw.get("requires_captcha"), meta.get("requires_captcha")
        )
        only_user_ids = raw.get("only_user_ids", meta.get("only_user_ids"))
    else:
        pid = str(getattr(raw, "id", None) or meta.get("id") or folder.name)
        name = str(getattr(raw, "name", None) or meta.get("name") or pid)
        ok_limit = _int_or(
            getattr(raw, "ok_limit", None), meta.get("ok_limit"), OK_LIMIT
        )
        order = _int_or(getattr(raw, "order", None), meta.get("order"), 100)
        description = str(
            getattr(raw, "description", None) or meta.get("description") or ""
        )
        link = str(getattr(raw, "link", None) or meta.get("link") or "")
        twitter_url = str(
            getattr(raw, "twitter_url", None) or meta.get("twitter_url") or ""
        )
        mint_info = str(
            getattr(raw, "mint_info", None) or meta.get("mint_info") or ""
        )
        mint_info_en = str(
            getattr(raw, "mint_info_en", None) or meta.get("mint_info_en") or ""
        )
        mint_url = str(
            getattr(raw, "mint_url", None) or meta.get("mint_url") or ""
        )
        help_html = raw.help_html
        parse = raw.parse
        validate = raw.validate
        submit = raw.submit
        format_result = getattr(raw, "format_result", None)
        allow_direct = bool(getattr(raw, "allow_direct", False))
        prefer_direct = bool(getattr(raw, "prefer_direct", False))
        min_delay = _opt_float(getattr(raw, "min_delay", None))
        max_delay = _opt_float(getattr(raw, "max_delay", None))
        retries = _opt_int(getattr(raw, "retries", None))
        retry_delay = _opt_float(getattr(raw, "retry_delay", None))
        bulk_columns = list(getattr(raw, "bulk_columns", None) or [])
        auto_tweet_urls = getattr(
            raw, "auto_tweet_urls", meta.get("auto_tweet_urls")
        )
        bulk_allow_line_parse = bool(
            getattr(
                raw,
                "bulk_allow_line_parse",
                meta.get("bulk_allow_line_parse", False),
            )
        )
        version = str(
            getattr(raw, "version", None) or meta.get("version") or ""
        )
        health_url = str(
            getattr(raw, "health_url", None) or meta.get("health_url") or ""
        )
        health_method = str(
            getattr(raw, "health_method", None)
            or meta.get("health_method")
            or "GET"
        )
        health_required_env_any = list(
            getattr(raw, "health_required_env_any", None)
            or meta.get("health_required_env_any")
            or []
        )
        queue_prepare = getattr(raw, "queue_prepare", None)
        queue_before_submit = getattr(raw, "queue_before_submit", None)
        queue_after_submit = getattr(raw, "queue_after_submit", None)
        queue_cleanup = getattr(raw, "queue_cleanup", None)
        queue_parallel = _opt_int(
            getattr(raw, "queue_parallel", meta.get("queue_parallel"))
        )
        queue_parallel_vip = _opt_int(
            getattr(raw, "queue_parallel_vip", meta.get("queue_parallel_vip"))
        )
        auto_close_on_gate = _bool_or(
            getattr(raw, "auto_close_on_gate", None), meta.get("auto_close_on_gate")
        )
        project_concurrency = _opt_int(
            getattr(raw, "project_concurrency", meta.get("project_concurrency"))
        )
        max_job_parallel = _opt_int(
            getattr(raw, "max_job_parallel", meta.get("max_job_parallel"))
        )
        self_sourced = bool(
            getattr(raw, "self_sourced", meta.get("self_sourced", False))
        )
        generate_rows = getattr(raw, "generate_rows", None)
        queue_deliver = getattr(raw, "queue_deliver", None)
        queue_redeliver = getattr(raw, "queue_redeliver", None)
        batch_size = _int_or(
            getattr(raw, "batch_size", None), meta.get("batch_size"), 0
        )
        min_ok_limit = _int_or(
            getattr(raw, "min_ok_limit", None), meta.get("min_ok_limit"), 0
        )
        requires_captcha = _bool_or(
            getattr(raw, "requires_captcha", None), meta.get("requires_captcha")
        )
        only_user_ids = getattr(
            raw, "only_user_ids", meta.get("only_user_ids")
        )

    if not callable(help_html):
        text = str(help_html)
        help_html = lambda t=text: t  # noqa: E731
    if not callable(parse) or not callable(validate) or not callable(submit):
        raise TypeError(f"{pid}: parse/validate/submit must be callable")
    # Light config validation (micro-audit): folder name must match id
    if pid != folder.name:
        raise ValueError(
            f"{folder.name}: project id {pid!r} must match folder name"
        )

    # Normalize bulk_columns: list of (key, required, label)
    norm_cols: list[tuple[str, bool, str]] = []
    for col in bulk_columns:
        if isinstance(col, (list, tuple)) and len(col) >= 2:
            key = str(col[0]).strip()
            if not key or not key.replace("_", "").isalnum():
                raise ValueError(f"{pid}: invalid bulk_columns key {col!r}")
            req = bool(col[1])
            lab = str(col[2]) if len(col) > 2 else key
            norm_cols.append((key, req, lab))
        elif isinstance(col, dict):
            key = str(col.get("key") or col.get("name") or "").strip()
            if not key:
                continue
            if not key.replace("_", "").isalnum():
                raise ValueError(f"{pid}: invalid bulk_columns key {key!r}")
            req = bool(col.get("required", True))
            lab = str(col.get("label") or key)
            norm_cols.append((key, req, lab))
        else:
            raise ValueError(f"{pid}: bulk_columns entry must be tuple/dict, got {col!r}")

    # toml overrides for limit/order/name/links when present
    if "ok_limit" in meta:
        ok_limit = int(meta["ok_limit"])
    if "order" in meta:
        order = int(meta["order"])
    if "name" in meta:
        name = str(meta["name"])
    if "description" in meta:
        description = str(meta["description"])
    if "link" in meta:
        link = str(meta["link"] or "")
    if "twitter_url" in meta:
        twitter_url = str(meta["twitter_url"] or "")
    if "mint_info" in meta:
        mint_info = str(meta["mint_info"] or "")
    if "mint_info_en" in meta:
        mint_info_en = str(meta["mint_info_en"] or "")
    if "mint_url" in meta:
        mint_url = str(meta["mint_url"] or "")
    if "version" in meta and meta.get("version") not in (None, ""):
        version = str(meta["version"])
    if "health_url" in meta and meta.get("health_url") not in (None, ""):
        health_url = str(meta["health_url"])
    if "health_method" in meta and meta.get("health_method") not in (None, ""):
        health_method = str(meta["health_method"])
    if "health_required_env_any" in meta:
        health_required_env_any = list(meta.get("health_required_env_any") or [])
    if "auto_tweet_urls" in meta and meta.get("auto_tweet_urls") not in (None, ""):
        auto_tweet_urls = meta.get("auto_tweet_urls")
    if "bulk_allow_line_parse" in meta:
        bulk_allow_line_parse = bool(meta.get("bulk_allow_line_parse"))
    if "only_user_ids" in meta:
        only_user_ids = meta.get("only_user_ids")

    # Delay pair: if only one set, keep the other from config at call site;
    # if both set and min > max, swap.
    if min_delay is not None and max_delay is not None and min_delay > max_delay:
        min_delay, max_delay = max_delay, min_delay

    auto_n: int | None
    if auto_tweet_urls is None or auto_tweet_urls == "":
        auto_n = None
    else:
        auto_n = int(auto_tweet_urls)

    health_method = health_method.strip().upper() or "GET"
    if health_method not in ("GET", "HEAD", "OPTIONS"):
        raise ValueError(f"{pid}: health_method must be read-only")

    # A self-sourced project replaces the /data file as the row source, so it
    # must be able to mint rows — otherwise its jobs would silently be empty.
    if self_sourced and not callable(generate_rows):
        raise ValueError(
            f"{pid}: self_sourced project must expose generate_rows()"
        )
    if batch_size < 0:
        raise ValueError(f"{pid}: batch_size must be >= 0")
    if min_ok_limit < 0:
        raise ValueError(f"{pid}: min_ok_limit must be >= 0")
    health_env = [
        str(name).strip()
        for name in health_required_env_any
        if str(name).strip()
    ]
    # A single id is accepted as well as a list, and anything unparsable is a
    # config error rather than a silently public project: the whole point of
    # the field is that nobody outside it reaches the project.
    if only_user_ids is None or only_user_ids == "":
        allowed_ids: tuple[int, ...] = ()
    else:
        if isinstance(only_user_ids, (int, str)):
            only_user_ids = [only_user_ids]
        try:
            allowed_ids = tuple(
                sorted({int(str(uid).strip()) for uid in only_user_ids})
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"{pid}: only_user_ids must be Telegram ids, got {only_user_ids!r}"
            ) from exc

    return Project(
        id=pid,
        name=name,
        ok_limit=ok_limit,
        order=order,
        description=description,
        help_html=help_html,
        parse=parse,
        validate=validate,
        submit=submit,
        format_result=format_result if callable(format_result) else None,
        path=folder,
        link=link.strip(),
        twitter_url=twitter_url.strip(),
        mint_info=mint_info.strip(),
        mint_info_en=mint_info_en.strip(),
        mint_url=mint_url.strip(),
        allow_direct=allow_direct,
        prefer_direct=prefer_direct,
        min_delay=min_delay,
        max_delay=max_delay,
        retries=retries,
        retry_delay=retry_delay,
        bulk_columns=norm_cols,
        auto_tweet_urls=auto_n,
        bulk_allow_line_parse=bool(bulk_allow_line_parse),
        version=version.strip(),
        health_url=health_url.strip(),
        health_method=health_method,
        health_required_env_any=health_env,
        queue_prepare=queue_prepare if callable(queue_prepare) else None,
        queue_before_submit=(
            queue_before_submit if callable(queue_before_submit) else None
        ),
        queue_after_submit=(
            queue_after_submit if callable(queue_after_submit) else None
        ),
        queue_cleanup=queue_cleanup if callable(queue_cleanup) else None,
        queue_parallel=queue_parallel,
        queue_parallel_vip=queue_parallel_vip,
        auto_close_on_gate=bool(auto_close_on_gate),
        project_concurrency=project_concurrency,
        max_job_parallel=max_job_parallel,
        self_sourced=self_sourced,
        generate_rows=generate_rows if callable(generate_rows) else None,
        batch_size=batch_size,
        min_ok_limit=min_ok_limit,
        requires_captcha=requires_captcha or _names_a_captcha_key(
            health_required_env_any
        ),
        queue_deliver=queue_deliver if callable(queue_deliver) else None,
        queue_redeliver=queue_redeliver if callable(queue_redeliver) else None,
        only_user_ids=allowed_ids,
    )


def _load_one(folder: Path, *, include_disabled: bool = False) -> Project | None:
    toml_path = folder / "project.toml"
    module_path = folder / "module.py"
    if not toml_path.is_file():
        _log(f"skip {folder.name}: no project.toml")
        return None
    if not module_path.is_file():
        _log(f"skip {folder.name}: no module.py")
        return None

    meta = _load_toml(toml_path)
    if not meta.get("enabled", True) and not include_disabled:
        _log(f"skip {folder.name}: enabled=false")
        return None

    pid = str(meta.get("id") or folder.name)
    if pid != folder.name:
        _log(f"warn {folder.name}: id={pid!r} != folder name (using folder name)")
        pid = folder.name
        meta["id"] = pid

    mod = _import_module_file(pid, module_path)
    if hasattr(mod, "get_project") and callable(mod.get_project):
        raw = mod.get_project()
    elif hasattr(mod, "project"):
        raw = mod.project
    else:
        raise AttributeError(f"{pid}: module must define get_project() or project")

    return _coerce_project(raw, meta, folder)


def load_projects(projects_dir: Path | None = None, *, force: bool = False) -> dict[str, Project]:
    with _projects_lock:
        return _load_projects_locked(projects_dir, force=force)


def load_project_folder(project_id: str, projects_dir: Path | None = None) -> Project | None:
    """One project straight off disk, `enabled = false` included — tools only.

    The registry is the single trigger for everything user-facing: the moment
    an id loads, the project is in `/start` and the one-shot announcement
    preview goes out (PROJECTS.md шаг D). That is why a new project stays
    disabled until it has been tested on the operator's wallets — and why the
    test cannot go through the registry to reach it. This builds the same
    `Project` without registering it anywhere, so `tools/smoke_project.py` can
    run a project no user can see. Nothing in the bot calls it.
    """
    root = Path(projects_dir or PROJECTS_DIR)
    folder = root / (project_id or "")
    if not folder.is_dir() or folder.name.startswith("_"):
        return None
    return _load_one(folder, include_disabled=True)


def _load_projects_locked(
    projects_dir: Path | None = None,
    *,
    force: bool = False,
) -> dict[str, Project]:
    global _projects, _loaded
    if _loaded and not force:
        return _projects

    root = Path(projects_dir or PROJECTS_DIR)
    # /gform adapters live in state (a user's form is runtime state, not code
    # the repo ships — §13.1). Same loader, same contract; only the folder
    # differs. A duplicate id across the two dirs is a config error, exactly
    # like a duplicate inside one.
    from bot.config import USER_PROJECTS_DIR

    roots = [root]
    if Path(USER_PROJECTS_DIR) != root and Path(USER_PROJECTS_DIR).is_dir():
        roots.append(Path(USER_PROJECTS_DIR))
    found: dict[str, Project] = {}

    for r in roots:
        if not r.is_dir():
            _log(f"projects dir missing: {r}")
            continue
        for folder in sorted(r.iterdir()):
            if not folder.is_dir():
                continue
            name = folder.name
            if name.startswith("_"):
                continue
            try:
                proj = _load_one(folder)
                if proj is None:
                    continue
                if proj.id in found:
                    raise RuntimeError(f"duplicate project id: {proj.id}")
                found[proj.id] = proj
                _log(f"loaded {proj.id} ({proj.name})")
            except Exception as e:
                _log(f"FAILED {name}: {e}")
                if PROJECTS_STRICT:
                    raise

    _projects = found
    _loaded = True
    return _projects


def _is_runtime_disabled(project_id: str) -> bool:
    """SQLite runtime off (admin form-closed) — not toml enabled=false."""
    import os

    marker = "AUTOREG_RT_OFF_" + "".join(
        ch if ch.isalnum() else "_" for ch in (project_id or "").upper()
    )
    if os.getenv(marker) == "1":
        return True
    try:
        from bot import db as bot_db

        return bot_db.is_project_runtime_disabled(project_id)
    except Exception:
        return False


def list_projects(*, include_runtime_disabled: bool = False) -> list[Project]:
    """Enabled projects for users. Runtime-disabled are hidden unless flag set."""
    load_projects()
    items = list(_projects.values())
    if not include_runtime_disabled:
        items = [p for p in items if not _is_runtime_disabled(p.id)]
    return sorted(items, key=lambda p: (p.order, p.name.lower()))


def get_project(
    project_id: str, *, allow_runtime_disabled: bool = False
) -> Project | None:
    """
    Return loaded project. Runtime-disabled → None unless allow_runtime_disabled
    (admin enable / inspect).
    """
    load_projects()
    p = _projects.get(project_id)
    if p is None:
        return None
    if not allow_runtime_disabled and _is_runtime_disabled(project_id):
        return None
    return p


def reload_projects() -> list[Project]:
    """Reload modules from disk; runtime DB disable still filters list_projects."""
    return list(load_projects(force=True).values())
