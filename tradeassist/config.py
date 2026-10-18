"""Settings, read once from the environment (and a .env file if present)."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from .features import FEATURE_DEFAULTS, load_features
from .freshness import DEFAULT_SECONDS as FRESHNESS_DEFAULTS
from .freshness import thresholds as freshness_thresholds

ROOT = Path(__file__).resolve().parent.parent

DEFAULT_WATCHLIST = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "BRK.B", "JPM", "JNJ",
    "V", "PG", "MA", "HD", "CVX", "MRK", "ABBV", "PEP", "KO", "COST",
]


def _clean_token(value: str | None) -> str:
    """Treat empty values and the placeholders from env.example as 'no token'."""
    if not value:
        return ""
    value = value.strip()
    if not value or value.lower().startswith("your_") or value.endswith("_here"):
        return ""
    return value


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc
    if not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number, got {raw!r}")
    return value


def _int(name: str, default: int) -> int:
    return int(_float(name, default))


def _flag(name: str, default: bool = False) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"{name} must be 1 or 0, got {raw!r}")


def _choice(name: str, default: str, allowed: tuple[str, ...]) -> str:
    raw = (os.getenv(name) or "").strip().lower() or default
    if raw not in allowed:
        raise ValueError(f"{name} must be one of {', '.join(allowed)}, got {raw!r}")
    return raw


@dataclass(frozen=True)
class Settings:
    live_token: str = field(default="", repr=False)
    sandbox_token: str = field(default="", repr=False)
    data_mode: str = "auto"  # auto | live | sandbox | demo | robinhood
    db_path: Path = ROOT / "data" / "tradeassist.db"

    # Account and hard limits (percent values are percentages, e.g. 2.0 = 2%)
    account_size: float = 10_000.0
    max_risk_per_trade_pct: float = 2.0
    max_open_risk_pct: float = 10.0
    daily_loss_stop_pct: float = 3.0
    # REALIZED_PLUS_UNREALIZED counts today's change in open positions toward the daily stop;
    # REALIZED_ONLY counts closed trades only.
    daily_loss_mode: str = "REALIZED_PLUS_UNREALIZED"
    # What the trade ticket does when today's loss or the portfolio's risk cannot be fully measured:
    # WARN (default) says so beside the result and never calls the ticket "inside all of your
    # limits"; BLOCK also makes it a limit the ticket breaks, which you can override on purpose.
    incomplete_risk_policy: str = "WARN"
    max_spread_pct: float = 10.0
    min_dte: int = 7

    # Model inputs
    risk_free_rate: float = 0.04
    stock_cost_bps: float = 5.0  # per side, used by the pattern lab and backtester

    # SEC EDGAR requires a descriptive User-Agent with contact details.
    sec_user_agent: str = ""

    watchlist: list[str] = field(default_factory=lambda: list(DEFAULT_WATCHLIST))

    # AI briefs and news (2.4). The keys never leave the server and never appear in logs.
    anthropic_api_key: str = field(default="", repr=False)
    ai_model: str = "claude-sonnet-5-5"
    ai_web_search: bool = True
    ai_daily_requests: int = 40
    finnhub_api_key: str = field(default="", repr=False)

    # Robinhood data connection (DATA_MODE=robinhood)
    robinhood_callback_port: int = 8421
    robinhood_poll_seconds: float = 3.0
    robinhood_url: str = "https://agent.robinhood.com/mcp/trading"
    # 2.6: read your holdings and past orders through the same connection (off unless ROBINHOOD_ACCOUNT_READ=1)
    robinhood_account_read: bool = False

    # 2.8: the morning email. SMTP settings; with Gmail, SMTP_PASSWORD is an app password
    # (myaccount.google.com/apppasswords, needs 2-Step Verification). The password never leaves this
    # computer and never appears in logs or replies.
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = field(default="", repr=False)
    email_to: str = ""                 # where the morning plan goes; SMTP_USER when empty
    # 2.8: the limit-price cap: reaching target 1 must pay at least this many times what the stop loses.
    morning_min_payoff: float = 1.0

    # 2.10: live news and moves. RTPR (rtpr.io, a paid real-time press-release feed) is optional; SEC 8-K filings
    # (SEC_USER_AGENT) and Finnhub headlines (FINNHUB_API_KEY) are used when set. The key never leaves the server.
    rtpr_api_key: str = field(default="", repr=False)
    live_universe: str = "large"            # large: the ~330 large stocks plus watchlist and holdings; all: any ticker
    live_min_reaction_pct: float = 1.0      # a move since the news of at least this percent...
    live_min_reaction_atr: float = 0.3      # ...and this many of the stock's daily ranges
    live_min_day_atr: float = 1.5           # when the news reached us late: the day's move in daily ranges...
    live_min_day_pct: float = 3.0           # ...and in percent
    live_news_email: bool = False           # email each new opportunity and each alert on a stock you hold
    live_news_autostart: bool = False       # start listening when the app starts
    # 2.11: news found by searching. Google News search feeds are free (no key); Brave is optional, capped so it
    # stays inside the $5 monthly credit that comes with a key. Movers are scanned this often while trading.
    google_news: bool = True
    brave_api_key: str = field(default="", repr=False)
    brave_monthly_limit: int = 900
    brave_daily_limit: int = 40
    live_mover_sweep_minutes: float = 5.0   # 0 switches the "why is it moving?" scan off

    # V2 areas that can be switched off (see features.py)
    features: dict[str, bool] = field(default_factory=lambda: dict(FEATURE_DEFAULTS))
    # Seconds a price may age while the market is open before it is called stale (freshness.py)
    freshness: dict[str, float] = field(default_factory=lambda: dict(FRESHNESS_DEFAULTS))

    @property
    def robinhood_signin_path(self) -> Path:
        """Where the Robinhood sign-in is kept: beside the database, readable only by you."""
        return self.db_path.parent / "robinhood_signin.json"

    @property
    def resolved_mode(self) -> str:
        mode = self.data_mode.lower()
        if mode == "auto":
            if self.live_token:
                return "live"
            if self.sandbox_token:
                return "sandbox"
            return "demo"
        if mode == "live" and not self.live_token:
            raise ValueError("DATA_MODE=live needs TRADIER_LIVE_TOKEN")
        if mode == "sandbox" and not self.sandbox_token:
            raise ValueError("DATA_MODE=sandbox needs TRADIER_SANDBOX_TOKEN")
        if mode not in {"live", "sandbox", "demo", "robinhood"}:
            raise ValueError(f"DATA_MODE must be auto, live, sandbox, demo or robinhood, got {self.data_mode!r}")
        return mode


def load_settings(env_file: str | os.PathLike | None = None) -> Settings:
    """Build Settings from the environment. `.env` in the project root is loaded if present."""
    load_dotenv(env_file or ROOT / ".env", override=False)

    watch_raw = os.getenv("WATCHLIST", "")
    watchlist = [s.strip().upper() for s in watch_raw.split(",") if s.strip()] or list(DEFAULT_WATCHLIST)

    db_raw = os.getenv("TRADEASSIST_DB", "")
    db_path = Path(db_raw).expanduser() if db_raw else ROOT / "data" / "tradeassist.db"
    if not db_path.is_absolute():
        db_path = ROOT / db_path

    loss_mode = (os.getenv("DAILY_LOSS_MODE") or "REALIZED_PLUS_UNREALIZED").strip().upper()
    if loss_mode not in ("REALIZED_PLUS_UNREALIZED", "REALIZED_ONLY"):
        raise ValueError(f"DAILY_LOSS_MODE must be REALIZED_PLUS_UNREALIZED or REALIZED_ONLY, got {loss_mode!r}")

    policy = (os.getenv("INCOMPLETE_RISK_POLICY") or "WARN").strip().upper()
    if policy not in ("WARN", "BLOCK"):
        raise ValueError(f"INCOMPLETE_RISK_POLICY must be WARN or BLOCK, got {policy!r}")
    settings = Settings(
        live_token=_clean_token(os.getenv("TRADIER_LIVE_TOKEN")),
        sandbox_token=_clean_token(os.getenv("TRADIER_SANDBOX_TOKEN")),
        data_mode=os.getenv("DATA_MODE", "auto").strip() or "auto",
        db_path=db_path,
        account_size=_float("ACCOUNT_SIZE", 10_000.0),
        max_risk_per_trade_pct=_float("MAX_RISK_PER_TRADE_PCT", 2.0),
        max_open_risk_pct=_float("MAX_OPEN_RISK_PCT", 10.0),
        daily_loss_stop_pct=_float("DAILY_LOSS_STOP_PCT", 3.0),
        daily_loss_mode=loss_mode,
        incomplete_risk_policy=policy,
        max_spread_pct=_float("MAX_SPREAD_PCT", 10.0),
        min_dte=_int("MIN_DTE", 7),
        risk_free_rate=_float("RISK_FREE_RATE", 0.04),
        stock_cost_bps=_float("STOCK_COST_BPS", 5.0),
        sec_user_agent=_clean_token(os.getenv("SEC_USER_AGENT")),
        anthropic_api_key=_clean_token(os.getenv("ANTHROPIC_API_KEY")),
        ai_model=(os.getenv("AI_MODEL") or "").strip() or "claude-sonnet-5-5",
        ai_web_search=(os.getenv("AI_WEB_SEARCH") or "1").strip().lower() not in ("0", "false", "no", "off"),
        ai_daily_requests=max(_int("AI_DAILY_REQUESTS", 40), 0),
        finnhub_api_key=_clean_token(os.getenv("FINNHUB_API_KEY")),
        watchlist=watchlist,
        robinhood_callback_port=_int("ROBINHOOD_CALLBACK_PORT", 8421),
        robinhood_poll_seconds=max(_float("ROBINHOOD_POLL_SECONDS", 3.0), 1.0),
        robinhood_url=(os.getenv("ROBINHOOD_MCP_URL") or "").strip() or "https://agent.robinhood.com/mcp/trading",
        robinhood_account_read=(os.getenv("ROBINHOOD_ACCOUNT_READ") or "0").strip().lower() in ("1", "true", "yes", "on"),
        smtp_host=(os.getenv("SMTP_HOST") or "").strip() or "smtp.gmail.com",
        smtp_port=_int("SMTP_PORT", 587),
        smtp_user=_clean_token(os.getenv("SMTP_USER")),
        smtp_password=(os.getenv("SMTP_PASSWORD") or "").strip(),
        email_to=_clean_token(os.getenv("EMAIL_TO")),
        morning_min_payoff=_float("MORNING_MIN_PAYOFF", 1.0),
        rtpr_api_key=_clean_token(os.getenv("RTPR_API_KEY")),
        live_universe=_choice("LIVE_UNIVERSE", "large", ("large", "all")),
        live_min_reaction_pct=_float("LIVE_MIN_REACTION_PCT", 1.0),
        live_min_reaction_atr=_float("LIVE_MIN_REACTION_ATR", 0.3),
        live_min_day_atr=_float("LIVE_MIN_DAY_ATR", 1.5),
        live_min_day_pct=_float("LIVE_MIN_DAY_PCT", 3.0),
        live_news_email=_flag("LIVE_NEWS_EMAIL"),
        live_news_autostart=_flag("LIVE_NEWS_AUTOSTART"),
        google_news=_flag("GOOGLE_NEWS", True),
        brave_api_key=_clean_token(os.getenv("BRAVE_API_KEY")),
        brave_monthly_limit=max(_int("BRAVE_MONTHLY_LIMIT", 900), 0),
        brave_daily_limit=max(_int("BRAVE_DAILY_LIMIT", 40), 0),
        live_mover_sweep_minutes=max(_float("LIVE_MOVER_SWEEP_MINUTES", 5.0), 0.0),
        features=load_features(),
        freshness=freshness_thresholds(),
    )
    for name, value in (("ACCOUNT_SIZE", settings.account_size), ("MAX_RISK_PER_TRADE_PCT", settings.max_risk_per_trade_pct),
                        ("MAX_OPEN_RISK_PCT", settings.max_open_risk_pct),
                        ("DAILY_LOSS_STOP_PCT", settings.daily_loss_stop_pct),
                        ("LIVE_MIN_REACTION_PCT", settings.live_min_reaction_pct),
                        ("LIVE_MIN_REACTION_ATR", settings.live_min_reaction_atr),
                        ("LIVE_MIN_DAY_ATR", settings.live_min_day_atr), ("LIVE_MIN_DAY_PCT", settings.live_min_day_pct)):
        if value <= 0:
            raise ValueError(f"{name} must be above zero, got {value:g}")
    return settings
