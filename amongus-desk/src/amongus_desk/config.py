"""Desk configuration: one YAML file plus secrets from environment variables.

Secrets never live in YAML. They are read from the environment at load time:

=============================  ==========================================================
``ANTHROPIC_API_KEY``          read by the Anthropic SDK itself (Pink's Claude review)
``TWELVEDATA_API_KEY``         market data when ``feed.kind: twelvedata``
``DESK_DASHBOARD_TOKEN``       bearer token for the dashboard (auto-generated if unset)
``DESK_NTFY_TOPIC``            ntfy topic for phone alerts (optional)
``DESK_TELEGRAM_BOT_TOKEN``    Telegram bot token (optional)
``DESK_TELEGRAM_CHAT_ID``      Telegram chat id (optional)
``DESK_LIVE_ACK``              must equal ``I-ACCEPT-REAL-MONEY-RISK`` for ``mode: live``
=============================  ==========================================================

The desk never asks for, stores or types broker (UniTrader) credentials. The operator logs in to the
terminal by hand; the desk only verifies, from the screen, that the expected account is showing.
"""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Mapping, MutableMapping
from datetime import datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Literal, Self

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PositiveFloat,
    PositiveInt,
    PrivateAttr,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

from .domain import Mode, ensure_utc

LIVE_ACK_PHRASE = "I-ACCEPT-REAL-MONEY-RISK"
MAX_RISK_PER_TRADE_PCT = 2.0


