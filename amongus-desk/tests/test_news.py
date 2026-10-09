from __future__ import annotations

import os
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pytest

from amongus_desk.config import NewsConfig
from amongus_desk.news import NewsCalendar
from factories import make_instrument

MON = datetime(2026, 10, 5, 10, 0, tzinfo=UTC)  # Monday 10:00, in session


def news(**over: Any) -> NewsConfig:
    return NewsConfig.model_validate(over)


def cal(tmp_path: Path, **over: Any) -> NewsCalendar:
    return NewsCalendar(news(**over), tmp_path)


def test_clean_session_passes(tmp_path: Path) -> None:
    res = cal(tmp_path).check(make_instrument(), MON)
    assert res.ok and res.reasons == [] and res.next_event is None


def test_outside_session(tmp_path: Path) -> None:
    res = cal(tmp_path).check(make_instrument(), MON.replace(hour=5))
    assert not res.ok and "outside the configured trading sessions" in res.reasons[0]


def test_instrument_without_sessions_trades_any_time(tmp_path: Path) -> None:
    inst = make_instrument(sessions=[])
    assert cal(tmp_path, rollover_block=None).check(inst, MON.replace(hour=3)).ok


@pytest.mark.parametrize(
    ("moment", "blocked"),
    [
        (datetime(2026, 10, 9, 20, 44, tzinfo=UTC), False),  # Friday before close
        (datetime(2026, 10, 9, 20, 45, tzinfo=UTC), True),  # Friday at close
        (datetime(2026, 10, 10, 12, 0, tzinfo=UTC), True),  # Saturday
        (datetime(2026, 10, 11, 22, 14, tzinfo=UTC), True),  # Sunday before open
        (datetime(2026, 10, 11, 22, 15, tzinfo=UTC), False),  # Sunday at open
    ],
)
def test_weekend(tmp_path: Path, moment: datetime, blocked: bool) -> None:
    inst = make_instrument(sessions=[])
    res = cal(tmp_path, rollover_block=None).check(inst, moment)
    assert (any("weekend" in r for r in res.reasons)) is blocked
    assert not any(
        "weekend" in r for r in cal(tmp_path, block_weekends=False, rollover_block=None).check(inst, moment).reasons
    )


def test_rollover_window(tmp_path: Path) -> None:
    inst = make_instrument(sessions=[])
    c = cal(tmp_path)
    assert any("rollover" in r for r in c.check(inst, MON.replace(hour=21, minute=30)).reasons)
    assert c.check(inst, MON.replace(hour=20, minute=49)).ok


def test_blackout_window_and_currency_intersection(tmp_path: Path) -> None:
    nfp = {"at": "2026-10-05T12:30:00Z", "currencies": ["USD"], "title": "US NFP"}
    boj = {"at": "2026-10-05T12:30:00Z", "currencies": ["JPY"], "title": "BoJ"}
    c = cal(tmp_path, events=[nfp, boj], blackout_minutes_before=15, blackout_minutes_after=10)
    inst = make_instrument()
    before = c.check(inst, MON.replace(hour=12, minute=14))
    assert before.ok and before.next_event is not None and before.next_event.title == "US NFP"
    during = c.check(inst, MON.replace(hour=12, minute=15))
    assert not during.ok and "US NFP" in during.reasons[0] and "BoJ" not in " ".join(during.reasons)
    assert not c.check(inst, MON.replace(hour=12, minute=40)).ok
    after = c.check(inst, MON.replace(hour=12, minute=41))
    assert after.ok and after.next_event is None


def test_events_file_load_reload_and_broken_file(tmp_path: Path) -> None:
    path = tmp_path / "events.yaml"
    path.write_text('- {at: "2026-10-05T12:30:00Z", currencies: [EUR], title: "ECB"}\n', encoding="utf-8")
    c = cal(tmp_path, events_file="events.yaml")
    assert [e.title for e in c.events()] == ["ECB"]
    assert c.status(MON)["events_file"]["ok"] is True

    # edit -> reloaded (bump mtime explicitly so the test doesn't depend on timestamp resolution)
    path.write_text('- {at: "2026-10-05T13:00:00Z", currencies: [USD], title: "CPI"}\n', encoding="utf-8")
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    assert [e.title for e in c.events()] == ["CPI"]

    # broken -> last good list kept, error reported, trading still allowed (file did load once)
    path.write_text("- {at: not-a-date}\n", encoding="utf-8")
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))
    assert [e.title for e in c.events()] == ["CPI"]
    status = c.status(MON)
    assert status["ok"] is False and "invalid" in status["error"]
    assert c.check(make_instrument(), MON).ok


def test_events_file_never_loaded_fails_closed(tmp_path: Path) -> None:
    c = cal(tmp_path, events_file="missing.yaml")
    res = c.check(make_instrument(), MON)
    assert not res.ok and any("news calendar unavailable" in r for r in res.reasons)
    (tmp_path / "missing.yaml").write_text("[]\n", encoding="utf-8")
    assert c.check(make_instrument(), MON).ok


@pytest.mark.parametrize(
    ("content", "fragment"),
    [("", "empty"), ("a: 1\n", "top-level list"), ("[1]\n", "mapping"), ("[unclosed\n", "not valid YAML")],
)
def test_events_file_errors(tmp_path: Path, content: str, fragment: str) -> None:
    (tmp_path / "e.yaml").write_text(content, encoding="utf-8")
    c = cal(tmp_path, events_file="e.yaml")
    c.events()
    assert fragment in (c.status(MON)["error"] or "")


def test_event_without_currencies_affects_everything(tmp_path: Path) -> None:
    c = cal(tmp_path, events=[{"at": "2026-10-05T10:05:00Z", "currencies": [], "title": "Market-wide"}])
    assert not c.check(make_instrument(), MON).ok


def test_status_shape(tmp_path: Path) -> None:
    c = cal(tmp_path, events=[{"at": "2026-10-05T12:30:00Z", "currencies": ["USD"], "title": "NFP"}])
    s = c.status(MON)
    assert s["events_total"] == 1 and s["upcoming"][0]["title"] == "NFP"
    assert s["rollover_block"]["start"] == "20:50"
    assert c.status(MON + timedelta(days=1))["upcoming"] == []
    assert time(20, 45).strftime("%H:%M") == s["weekend_close_utc"]
