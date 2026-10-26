"""Regression tests for is_retryable_failure / is_gas_lock_message (H1 lock⊂block)."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="http_retry_test_"))
os.environ["BOT_DB"] = str(_TMP / "t.db")
os.environ["BOT_TOKEN"] = "test:token"

for mod in list(sys.modules):
    if mod == "bot" or mod.startswith("bot.") or mod == "handlers" or mod.startswith("handlers."):
        del sys.modules[mod]

from bot.http_client import (  # noqa: E402
    _has_word,
    backoff_sleep,
    is_gas_lock_message,
    is_retryable_failure,
)


def _assert(cond: bool, msg: str = "") -> None:
    if not cond:
        raise AssertionError(msg or "assert failed")


def test_has_word_lock_not_in_block() -> None:
    # Substring traps: "lock" is inside "block*" but not a whole word
    _assert(_has_word("block connection", "lock") is False)
    _assert(_has_word("blocked by firewall", "lock") is False)
    _assert(_has_word("blocklist entry", "lock") is False)
    # LockService is one token — whole-word "lock" does not match (phrase path covers it)
    _assert(_has_word("LockService busy", "lock") is False)
    # Real whole-word lock
    _assert(_has_word("lock timeout", "lock") is True)
    _assert(_has_word("holding the lock", "lock") is True)
    _assert(_has_word("the LOCK was held", "lock") is True)


def test_block_not_gas_lock() -> None:
    """'block' must never be treated as spreadsheet lock."""
    cases = (
        "proxy error via 1.2.3.4:8080: block connection",
        "connection blocked",
        "user blocked",
        "IP blocklist",
        "request was blocked by WAF",
    )
    for msg in cases:
        _assert(not is_gas_lock_message(msg), f"gas_lock? {msg!r}")
        _assert(
            not is_retryable_failure(msg, reason="", is_html=False),
            f"retryable? {msg!r}",
        )


def test_real_lock_still_detected() -> None:
    locks = (
        "Exception: Lock timeout after 30 seconds",
        "Spreadsheet lock busy",
        "LockService: another process was holding the lock",
        "lock timeout",
        "Apps Script LockService contention",
    )
    for msg in locks:
        _assert(is_gas_lock_message(msg), f"not gas_lock: {msg!r}")
        _assert(is_retryable_failure(msg, is_html=False), f"not retryable: {msg!r}")
    # reason short-circuit still works for real gas_busy
    _assert(is_retryable_failure("anything", reason="gas_busy", is_html=False))


def test_duplicate_not_retryable() -> None:
    _assert(not is_retryable_failure("duplicate wallet", reason="duplicate"))
    _assert(not is_retryable_failure("already registered duplicate", reason=""))


def test_html_and_http_transient_still_retryable() -> None:
    _assert(is_retryable_failure("page", is_html=True))
    _assert(is_retryable_failure("HTTP 503 service unavailable", is_html=False))
    _assert(is_retryable_failure("connect timeout via proxy", is_html=False))
    _assert(is_retryable_failure("rate limit exceeded", is_html=False))


def test_permanent_reject_not_retryable() -> None:
    # No lock/timeout/html markers — permanent business noise
    _assert(
        not is_retryable_failure("invalid twitter handle", reason="invalid twitter")
    )
    _assert(not is_retryable_failure("invalid wallet address", reason="bad_wallet"))


# Apps Script localizes LockService by the exit IP. Collected on minibroker
# 2026-08-20 (the adapter is gone; the wordings are the lesson).
LOCK_WORDINGS = (
    "Lock timeout: another process was holding the lock for too long.",
    (
        "Expiration de la demande de verrouillage : un autre processus a "
        "conservé le verrouillage trop longtemps."
    ),
    "锁超时：另一个进程占用锁时间过久。（第 3 行，“Code”文件）",
    "ロックのタイムアウト: 別のプロセスがロックを保持している時間が長すぎました。",
    "잠금 시간초과: 다른 프로세스에서 잠금 상태를 너무 오래 유지하고 있습니다.('Code' 파일, 3행)",
    "Kilitleme zaman aşımı: Başka bir işlem kilidi çok uzun süredir tutuyordu.",
    (
        "Se ha superado el tiempo de espera para la solicitud de bloqueo: otro "
        "proceso ha retenido el bloqueo durante demasiado tiempo."
    ),
    "Tempo limite do bloqueio: outro processo manteve o bloqueio por muito tempo.",
    "Thời gian chờ khóa: một quá trình khác đã giữ khóa quá lâu.",
    (
        "Time-out van vergrendeling: een ander proces heeft de vergrendeling te "
        "lang vastgehouden. (regel 3, bestand 'Code')"
    ),
    (
        "Zeitüberschreitung bei Anforderung der Exklusivbearbeitung: Ein anderer "
        "Vorgang hat die Exklusivbearbeitung zu lange gebunden."
    ),
    "Таймаўт блакавання: іншы працэс утрымлівае блакаванне занадта доўга.",
    "หมดเวลาการล็อก: กระบวนการอื่นมีการล็อกไว้นานเกินไป",
    "लॉक टाइमआउट: अन्‍य प्रक्रिया से लॉक को लंबे समय के लिए रोका गया था.",
)


def test_the_locales_the_lock_arrives_in_are_all_read_as_a_lock() -> None:
    """Twelve languages in two hours, one exception — all of them a retry."""
    for message in (f"Exception: {wording}" for wording in LOCK_WORDINGS):
        _assert(is_gas_lock_message(message), message)
    _assert(not is_gas_lock_message("This wallet is already registered."))
    _assert(not is_gas_lock_message("Missing required fields."))
    _assert(not is_gas_lock_message("your entry was blocklisted"))
    _assert(not is_gas_lock_message("account blocked by the moderator"))


def test_a_row_contends_for_the_lock_instead_of_waiting_it_out() -> None:
    """Our rows do not queue behind other registrations (operator, 2026-08-20)."""
    lock = f"Exception: {LOCK_WORDINGS[0]}"
    for attempt in range(1, 5):
        contending = backoff_sleep(attempt, 1.5, lock, contend_lock=True)
        _assert(contending <= 2.5, str(contending))
        _assert(backoff_sleep(attempt, 1.5, lock) > contending)
    _assert(backoff_sleep(2, 1.5, "HTTP 502", contend_lock=True) > 2.5)


def main() -> None:
    tests = [
        test_has_word_lock_not_in_block,
        test_block_not_gas_lock,
        test_real_lock_still_detected,
        test_duplicate_not_retryable,
        test_html_and_http_transient_still_retryable,
        test_permanent_reject_not_retryable,
        test_the_locales_the_lock_arrives_in_are_all_read_as_a_lock,
        test_a_row_contends_for_the_lock_instead_of_waiting_it_out,
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