class ConfigError(Exception):
    """Configuration is invalid or unsafe. The message is meant for the operator."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SessionWindow(_Model):
    """A recurring UTC window, e.g. Mon-Fri 07:00-20:00. ``end < start`` means it crosses midnight."""

    days: list[Annotated[int, Field(ge=0, le=6)]] = Field(default_factory=lambda: [0, 1, 2, 3, 4])
    start: time
    end: time

    @field_validator("start", "end")
    @classmethod
    def _naive_time(cls, value: time) -> time:
        if value.tzinfo is not None:
            raise ValueError("session times are always UTC; do not add a timezone")
        return value

    def contains(self, moment: datetime) -> bool:
        moment = ensure_utc(moment)
        t = moment.time().replace(tzinfo=None)
        if self.start <= self.end:
            return moment.weekday() in self.days and self.start <= t < self.end
        # Crosses midnight: the part after midnight belongs to the session that started the day before.
        if t >= self.start:
            return moment.weekday() in self.days
        if t < self.end:
            return (moment - timedelta(days=1)).weekday() in self.days
        return False


class InstrumentConfig(_Model):
    symbol: str = Field(pattern=r"^[A-Z0-9._]{2,20}$", description="Symbol exactly as UniTrader shows it")
    feed_symbol: str = Field(min_length=1, description="Symbol in the market data feed, e.g. EUR/USD")
    enabled: bool = True
    pip_size: PositiveFloat  # price units per pip/point used for spreads and protective distances
    contract_size: PositiveFloat  # quote-currency value of a 1.0 price move for 1.0 lot
    quote_currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    quote_to_account_rate: PositiveFloat = 1.0  # static conversion when quote currency != account currency
    lot_step: Decimal = Decimal("0.01")
    min_lot: Decimal = Decimal("0.01")
    max_lot: Decimal = Decimal("0.10")
    typical_spread_pips: float = Field(ge=0)
    max_spread_pips: float | None = Field(default=None, gt=0)
    price_decimals: int = Field(default=5, ge=0, le=8)
    currencies: list[Annotated[str, Field(pattern=r"^[A-Z]{3}$")]] = Field(default_factory=list)
    sessions: list[SessionWindow] = Field(default_factory=list)
    # Anti-guesswork: contract specs differ between brokers. In demo/live mode an instrument only
    # trades once the operator has copied contract size / lot step / min lot from UniTrader's own
    # symbol specification and set this to true.
    specs_verified: bool = False

    @model_validator(mode="after")
    def _lots(self) -> Self:
        for name in ("lot_step", "min_lot", "max_lot"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{self.symbol}: {name} must be positive")
        if self.min_lot > self.max_lot:
            raise ValueError(f"{self.symbol}: min_lot exceeds max_lot")
        if (self.min_lot % self.lot_step) != 0 or (self.max_lot % self.lot_step) != 0:
            raise ValueError(f"{self.symbol}: min_lot and max_lot must be multiples of lot_step")
        return self

    def in_session(self, moment: datetime) -> bool:
        return not self.sessions or any(w.contains(moment) for w in self.sessions)

    @property
    def typical_spread_price(self) -> float:
        return self.typical_spread_pips * self.pip_size


class StrategyConfig(_Model):
    timeframe_minutes: Literal[1, 5, 15, 30, 60] = 5
    donchian_lookback: int = Field(default=20, ge=5, le=500)
    atr_period: int = Field(default=14, ge=2, le=200)
    atr_slow_period: int = Field(default=100, ge=10, le=1000)
    breakout_buffer_atr: float = Field(default=0.05, ge=0, le=2)
    min_range_atr: float = Field(default=1.2, ge=0, le=10)
    min_body_ratio: float = Field(default=0.5, ge=0, le=1)
    min_close_location: float = Field(default=0.6, ge=0, le=1)
    max_extension_atr: float = Field(default=1.0, gt=0, le=10)
    squeeze_period: int = Field(default=20, ge=5, le=200)
    squeeze_lookback: int = Field(default=100, ge=10, le=2000)
    max_squeeze_percentile: float = Field(default=0.5, ge=0, le=1)  # 1.0 disables the squeeze filter
    trend_ema_period: int = Field(default=50, ge=5, le=500)
    trend_slope_bars: int = Field(default=5, ge=1, le=100)
    require_trend_alignment: bool = True
    stop_atr_mult: float = Field(default=1.5, gt=0, le=20)
    target_r: float = Field(default=2.0, gt=0, le=20)
    time_stop_bars: int = Field(default=24, ge=1, le=10_000)
    min_stop_spread_multiple: float = Field(default=4.0, ge=0, le=100)
    signal_cooldown_bars: int = Field(default=6, ge=0, le=1000)
    stale_after_bars: int = Field(default=2, ge=1, le=100)

    @property
    def warmup_bars(self) -> int:
        """Bars of history needed before the scout may fire."""
        return (
            max(
                self.donchian_lookback + 1,
                self.atr_slow_period + 1,
                self.trend_ema_period + self.trend_slope_bars,
                self.squeeze_period + self.squeeze_lookback,
            )
            + 2
        )


class RiskConfig(_Model):
    account_currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    starting_equity: PositiveFloat
    risk_per_trade_pct: float = Field(default=0.25, gt=0, le=MAX_RISK_PER_TRADE_PCT)
    max_lots_per_order: Decimal = Decimal("0.10")
    max_open_positions: int = Field(default=1, ge=1, le=10)
    max_positions_per_symbol: int = Field(default=1, ge=1, le=10)
    max_trades_per_day: int = Field(default=4, ge=1, le=100)
    max_daily_loss_pct: float = Field(default=1.5, gt=0, le=20)
    max_consecutive_losses: int = Field(default=3, ge=1, le=100)
    loss_streak_cooldown_minutes: int = Field(default=240, ge=0, le=10_080)
    min_minutes_between_trades: int = Field(default=15, ge=0, le=10_080)
    symbol_cooldown_minutes_after_loss: int = Field(default=60, ge=0, le=10_080)
    day_rollover_utc: time = time(0, 0)
    max_notional_per_order: float | None = Field(default=None, gt=0)
    flatten_on_daily_loss: bool = False

    @field_validator("max_lots_per_order")
    @classmethod
    def _positive_lots(cls, value: Decimal) -> Decimal:
        if value <= 0:
            raise ValueError("max_lots_per_order must be positive")
        return value


class NewsEvent(_Model):
    at: datetime
    currencies: list[Annotated[str, Field(pattern=r"^[A-Z]{3}$")]]
    title: str = Field(min_length=1, max_length=200)

    @field_validator("at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class NewsConfig(_Model):
    block_weekends: bool = True
    weekend_close_utc: time = time(20, 45)  # Friday: no new trades after this
    weekend_open_utc: time = time(22, 15)  # Sunday: no new trades before this
    # Daily rollover (17:00 New York) moves between 21:00 and 22:00 UTC with US daylight saving time;
    # the default covers both. Spreads widen sharply around it.
    rollover_block: SessionWindow | None = SessionWindow(
        days=[0, 1, 2, 3, 4, 5, 6], start=time(20, 50), end=time(22, 10)
    )
    blackout_minutes_before: int = Field(default=15, ge=0, le=1440)
    blackout_minutes_after: int = Field(default=15, ge=0, le=1440)
    events: list[NewsEvent] = Field(default_factory=list)
    # Optional YAML file with a top-level list of events (same shape as ``events``); re-read when it changes.
    events_file: Path | None = None


class LlmConfig(_Model):
    enabled: bool = False
    model: str = "claude-opus-5-5"
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "low"
    max_tokens: int = Field(default=4096, ge=256, le=64_000)
    timeout_seconds: float = Field(default=60.0, gt=0, le=600)
    max_retries: int = Field(default=2, ge=0, le=10)
    server_fallback: bool = True  # server-side refusal fallback (beta) on supported models
    failure_policy: Literal["veto", "abstain"] = "veto"
    max_calls_per_day: int = Field(default=40, ge=0, le=10_000)
    recent_bars: int = Field(default=30, ge=5, le=300)


class SyntheticFeedConfig(_Model):
    seed: int = 7
    interval_seconds: float = Field(default=1.0, ge=0, le=3600)  # wall time between emitted bars (0 = fast)
    start: datetime | None = None  # first bar time; defaults to "now" rounded down
    base_prices: dict[str, PositiveFloat] = Field(default_factory=dict)

    @field_validator("start")
    @classmethod
    def _utc_start(cls, value: datetime | None) -> datetime | None:
        return ensure_utc(value) if value is not None else None


class CsvFeedConfig(_Model):
    path: Path | None = None  # CSV with columns: symbol,start,open,high,low,close[,volume]


class TwelveDataConfig(_Model):
    base_url: str = "https://api.twelvedata.com"
    daily_credit_limit: PositiveInt = 800
    per_minute_credit_limit: PositiveInt = 8
    request_timeout_seconds: float = Field(default=15.0, gt=0, le=120)
    poll_outside_sessions: bool = False


class FeedConfig(_Model):
    kind: Literal["synthetic", "csv", "twelvedata"] = "synthetic"
    poll_delay_seconds: float = Field(default=5.0, ge=0, le=300)
    max_staleness_bars: int = Field(default=3, ge=1, le=1000)
    synthetic: SyntheticFeedConfig = SyntheticFeedConfig()
    csv: CsvFeedConfig = CsvFeedConfig()
    twelvedata: TwelveDataConfig = TwelveDataConfig()


class PaperConfig(_Model):
    slippage_pips: float = Field(default=0.2, ge=0, le=100)


class UniTraderConfig(_Model):
    layout_path: Path = Path("config/unitrader.layout.yaml")
    backend: Literal["win32", "fake"] = "win32"
    template_threshold: float = Field(default=0.92, gt=0, le=1)
    focus_retries: int = Field(default=3, ge=1, le=20)
    focus_retry_delay_seconds: float = Field(default=0.3, ge=0, le=5)
    action_delay_seconds: float = Field(default=0.08, ge=0, le=2)
    lot_readback: Literal["clipboard", "none"] = "clipboard"
    commit_key: Literal["tab", "enter", "none"] = "tab"
    decimal_separator: Literal[".", ","] = "."
    fill_timeout_seconds: float = Field(default=8.0, gt=0, le=120)
    fill_poll_interval_seconds: float = Field(default=0.25, gt=0, le=10)
    change_threshold: float = Field(default=0.02, gt=0, le=1)  # fraction of indicator pixels that changed
    stability_check: bool = True
    dialog_timeout_seconds: float = Field(default=3.0, ge=0, le=60)
    protective_orders: Literal["panel_fields", "none"] = "none"
    protective_units: Literal["price", "pips"] = "price"
    exit_method: Literal["close_all", "opposite_order"] = "close_all"
    netting_account_confirmed: bool = False
    allow_unprotected_live: bool = False
    save_evidence: bool = True
    max_order_seconds: float = Field(default=25.0, gt=1, le=300)
    health_interval_seconds: float = Field(default=30.0, ge=5, le=3600)

    @model_validator(mode="after")
    def _order_budget(self) -> Self:
        needed = self.fill_timeout_seconds + self.dialog_timeout_seconds
        if self.max_order_seconds <= needed:
            raise ValueError(
                f"max_order_seconds ({self.max_order_seconds:g}) must exceed fill_timeout_seconds + "
                f"dialog_timeout_seconds ({needed:g}); otherwise the venue never has time to confirm a click"
            )
        return self

    @model_validator(mode="after")
    def _field_entry(self) -> Self:
        problem = self.field_entry_problem()
        if problem is not None:
            raise ValueError(problem)
        return self

    def field_entry_problem(self) -> str | None:
        """Why ``lot_readback`` + ``commit_key`` cannot be verified safely, or None (the venue re-checks it)."""
        if self.lot_readback == "clipboard" and self.commit_key == "none":
            return (
                "lot_readback: clipboard needs commit_key: tab or enter. The desk reads every field back after "
                "the terminal committed it; without a commit key the value is only committed when the BUY/SELL "
                "click leaves the field, and nothing could verify it"
            )
        if self.lot_readback == "none" and self.commit_key == "enter":
            return (
                "commit_key: enter needs lot_readback: clipboard. Enter goes to whatever has the keyboard focus "
                "(e.g. the default button of a dialog) and without the read-back nothing proves the lot field had it"
            )
        return None


class ExecutionConfig(_Model):
    venue: Literal["paper", "unitrader"] = "paper"
    dry_run: bool = False
    paper: PaperConfig = PaperConfig()
    unitrader: UniTraderConfig = UniTraderConfig()


class DashboardConfig(_Model):
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1, le=65535)
    allowed_hosts: list[str] = Field(default_factory=lambda: ["127.0.0.1", "localhost"])
    allow_manual_meetings: bool = True
    max_auth_failures: int = Field(default=10, ge=1, le=1000)
    auth_failure_window_seconds: int = Field(default=600, ge=10, le=86_400)


class NotifyConfig(_Model):
    ntfy_server: str = "https://ntfy.sh"
    min_interval_seconds: int = Field(default=300, ge=0, le=86_400)  # per dedup key
    request_timeout_seconds: float = Field(default=10.0, gt=0, le=60)


class Secrets(_Model):
    twelvedata_api_key: SecretStr | None = None
    dashboard_token: SecretStr | None = None
    ntfy_topic: SecretStr | None = None
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None
    live_ack: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Secrets:
        def get(name: str) -> str | None:
            value = env.get(name, "").strip()
            return value or None

        def secret(name: str) -> SecretStr | None:
            value = get(name)
            return SecretStr(value) if value else None

        return cls(
            twelvedata_api_key=secret("TWELVEDATA_API_KEY"),
            dashboard_token=secret("DESK_DASHBOARD_TOKEN"),
            ntfy_topic=secret("DESK_NTFY_TOPIC"),
            telegram_bot_token=secret("DESK_TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=get("DESK_TELEGRAM_CHAT_ID"),
            live_ack=get("DESK_LIVE_ACK"),
        )


def _is_loopback(host: str) -> bool:
    if host in ("localhost",):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class DeskConfig(_Model):
    mode: Mode = Mode.PAPER
    data_dir: Path = Path("data")
    instruments: list[InstrumentConfig] = Field(min_length=1)
    strategy: StrategyConfig = StrategyConfig()
    risk: RiskConfig
    news: NewsConfig = NewsConfig()
    llm: LlmConfig = LlmConfig()
    feed: FeedConfig = FeedConfig()
    execution: ExecutionConfig = ExecutionConfig()
    dashboard: DashboardConfig = DashboardConfig()
    notify: NotifyConfig = NotifyConfig()
    secrets: Secrets = Field(default_factory=Secrets, exclude=True)
    _root_dir: Path = PrivateAttr(default_factory=Path.cwd)

    @model_validator(mode="after")
    def _safety(self) -> Self:
        problems: list[str] = []
        symbols = [i.symbol for i in self.instruments]
        if len(set(symbols)) != len(symbols):
            problems.append("instrument symbols must be unique")
        if not any(i.enabled for i in self.instruments):
            problems.append("at least one instrument must be enabled")

        if self.mode is Mode.PAPER and self.execution.venue != "paper":
            problems.append("mode 'paper' requires execution.venue: paper")
        if self.mode in (Mode.DEMO, Mode.LIVE) and self.execution.venue != "unitrader":
            problems.append(f"mode '{self.mode}' requires execution.venue: unitrader")
        if self.mode is Mode.LIVE:
            if self.secrets.live_ack != LIVE_ACK_PHRASE:
                problems.append(f"mode 'live' requires the environment variable DESK_LIVE_ACK={LIVE_ACK_PHRASE}")
            if self.execution.unitrader.backend != "win32":
                problems.append("mode 'live' requires execution.unitrader.backend: win32")
            ut = self.execution.unitrader
            if ut.protective_orders == "none" and not ut.allow_unprotected_live:
                problems.append(
                    "mode 'live' with protective_orders: none leaves positions without a broker-side stop if "
                    "the desk stops; set protective_orders: panel_fields or allow_unprotected_live: true"
                )

        ut = self.execution.unitrader
        simulated = self.mode is Mode.DEMO and ut.backend == "fake"
        if self.mode in (Mode.DEMO, Mode.LIVE) and self.feed.kind != "twelvedata" and not simulated:
            problems.append(
                f"mode '{self.mode}' clicks a real terminal and needs real market data: set feed.kind: twelvedata "
                f"(and TWELVEDATA_API_KEY), not '{self.feed.kind}'"
            )
        if self.execution.venue == "unitrader":
            if ut.exit_method == "close_all" and self.risk.max_open_positions != 1:
                problems.append("exit_method 'close_all' can only target one position: set risk.max_open_positions: 1")
            if ut.exit_method == "opposite_order" and ut.protective_orders == "panel_fields":
                problems.append(
                    "exit_method 'opposite_order' can't be combined with protective_orders 'panel_fields': the "
                    "closing order would carry the panel's SL/TP fields, which the desk can't verify are empty"
                )
            if ut.exit_method == "opposite_order" and not ut.netting_account_confirmed:
                problems.append(
                    "exit_method 'opposite_order' only closes positions on a netting account (on a hedging "
                    "account it opens a second position); confirm with netting_account_confirmed: true"
                )

        if self.risk.max_positions_per_symbol > self.risk.max_open_positions:
            problems.append("risk.max_positions_per_symbol cannot exceed risk.max_open_positions")
        for inst in self.instruments:
            if inst.quote_currency != self.risk.account_currency and inst.quote_to_account_rate == 1.0:
                problems.append(
                    f"{inst.symbol}: quote currency {inst.quote_currency} differs from account currency "
                    f"{self.risk.account_currency}; set quote_to_account_rate"
                )

        if self.feed.kind == "csv" and self.feed.csv.path is None:
            problems.append("feed.kind 'csv' requires feed.csv.path")

        if self.dashboard.enabled and not _is_loopback(self.dashboard.host):
            token = self.secrets.dashboard_token
            if token is None or len(token.get_secret_value()) < 24:
                problems.append(
                    "the dashboard listens on a non-loopback address; set DESK_DASHBOARD_TOKEN "
                    "(at least 24 random characters)"
                )

        if problems:
            raise ValueError("; ".join(problems))
        return self

    @property
    def enabled_instruments(self) -> list[InstrumentConfig]:
        return [i for i in self.instruments if i.enabled]

    def instrument(self, symbol: str) -> InstrumentConfig:
        for inst in self.instruments:
            if inst.symbol == symbol:
                return inst
        raise KeyError(symbol)

    @property
    def root_dir(self) -> Path:
        """Directory that relative paths in the config resolve against (see :func:`load_config`)."""
        return self._root_dir

    def resolve_path(self, path: Path) -> Path:
        return path if path.is_absolute() else (self._root_dir / path)

    def with_root(self, root: Path) -> DeskConfig:
        self._root_dir = root.resolve()
        return self


def load_config(path: Path | str, env: Mapping[str, str] | None = None) -> DeskConfig:
    """Load and validate the YAML config; raise :class:`ConfigError` with an operator-readable message."""
    path = Path(path)
    env = os.environ if env is None else env
    try:
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"config file {path} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"config file {path} must contain a mapping at the top level")
    if "secrets" in raw:
        raise ConfigError("secrets do not belong in the config file; use environment variables")
    raw["secrets"] = Secrets.from_env(env)
    try:
        cfg = DeskConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(path, exc)) from exc
    return cfg.with_root(project_root(path))


def project_root(config_path: Path | str) -> Path:
    """Where relative paths (and ``.env``) live: the parent of a ``config/`` directory holding the config
    file, otherwise the config file's own directory."""
    folder = Path(config_path).resolve().parent
    return folder.parent if folder.name == "config" else folder


def load_dotenv(path: Path, env: MutableMapping[str, str]) -> list[str]:
    """Load ``KEY=VALUE`` lines from ``path`` into ``env`` without overriding variables already set.

    Blank values, ``#`` comments and lines without ``=`` are skipped; one pair of surrounding quotes is
    removed. Returns the names that were set (never the values).
    """
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    loaded: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.removeprefix("export ").partition("=")
        name, value = name.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if not name or not value or name in env:
            continue
        env[name] = value
        loaded.append(name)
    return loaded


def _format_validation_error(path: Path, exc: ValidationError) -> str:
    lines = [f"invalid config {path}:"]
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "(top level)"
        lines.append(f"  - {loc}: {err['msg']}")
    return "\n".join(lines)
