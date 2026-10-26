"""`/mint <id> clear`: both spellings, and no crash on a calendar-only row.

The clear branch used to answer with `project.name`, which is None for a
calendar-only project (a drop with no adapter in `projects/` — the Hood
Citizens shape): the row was already deleted when the AttributeError hit, so
the admin saw nothing and the handler died. The pipe form `foo | clear` never
matched the old tail check at all and came back as «не разобрал «clear»».
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="mint_cmd_"))
os.environ["BOT_TOKEN"] = "t:t"
os.environ["BOT_DB"] = str(_TMP / "t.db")

for _mod in list(sys.modules):
    if _mod == "bot" or _mod.startswith("bot."):
        del sys.modules[_mod]

from bot import mint_ux  # noqa: E402
from bot import mints as bot_mints  # noqa: E402


def test_bare_form_is_a_clear() -> None:
    assert mint_ux.clear_target("hoodcitizens clear") == "hoodcitizens"


def test_pipe_form_is_a_clear() -> None:
    assert mint_ux.clear_target("hoodcitizens | clear") == "hoodcitizens"


def test_case_and_padding_do_not_change_the_verdict() -> None:
    assert mint_ux.clear_target("HoodCitizens   |   CLEAR") == "hoodcitizens"


def test_anything_after_clear_is_not_a_clear() -> None:
    """`foo clear | supply abc` must reach the parser and be refused there."""
    assert mint_ux.clear_target("foo clear | supply abc") is None


def test_a_plain_edit_is_not_a_clear() -> None:
    assert mint_ux.clear_target("foo | supply 2222") is None
    assert mint_ux.clear_target("foo | name Hood Citizens") is None


def test_missing_project_or_clear_is_not_a_clear() -> None:
    assert mint_ux.clear_target("") is None
    assert mint_ux.clear_target("clear") is None
    assert mint_ux.clear_target("| clear") is None


def test_calendar_only_row_clears_without_a_registry_project() -> None:
    """The crash path: a name-only calendar row, no adapter behind it."""
    bot_mints.set_mint(
        "hoodcitizens",
        name="Hood Citizens",
        source="manual",
        updated_by=1,
    )
    rows = [r for r in bot_mints.calendar({}) if r["project_id"] == "hoodcitizens"]
    assert rows, "the calendar-only row must exist first"
    bot_mints.clear_project("hoodcitizens")  # used to AttributeError in tgbot
    rows = [r for r in bot_mints.calendar({}) if r["project_id"] == "hoodcitizens"]
    assert not rows, "the row must be gone"


def test_clearing_an_unknown_id_is_a_quiet_no_op() -> None:
    bot_mints.clear_project("never-existed")


def test_the_pipe_form_alone_does_not_parse_as_an_edit() -> None:
    """Why clear_target exists: the generic parser cannot read `| clear`."""
    _pid, _payload, errors = mint_ux.parse_mint_command("hoodcitizens | clear")
    assert errors, "parser must refuse the clear segment (the clear branch handles it)"


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            import traceback

            traceback.print_exc()
            print(f"FAIL {fn.__name__}: {exc}")
    if failed:
        print(f"FAILED {failed}/{len(tests)}")
        sys.exit(1)
    print(f"ALL {len(tests)} PASSED")
    sys.exit(0)


if __name__ == "__main__":
    main()
