from __future__ import annotations

import json
import logging
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from amongus_desk import cli, wiring
from amongus_desk.audit import AuditLog
from amongus_desk.config import load_config
from amongus_desk.domain import HealthCheck, IntentStatus, Mode, OrderIntent, PositionStatus, Side, VenueHealth
from amongus_desk.killswitch import KillSwitch
from amongus_desk.store import Store
from factories import T0, eurusd
from rigs import desk_cfg

ROOT = Path(__file__).resolve().parents[1]


def write_config(tmp_path: Path, **over: Any) -> Path:
    raw: dict[str, Any] = {
        "data_dir": str(tmp_path / "data"),
        "instruments": [eurusd()],
        "risk": {"starting_equity": 10_000},
    }
    raw.update(over)
    path = tmp_path / "desk.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


def test_packaged_example_matches_repo_example() -> None:
    packaged = ROOT / "src" / "amongus_desk" / "templates" / "desk.example.yaml"
    assert packaged.read_text(encoding="utf-8") == (ROOT / "config" / "desk.example.yaml").read_text(encoding="utf-8")


def test_init_config_never_overwrites(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = tmp_path / "config" / "desk.yaml"
    assert cli.main(["init-config", "--path", str(target)]) == cli.EXIT_OK
    assert load_config(target, env={}).mode is Mode.PAPER
    target.write_text("# mine\n", encoding="utf-8")
    assert cli.main(["init-config", "--path", str(target)]) == cli.EXIT_OK
    assert target.read_text(encoding="utf-8") == "# mine\n"
    assert "not overwriting" in capsys.readouterr().out


def test_config_error_exit_code(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("instruments: []\n", encoding="utf-8")
    assert cli.main(["status", "-c", str(bad)]) == cli.EXIT_CONFIG
    assert "configuration error" in capsys.readouterr().err


def test_kill_status_resume(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg_path = write_config(tmp_path)
    assert cli.main(["kill", "-c", str(cfg_path), "--reason", "going to sleep"]) == cli.EXIT_OK
    assert KillSwitch(tmp_path / "data").engaged()
    assert cli.main(["status", "-c", str(cfg_path)]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "ENGAGED" in out and "going to sleep" in out
    assert cli.main(["status", "-c", str(cfg_path), "--json"]) == cli.EXIT_OK
    data = json.loads(capsys.readouterr().out)
    assert data["kill_switch"]["engaged"] is True
    assert cli.main(["resume", "-c", str(cfg_path)]) == cli.EXIT_OK
    assert not KillSwitch(tmp_path / "data").engaged()


def test_resume_refused_while_reconcile_pending(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg_path = write_config(tmp_path)
    store = Store(tmp_path / "data" / "desk.db")
    intent = OrderIntent(
        meeting_id=None,
        symbol="EURUSD",
        side=Side.BUY,
        lots=None,
        entry_ref=None,
        stop_price=None,
        target_price=None,
        mode=Mode.PAPER,
        created_at=T0,
    )
    store.add_intent(intent)
    store.set_intent_status(intent.id, IntentStatus.SUBMITTING)
    store.close()
    KillSwitch(tmp_path / "data").engage("x", by="t")
    assert cli.main(["resume", "-c", str(cfg_path)]) == cli.EXIT_PREFLIGHT
    assert "reconciling" in capsys.readouterr().out
    assert KillSwitch(tmp_path / "data").engaged()
    assert cli.main(["status", "-c", str(cfg_path)]) == cli.EXIT_OK
    assert "NEEDS RECONCILING (1)" in capsys.readouterr().out


def test_audit_verify(tmp_path: Path) -> None:
    cfg_path = write_config(tmp_path)
    log = AuditLog(tmp_path / "data" / "audit.jsonl", fsync=False)
    log.append("x", {"a": 1})
    log.close()
    assert cli.main(["audit-verify", "-c", str(cfg_path)]) == cli.EXIT_OK
    p = tmp_path / "data" / "audit.jsonl"
    p.write_text(p.read_text().replace('"a":1', '"a":2'), encoding="utf-8")
    assert cli.main(["audit-verify", "-c", str(cfg_path)]) == cli.EXIT_ERROR


def test_preflight_in_paper_mode(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["preflight", "-c", str(write_config(tmp_path))]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "paper mode" in out and "no phone alerts" in out


def test_test_order_refuses_live_without_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DESK_LIVE_ACK", "I-ACCEPT-REAL-MONEY-RISK")
    monkeypatch.setenv("TWELVEDATA_API_KEY", "test-key")
    monkeypatch.setattr("sys.platform", "win32")
    cfg_path = write_config(
        tmp_path,
        mode="live",
        feed={"kind": "twelvedata"},
        execution={"venue": "unitrader", "unitrader": {"protective_orders": "panel_fields"}},
    )
    assert cli.main(["test-order", "-c", str(cfg_path), "--symbol", "EURUSD", "--side", "BUY"]) == cli.EXIT_CONFIG
    assert "--i-know-this-is-live" in capsys.readouterr().out


def _demo_fake(tmp_path: Path, **inst: Any) -> Path:
    return write_config(
        tmp_path,
        mode="demo",
        instruments=[eurusd(**inst)],
        execution={"venue": "unitrader", "unitrader": {"backend": "fake"}},
    )


def _intents(tmp_path: Path) -> list[tuple[str, IntentStatus]]:
    store = Store(tmp_path / "data" / "desk.db")
    try:
        return [(r.intent.purpose, r.status) for r in reversed(store.recent_intents())]
    finally:
        store.close()


def test_test_order_paper_round_trip_closes_with_an_opposite_order(tmp_path: Path) -> None:
    cfg_path = write_config(tmp_path)
    code = cli.main(["test-order", "-c", str(cfg_path), "--symbol", "EURUSD", "--side", "BUY", "--yes"])
    assert code == cli.EXIT_OK
    assert _intents(tmp_path) == [("open", IntentStatus.CONFIRMED), ("close", IntentStatus.CONFIRMED)]
    store = Store(tmp_path / "data" / "desk.db")
    assert store.active_positions() == [] and len(store.recent_positions()) == 1
    store.close()


def test_test_order_simulated_terminal_round_trip_uses_close_all(tmp_path: Path) -> None:
    cfg_path = _demo_fake(tmp_path, specs_verified=True)
    code = cli.main(["test-order", "-c", str(cfg_path), "--symbol", "EURUSD", "--side", "BUY", "--yes"])
    assert code == cli.EXIT_OK
    assert _intents(tmp_path) == [("open", IntentStatus.CONFIRMED), ("close_all", IntentStatus.CONFIRMED)]


def test_test_order_refuses_unverified_specs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg_path = _demo_fake(tmp_path, specs_verified=False)
    code = cli.main(["test-order", "-c", str(cfg_path), "--symbol", "EURUSD", "--side", "BUY", "--yes"])
    assert code == cli.EXIT_CONFIG and "specs_verified" in capsys.readouterr().out
    assert _intents(tmp_path) == []


def test_test_order_refuses_while_the_desk_has_positions(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg_path = write_config(tmp_path)
    assert cli.main(["test-order", "-c", str(cfg_path), "--symbol", "EURUSD", "--side", "BUY", "--yes"]) == 0
    store = Store(tmp_path / "data" / "desk.db")
    (pos,) = store.recent_positions()
    store.upsert_position(pos.model_copy(update={"status": PositionStatus.OPEN, "closed_at": None}))
    store.close()
    capsys.readouterr()
    code = cli.main(["test-order", "-c", str(cfg_path), "--symbol", "EURUSD", "--side", "SELL", "--yes"])
    assert code == cli.EXIT_PREFLIGHT and "active position" in capsys.readouterr().out
    assert len(_intents(tmp_path)) == 2  # nothing new was sent


def test_test_order_refuses_while_killed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg_path = write_config(tmp_path)
    KillSwitch(tmp_path / "data").engage("testing", by="test")
    code = cli.main(["test-order", "-c", str(cfg_path), "--symbol", "EURUSD", "--side", "BUY", "--yes"])
    assert code == cli.EXIT_PREFLIGHT and "kill switch" in capsys.readouterr().out
    assert _intents(tmp_path) == []


def test_test_order_validates_lots(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg_path = write_config(tmp_path)
    code = cli.main(["test-order", "-c", str(cfg_path), "--symbol", "EURUSD", "--side", "BUY", "--lots", "5"])
    assert code == cli.EXIT_CONFIG and "lots must be" in capsys.readouterr().out
    code = cli.main(["test-order", "-c", str(cfg_path), "--symbol", "XAUUSD", "--side", "BUY"])
    assert code == cli.EXIT_CONFIG


def test_test_order_needs_typed_confirmation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg_path = write_config(tmp_path)
    monkeypatch.setattr("builtins.input", lambda _prompt: "yes")
    assert cli.main(["test-order", "-c", str(cfg_path), "--symbol", "EURUSD", "--side", "BUY"]) == cli.EXIT_OK
    store = Store(tmp_path / "data" / "desk.db")
    assert store.recent_intents() == []  # nothing was sent
    store.close()


# ------------------------------------------------------------------------------------------- login gate


def health(ok: bool) -> VenueHealth:
    detail = "ok" if ok else "UniTrader window not found: log in to UniTrader yourself"
    return VenueHealth(
        venue="unitrader", ok=ok, checked_at=T0, checks=[HealthCheck(name="window", ok=ok, detail=detail)]
    )


@pytest.mark.anyio
async def test_login_gate_asks_operator_to_log_in(tmp_path: Path) -> None:
    cfg = desk_cfg(tmp_path, mode="demo", execution={"venue": "unitrader", "unitrader": {"backend": "fake"}})
    results = iter([health(False), health(False), health(True)])
    said: list[str] = []
    prompts: list[str] = []

    async def preflight() -> VenueHealth:
        return next(results)

    def prompt(text: str) -> str:
        prompts.append(text)
        return ""

    ok = await cli.login_gate(preflight, cfg, interactive=True, prompt=prompt, say=said.append)
    assert ok and len(prompts) == 2
    text = "\n".join(said)
    assert "log in to your DEMO account yourself" in text
    assert "never asks for, stores or types your password" in text
    assert "✔ UniTrader preflight passed" in text


@pytest.mark.anyio
async def test_login_gate_quit_and_unattended(tmp_path: Path) -> None:
    cfg = desk_cfg(tmp_path, mode="demo", execution={"venue": "unitrader", "unitrader": {"backend": "fake"}})

    async def never() -> VenueHealth:
        return health(False)

    assert not await cli.login_gate(never, cfg, interactive=True, prompt=lambda _p: "q", say=lambda _s: None)
    alerts: list[str] = []

    async def on_wait(message: str) -> None:
        alerts.append(message)

    ok = await cli.login_gate(
        never, cfg, interactive=False, say=lambda _s: None, poll_seconds=0, on_wait=on_wait, max_polls=3
    )
    assert not ok and len(alerts) == 1 and "log in" in alerts[0]


def test_parser_has_all_commands() -> None:
    parser = cli.build_parser()
    for command in (
        "run",
        "preflight",
        "calibrate",
        "test-order",
        "backtest",
        "kill",
        "resume",
        "status",
        "audit-verify",
        "init-config",
    ):
        assert parser.parse_args(
            [
                command,
                *(["--symbol", "EURUSD", "--side", "BUY"] if command == "test-order" else []),
                *(["--synthetic", "10"] if command == "backtest" else []),
            ]
        )


def test_dotenv_next_to_the_config_folder_is_loaded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DESK_DASHBOARD_TOKEN", "x")  # so monkeypatch removes what the .env loader sets
    monkeypatch.delenv("DESK_DASHBOARD_TOKEN")
    project = tmp_path / "desk"
    (project / "config").mkdir(parents=True)
    raw = {
        "data_dir": "data",
        "instruments": [eurusd()],
        "risk": {"starting_equity": 10_000},
        "dashboard": {"host": "100.64.0.1", "allowed_hosts": ["100.64.0.1"]},
    }
    cfg_path = project / "config" / "desk.yaml"
    cfg_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    (project / ".env").write_text("DESK_DASHBOARD_TOKEN=" + "k" * 32 + "\n", encoding="utf-8")
    assert cli.main(["kill", "-c", str(cfg_path), "--reason", "test"]) == cli.EXIT_OK
    assert KillSwitch(project / "data").engaged()


def test_kill_still_works_when_the_config_does_not_validate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("DESK_DASHBOARD_TOKEN", raising=False)
    cfg_path = write_config(tmp_path, dashboard={"host": "100.64.0.1", "allowed_hosts": ["100.64.0.1"]})
    assert cli.main(["status", "-c", str(cfg_path)]) == cli.EXIT_CONFIG  # needs the token
    assert cli.main(["kill", "-c", str(cfg_path)]) == cli.EXIT_OK
    assert "engaging the kill switch" in capsys.readouterr().out
    assert KillSwitch(tmp_path / "data").engaged()


# ------------------------------------------------------------------------------------------- dashboard token file


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_dashboard_token_file_is_private_on_posix(tmp_path: Path) -> None:
    cfg = desk_cfg(tmp_path)
    token, generated = wiring.ensure_dashboard_token(cfg)
    path = tmp_path / "data" / wiring.TOKEN_FILE
    assert generated and len(token) >= 32 and path.read_text(encoding="utf-8").strip() == token
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    path.chmod(0o644)  # e.g. left behind by an older version, or copied over
    assert wiring.ensure_dashboard_token(cfg) == (token, False)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


class FakeIcacls:
    def __init__(self, returncode: int = 0, error: Exception | None = None) -> None:
        self.returncode = returncode
        self.error = error
        self.calls: list[tuple[list[str], dict[str, Any], str]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((argv, kwargs, Path(argv[1]).read_text(encoding="utf-8")))
        if self.error is not None:
            raise self.error
        return subprocess.CompletedProcess(argv, self.returncode, "", "Access is denied." if self.returncode else "")


def windows(monkeypatch: pytest.MonkeyPatch, icacls: FakeIcacls) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(wiring.subprocess, "run", icacls)
    monkeypatch.setenv("USERNAME", "operator")
    monkeypatch.setenv("USERDOMAIN", "VPS01")
    monkeypatch.setenv("SYSTEMROOT", r"C:\Windows")


def test_dashboard_token_file_acl_on_windows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # chmod 0600 does nothing on Windows: the file would inherit the folder's ACL (readable by every local user)
    icacls = FakeIcacls()
    windows(monkeypatch, icacls)
    cfg = desk_cfg(tmp_path)
    token, generated = wiring.ensure_dashboard_token(cfg)
    path = tmp_path / "data" / wiring.TOKEN_FILE
    assert generated and path.read_text(encoding="utf-8").strip() == token
    (argv, kwargs, content_then), *more = icacls.calls
    assert argv == [
        r"C:\Windows\System32\icacls.exe",
        str(path.with_suffix(".tmp")),
        "/inheritance:r",
        "/grant:r",
        r"VPS01\operator:F",
    ]
    assert content_then == ""  # restricted before the token was written into it
    assert not kwargs.get("shell") and not more
    assert wiring.ensure_dashboard_token(cfg) == (token, False)  # an existing file is restricted again
    assert icacls.calls[-1][0][1] == str(path)


@pytest.mark.parametrize("icacls", [FakeIcacls(returncode=5), FakeIcacls(error=FileNotFoundError("icacls.exe"))])
def test_dashboard_token_acl_failure_is_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, icacls: FakeIcacls
) -> None:
    windows(monkeypatch, icacls)
    with caplog.at_level(logging.WARNING, logger="amongus_desk.wiring"):
        token, generated = wiring.ensure_dashboard_token(desk_cfg(tmp_path))
    assert generated and len(token) >= 32
    assert "could not restrict" in caplog.text and ("Access is denied" in caplog.text or "icacls.exe" in caplog.text)
    caplog.clear()
    monkeypatch.delenv("USERNAME")
    with caplog.at_level(logging.WARNING, logger="amongus_desk.wiring"):
        assert wiring.ensure_dashboard_token(desk_cfg(tmp_path)) == (token, False)
    assert "USERNAME is not set" in caplog.text
