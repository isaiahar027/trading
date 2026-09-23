"""Typed, frozen configuration. Loaded once at startup; models never see or modify it."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

KELLY_HARD_CAP = 0.25  # quarter Kelly. Config may go lower, never higher.


@dataclass(frozen=True)
class RunConfig:
    mode: str = "paper"
    interval: str = "1h"
    state_dir: str = "state"
    starting_equity: float = 10_000.0


@dataclass(frozen=True)
class RiskConfig:
    max_drawdown: float = 0.15
    max_daily_loss: float = 0.03
    max_position_frac: float = 0.20
    max_gross_leverage: float = 1.0
    max_orders_per_hour: int = 12
    max_price_deviation: float = 0.02
    max_snapshot_age_s: float = 120
    min_order_notional: float = 11.0


@dataclass(frozen=True)
class PolicyConfig:
    min_setup_quality: float = 2.0
    min_direction_confidence: float = 0.80
    max_toxic_flow: float = 0.50
    kelly_fraction: float = 0.25
    stop_atr: float = 2.0
    target_atr: float = 3.0
    max_hold_bars: int = 48
    escalate_below_confidence: float = 0.60
    escalation_cooldown_s: float = 3600


@dataclass(frozen=True)
class CostConfig:
    taker_fee_bps: float = 4.5
    slippage_bps: float = 3.0

    @property
    def round_trip(self) -> float:
        return 2 * (self.taker_fee_bps + self.slippage_bps) / 1e4


@dataclass(frozen=True)
class JevConfig:
    url: str = "https://api.typesafe.ai/v1/systemone"
    model: str = "jev-1.13.0"
    timeout_s: float = 2.0
    max_retries: int = 2


@dataclass(frozen=True)
class BrainConfig:
    model: str = "claude-opus-5-5"
    effort: str = "high"


@dataclass(frozen=True)
class UniverseConfig:
    symbols: tuple[str, ...] = ("BTC", "ETH")


@dataclass(frozen=True)
class Config:
    run: RunConfig = field(default_factory=RunConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    costs: CostConfig = field(default_factory=CostConfig)
    jev: JevConfig = field(default_factory=JevConfig)
    brain: BrainConfig = field(default_factory=BrainConfig)
    universe: UniverseConfig = field(default_factory=UniverseConfig)

    def validate(self) -> None:
        r, p = self.risk, self.policy
        problems = []
        if not 0 < r.max_drawdown <= 0.15:
            problems.append("risk.max_drawdown must be in (0, 0.15]")
        if not 0 < r.max_daily_loss < r.max_drawdown:
            problems.append("risk.max_daily_loss must be positive and below max_drawdown")
        if not 0 < r.max_position_frac <= 1:
            problems.append("risk.max_position_frac must be in (0, 1]")
        if not 0 < p.kelly_fraction <= KELLY_HARD_CAP:
            problems.append(f"policy.kelly_fraction must be in (0, {KELLY_HARD_CAP}]")
        if p.min_direction_confidence < 0.80:
            problems.append("policy.min_direction_confidence may not be below 0.80")
        if p.min_setup_quality < 2:
            problems.append("policy.min_setup_quality may not be below 2")
        if p.target_atr <= 0 or p.stop_atr <= 0:
            problems.append("policy stop/target ATR multiples must be positive")
        if self.run.mode not in ("paper", "live"):
            problems.append("run.mode must be 'paper' or 'live'")
        if problems:
            raise ValueError("invalid config: " + "; ".join(problems))


_SECTIONS = {
    "run": RunConfig, "risk": RiskConfig, "policy": PolicyConfig, "costs": CostConfig,
    "jev": JevConfig, "brain": BrainConfig, "universe": UniverseConfig,
}


def load_config(path: str | Path = "config/default.toml") -> Config:
    raw = tomllib.loads(Path(path).read_text())
    kwargs = {}
    for name, cls in _SECTIONS.items():
        section = dict(raw.get(name, {}))
        known = {f.name for f in fields(cls)}
        unknown = set(section) - known
        if unknown:
            raise ValueError(f"unknown keys in [{name}]: {sorted(unknown)}")
        if name == "universe" and "symbols" in section:
            section["symbols"] = tuple(section["symbols"])
        kwargs[name] = cls(**section)
    cfg = Config(**kwargs)
    cfg.validate()
    return cfg
