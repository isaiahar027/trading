import ast
from pathlib import Path

import pytest

from trader.config import Config, RiskConfig, load_config
from trader.risk import KillSwitch, RiskManager
from trader.market import OrderIntent, Portfolio, Position

NOW = 1_750_000_000_000


@pytest.fixture
def ks(tmp_path):
    k = KillSwitch(tmp_path)
    k.arm()
    return k


def rm(ks, equity=10_000.0, **cfg):
    return RiskManager(RiskConfig(**cfg), ks, equity, NOW)


def buy(qty=10.0, px=100.0, reduce=False):
    return OrderIntent("SOL", qty, px, reduce, "t")


def test_ok_order(ks):
    v = rm(ks).check(buy(), Portfolio(10_000), 100, NOW, NOW)
    assert v.approved and v.qty == 10.0


def test_not_armed_blocks_entries(tmp_path):
    k = KillSwitch(tmp_path)
    v = rm(k).check(buy(), Portfolio(10_000), 100, NOW, NOW)
    assert not v.approved and "not armed" in v.reasons[0]


def test_tripped_blocks_entries_allows_exits(ks):
    pf = Portfolio(10_000)
    pf.positions["SOL"] = Position("SOL", qty=5, entry_px=100)
    ks.trip("test")
    r = rm(ks)
    assert not r.check(buy(), pf, 100, NOW, NOW).approved
    v = r.check(buy(-5, reduce=True), pf, 100, NOW, NOW)
    assert v.approved and v.qty == -5 and v.flatten_all


def test_max_drawdown_trips_kill(ks):
    r = rm(ks)
    pf = Portfolio(8_400)  # 16% below the 10k peak
    r.update(pf, NOW)
    assert ks.tripped and "drawdown" in ks.reason()
    assert not r.check(buy(1), pf, 100, NOW, NOW).approved


def test_daily_loss_blocks_new_risk_until_next_day(ks):
    r = rm(ks)
    pf = Portfolio(9_690)
    assert not r.check(buy(1), pf, 100, NOW, NOW).approved
    r.update(pf, NOW + 86_400_000)  # new UTC day resets the budget
    assert r.check(buy(1), pf, 100, NOW + 86_400_000, NOW + 86_400_000).approved


def test_position_cap_clips_and_never_enlarges(ks):
    v = rm(ks).check(buy(1000), Portfolio(10_000), 100, NOW, NOW)
    assert v.approved and v.qty == pytest.approx(20.0)  # 20% of 10k at $100
    assert abs(v.qty) <= 1000


def test_gross_leverage_cap(ks):
    pf = Portfolio(10_000)
    pf.positions["BTC"] = Position("BTC", qty=0.09, entry_px=100_000)
    pf.marks["BTC"] = 100_000  # 9k gross already
    v = rm(ks).check(buy(15), pf, 100, NOW, NOW)
    assert v.approved and v.qty == pytest.approx(10.0)


@pytest.mark.parametrize("kw,reason", [
    (dict(mid=100, data_ts=NOW - 10 * 60_000), "stale"),
    (dict(mid=90, data_ts=NOW), "deviates"),
    (dict(mid=100, data_ts=NOW, qty=0.05), "below minimum"),
])
def test_sanity_rejects(ks, kw, reason):
    v = rm(ks).check(buy(kw.get("qty", 10)), Portfolio(10_000), kw["mid"], kw["data_ts"], NOW)
    assert not v.approved and reason in " ".join(v.reasons)


def test_rate_limit(ks):
    r = rm(ks, max_orders_per_hour=2)
    pf = Portfolio(10_000)
    assert r.check(buy(1), pf, 100, NOW, NOW).approved
    assert r.check(buy(1), pf, 100, NOW, NOW).approved
    assert not r.check(buy(1), pf, 100, NOW, NOW).approved


def test_reduce_only_that_increases_is_rejected(ks):
    pf = Portfolio(10_000)
    pf.positions["SOL"] = Position("SOL", qty=5, entry_px=100)
    assert not rm(ks).check(buy(5, reduce=True), pf, 100, NOW, NOW).approved


def test_risk_and_policy_do_not_import_the_model():
    src = Path(__file__).parents[1] / "src" / "trader"
    for mod in ("risk.py", "market.py", "config.py"):
        tree = ast.parse((src / mod).read_text())
        names = [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
        names += [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
        assert not any("jev" in n or "brain" in n or "anthropic" in n for n in names), mod
    pol = ast.parse((src / "policy.py").read_text())
    imported = [n.module for n in ast.walk(pol) if isinstance(n, ast.ImportFrom)]
    assert ".jev.client" not in imported and "brain" not in " ".join(filter(None, imported))


@pytest.mark.parametrize("override", [
    {"risk": {"max_drawdown": 0.2}},
    {"policy": {"kelly_fraction": 0.5}},
    {"policy": {"min_direction_confidence": 0.7}},
    {"policy": {"min_setup_quality": 1}},
])
def test_config_refuses_looser_limits(tmp_path, override):
    import tomllib
    raw = tomllib.loads(Path("config/default.toml").read_text())
    for sec, kv in override.items():
        raw[sec].update(kv)
    lines = []
    for sec, kv in raw.items():
        lines.append(f"[{sec}]")
        for k, v in kv.items():
            lines.append(f"{k} = {v!r}".replace("'", '"').replace("(", "[").replace(")", "]"))
    p = tmp_path / "c.toml"
    p.write_text("\n".join(lines))
    with pytest.raises(ValueError):
        load_config(p)


def test_default_config_loads():
    cfg = load_config("config/default.toml")
    assert cfg.risk.max_drawdown == 0.15 and cfg.policy.kelly_fraction == 0.25
