"""Collector observations; these clocks never infer when content became public."""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import time


def utc_text() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def elapsed_ms(start: float, end: float) -> float:
    """Only compare monotonic clocks captured inside this process/run."""
    return max(0.0, round((end - start) * 1000, 3))


@dataclass
class StageClock:
    started_counter: float
    finished_counter: float | None = None


def report_timing(*, provided_items: bool) -> dict:
    return {
        "input_kind": "provided_items" if provided_items else "rss_network",
        "list_started_at": None,
        "list_finished_at": None,
        "list_duration_ms": None,
        "list_succeeded": None,
    }


def item_timing(kind: str, observed_at: str) -> dict:
    return {
        "input_kind": kind,
        "list_observed_at": observed_at,
        "detail_started_at": None,
        "detail_finished_at": None,
        "detail_duration_ms": None,
        "detail_succeeded": None,
        "save_started_at": None,
        "save_returned_at": None,
        "save_duration_ms": None,
        "observed_to_save_ms": None,
    }


@contextmanager
def measure_stage(timing: dict, stage: str, *, success_only: bool = False):
    """Record a stage end even on errors, except an unconfirmed writer return.

    A successful detail stage means fetch/parse returned normally, not that
    attribution was verified or the content was saved. Exceptions propagate.
    """
    timing[stage + "_started_at"] = utc_text()
    clock = StageClock(time.perf_counter())
    succeeded = False
    try:
        yield clock
        succeeded = True
    finally:
        clock.finished_counter = time.perf_counter()
        if not success_only:
            timing[stage + "_succeeded"] = succeeded
        if succeeded or not success_only:
            suffix = "_returned_at" if success_only else "_finished_at"
            timing[stage + suffix] = utc_text()
            timing[stage + "_duration_ms"] = elapsed_ms(clock.started_counter, clock.finished_counter)
