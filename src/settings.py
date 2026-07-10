"""Central typed runtime settings — the single inventory of every env knob.

Every tunable the pipeline reads from the environment is declared here, once,
with its type, default, and (where one exists) legacy alias. Modules bind
their constants from ``settings()`` instead of parsing ``os.environ``
themselves, so:

- there is ONE greppable inventory of all knobs (this file);
- a malformed value fails fast with an error naming the offending variable,
  instead of a bare ValueError at whichever import happens to run first;
- duplicated reads (DEEPSEEK_BASE_URL used to be parsed in four modules)
  cannot drift apart.

Deliberately NOT here:
- ``APPLICANT_*`` (personal profile facts, ~30 fields) — they live in
  `applicant_profile.py`, already centralized and typed there.
- Filesystem layout (PROJECT_ROOT, LOG_DIR, ...) — that is `config.py`;
  paths are structure, not tuning. The one env-driven path (DOCS_DIR) is
  declared here and consumed by config.py.

Print the resolved settings:  uv run python -m src.settings
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, fields


class SettingsError(RuntimeError):
    """A malformed environment value; the message names the variable."""


def _raw(env: Mapping[str, str], name: str, *legacy: str) -> str | None:
    for key in (name, *legacy):
        value = env.get(key)
        if value is not None:
            return value
    return None


def _str(env: Mapping[str, str], name: str, default: str, *legacy: str) -> str:
    value = _raw(env, name, *legacy)
    return default if value is None else value


def _int(env: Mapping[str, str], name: str, default: int, *legacy: str) -> int:
    value = _raw(env, name, *legacy)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        raise SettingsError(f"{name}: expected an integer, got {value!r}") from None


def _float(env: Mapping[str, str], name: str, default: float, *legacy: str) -> float:
    value = _raw(env, name, *legacy)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        raise SettingsError(f"{name}: expected a number, got {value!r}") from None


def _flag(env: Mapping[str, str], name: str, default: bool, *legacy: str) -> bool:
    """The codebase's existing flag convention: only "0" disables."""
    value = _raw(env, name, *legacy)
    if value is None:
        return default
    return value != "0"


def _csv(env: Mapping[str, str], name: str, default: tuple[str, ...],
         *legacy: str) -> tuple[str, ...]:
    value = _raw(env, name, *legacy)
    if value is None:
        return default
    return tuple(s.strip() for s in value.split(",") if s.strip())


# Self-improvement knobs keep their RECOVERY_* compatibility aliases.
def _si_legacy(name: str) -> str:
    return "RECOVERY_" + name.removeprefix("SELF_IMPROVEMENT_")


@dataclass(frozen=True)
class Settings:
    # --- DeepSeek API (shared by apply agent, judge, playbooks, healthcheck)
    deepseek_api_key: str | None
    deepseek_base_url: str

    # --- Apply agent (src/apply.py, src/browser_agent.py)
    apply_model: str
    apply_max_turns: int
    apply_timeout_seconds: int
    apply_fastpath_enabled: bool
    apply_reasoning_effort: str      # off | low | medium | high | max
    apply_max_tokens: int
    apply_prune_min_chars: int
    apply_prune_keep_recent: int     # >= 1
    apply_grace_turns: int
    apply_auto_cookie: bool
    apply_teardown_grace_seconds: int
    apply_trajectory_enabled: bool
    apply_priority_stale_seconds: int
    google_account: str

    # --- Orchestrator
    watch_retry_seconds: int

    # --- Rent cap (shared by poller filter and apply pre-flight)
    max_rent: float

    # --- Poller
    poll_tier3_settle_ms: int
    poll_executor_threads: int
    poll_tier3_lock_timeout: float
    poll_tier3_render_timeout: float
    poll_tier3_close_timeout: float
    poll_zero_yield_alert_polls: int
    poll_min_price: float
    poll_min_surface: float
    poll_require_known_price: bool
    poll_cities: tuple[str, ...]
    poll_min_stay_months: int
    poll_judge_model: str
    poll_max_cycling_min: int
    poll_enable_tier3: bool
    listing_claim_ttl_seconds: int
    browser_lock_wait_alert_seconds: float

    # --- Notifications
    notify_to: str                   # placeholder default means "not configured"
    notify_enabled_flag: bool        # raw NOTIFY_ENABLED; notify.py adds the placeholder guard
    web_push_enabled: bool
    web_push_outcomes: frozenset[str]

    # --- Healthcheck / digest
    credit_currency: str
    credit_threshold: float
    server_ssh_hint: str
    healthcheck_services: tuple[str, ...]
    healthcheck_ping_url: str
    healthcheck_site_probes_json: str  # raw JSON; healthcheck parses fail-open
    self_improvement_health_window: int
    digest_interval_days: float

    # --- Site playbooks
    playbook_model: str              # defaults to apply_model
    playbook_max_chars: int
    playbook_timeout_seconds: int
    playbook_max_items: int

    # --- LLM pricing overrides (raw strings; llm_pricing parses fail-open)
    llm_model_prices_json: str
    llm_input_usd_per_1m: str | None
    llm_cached_input_usd_per_1m: str | None
    llm_output_usd_per_1m: str | None

    # --- Self-improvement harness / incidents
    self_improvement_eval_fixtures: str | None   # None -> harness default dir
    apply_harness_eval_fixtures: str | None
    self_improvement_dedup_hours: float

    # --- Dashboard
    dashboard_warm_interval_seconds: float

    # --- Self-improvement agent (RECOVERY_* legacy aliases still honored)
    self_improvement_enabled: bool
    self_improvement_base_url: str
    self_improvement_proxy_model: str
    self_improvement_max_turns: int
    self_improvement_diagnosis_max_turns: int
    self_improvement_max_budget_usd: float
    self_improvement_timeout_seconds: int
    self_improvement_verify_cmd: str
    self_improvement_allow_code_changes: bool
    self_improvement_allow_deploy: bool
    self_improvement_proposal_candidates: int
    self_improvement_outcomes: frozenset[str]

    # --- Paths (the one env-driven one; config.py consumes it)
    docs_dir: str | None


DEFAULT_SELF_IMPROVEMENT_OUTCOMES = frozenset({
    "blocked", "error", "incomplete", "login_required",
    "no_source_url", "not_available", "timeout", "unknown",
})


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    e: Mapping[str, str] = os.environ if env is None else env

    apply_model = _str(e, "APPLY_MODEL", "deepseek-v4-pro")
    reasoning = _str(e, "APPLY_REASONING_EFFORT", "off").lower()
    if reasoning == "minimal":
        reasoning = "low"

    return Settings(
        deepseek_api_key=_raw(e, "DEEPSEEK_API_KEY"),
        deepseek_base_url=_str(e, "DEEPSEEK_BASE_URL", "https://api.deepseek.com"),

        apply_model=apply_model,
        apply_max_turns=_int(e, "APPLY_MAX_TURNS", 60),
        apply_timeout_seconds=_int(e, "APPLY_TIMEOUT_SECONDS", 900),
        apply_fastpath_enabled=_flag(e, "APPLY_FASTPATH_ENABLED", True),
        apply_reasoning_effort=reasoning,
        apply_max_tokens=_int(e, "APPLY_MAX_TOKENS", 8000),
        apply_prune_min_chars=_int(e, "APPLY_PRUNE_MIN_CHARS", 2500),
        apply_prune_keep_recent=max(1, _int(e, "APPLY_PRUNE_KEEP_RECENT", 2)),
        apply_grace_turns=_int(e, "APPLY_GRACE_TURNS", 10),
        apply_auto_cookie=_flag(e, "APPLY_AUTO_COOKIE", True),
        apply_teardown_grace_seconds=_int(e, "APPLY_TEARDOWN_GRACE_SECONDS", 120),
        apply_trajectory_enabled=_flag(e, "APPLY_TRAJECTORY_ENABLED", True),
        apply_priority_stale_seconds=_int(e, "APPLY_PRIORITY_STALE_SECONDS", 1800),
        google_account=_str(e, "GOOGLE_ACCOUNT", "you@example.com"),

        watch_retry_seconds=_int(e, "WATCH_RETRY_SECONDS", 300),

        max_rent=_float(e, "MAX_RENT", 1750.0, "POLL_MAX_PRICE"),

        poll_tier3_settle_ms=_int(e, "POLL_TIER3_SETTLE_MS", 5500),
        poll_executor_threads=_int(e, "POLL_EXECUTOR_THREADS", 64),
        poll_tier3_lock_timeout=_float(e, "POLL_TIER3_LOCK_TIMEOUT", 120.0),
        poll_tier3_render_timeout=_float(e, "POLL_TIER3_RENDER_TIMEOUT", 120.0),
        poll_tier3_close_timeout=_float(e, "POLL_TIER3_CLOSE_TIMEOUT", 5.0),
        poll_zero_yield_alert_polls=_int(e, "POLL_ZERO_YIELD_ALERT_POLLS", 120),
        poll_min_price=_float(e, "POLL_MIN_PRICE", 800.0),
        poll_min_surface=_float(e, "POLL_MIN_SURFACE", 30.0),
        poll_require_known_price=_flag(e, "POLL_REQUIRE_KNOWN_PRICE", True),
        poll_cities=tuple(c.lower() for c in _csv(e, "POLL_CITIES", ("utrecht",))),
        poll_min_stay_months=_int(e, "POLL_MIN_STAY_MONTHS", 6),
        poll_judge_model=_str(e, "POLL_JUDGE_MODEL", "deepseek-v4-pro"),
        poll_max_cycling_min=_int(e, "POLL_MAX_CYCLING_MIN", 15),
        poll_enable_tier3=e.get("POLL_ENABLE_TIER3", "0") == "1",
        listing_claim_ttl_seconds=_int(e, "LISTING_CLAIM_TTL_SECONDS", 7200),
        browser_lock_wait_alert_seconds=_float(
            e, "BROWSER_LOCK_WAIT_ALERT_SECONDS", 300.0),

        notify_to=_str(e, "NOTIFY_TO", "you@example.com"),
        notify_enabled_flag=_flag(e, "NOTIFY_ENABLED", True),
        web_push_enabled=_flag(e, "WEB_PUSH_ENABLED", True),
        web_push_outcomes=frozenset(
            _csv(e, "WEB_PUSH_OUTCOMES", ("submitted",))),

        credit_currency=_str(e, "CREDIT_CURRENCY", "USD").upper(),
        credit_threshold=_float(e, "CREDIT_THRESHOLD", 2.0, "CREDIT_THRESHOLD_USD"),
        server_ssh_hint=_str(e, "SERVER_SSH", "root@your-server-ip"),
        healthcheck_services=_csv(
            e, "HEALTHCHECK_SERVICES",
            ("orchestrator", "poller", "browser-host", "litellm-proxy")),
        healthcheck_ping_url=_str(e, "HEALTHCHECK_PING_URL", ""),
        healthcheck_site_probes_json=_str(e, "HEALTHCHECK_SITE_PROBES", "{}"),
        self_improvement_health_window=_int(e, "SELF_IMPROVEMENT_HEALTH_WINDOW", 5),
        digest_interval_days=_float(e, "DIGEST_INTERVAL_DAYS", 7.0),

        playbook_model=_str(e, "PLAYBOOK_MODEL", apply_model),
        playbook_max_chars=_int(e, "PLAYBOOK_MAX_CHARS", 4000),
        playbook_timeout_seconds=_int(e, "PLAYBOOK_TIMEOUT_SECONDS", 120),
        playbook_max_items=_int(e, "PLAYBOOK_MAX_ITEMS", 40),

        llm_model_prices_json=_str(e, "LLM_MODEL_PRICES_JSON", ""),
        llm_input_usd_per_1m=_raw(e, "LLM_INPUT_USD_PER_1M"),
        llm_cached_input_usd_per_1m=_raw(e, "LLM_CACHED_INPUT_USD_PER_1M"),
        llm_output_usd_per_1m=_raw(e, "LLM_OUTPUT_USD_PER_1M"),

        self_improvement_eval_fixtures=_raw(e, "SELF_IMPROVEMENT_EVAL_FIXTURES"),
        apply_harness_eval_fixtures=_raw(e, "APPLY_HARNESS_EVAL_FIXTURES"),
        self_improvement_dedup_hours=_float(
            e, "SELF_IMPROVEMENT_DEDUP_HOURS", 24.0),

        dashboard_warm_interval_seconds=_float(
            e, "DASHBOARD_WARM_INTERVAL_SECONDS", 300.0),

        self_improvement_enabled=_flag(
            e, "SELF_IMPROVEMENT_ENABLED", True,
            _si_legacy("SELF_IMPROVEMENT_ENABLED")),
        self_improvement_base_url=_str(
            e, "SELF_IMPROVEMENT_BASE_URL", "http://127.0.0.1:4000",
            _si_legacy("SELF_IMPROVEMENT_BASE_URL")),
        self_improvement_proxy_model=_str(
            e, "SELF_IMPROVEMENT_PROXY_MODEL", "self-improvement-deepseek",
            _si_legacy("SELF_IMPROVEMENT_PROXY_MODEL")),
        self_improvement_max_turns=_int(
            e, "SELF_IMPROVEMENT_MAX_TURNS", 30,
            _si_legacy("SELF_IMPROVEMENT_MAX_TURNS")),
        self_improvement_diagnosis_max_turns=_int(
            e, "SELF_IMPROVEMENT_DIAGNOSIS_MAX_TURNS", 15,
            _si_legacy("SELF_IMPROVEMENT_DIAGNOSIS_MAX_TURNS")),
        self_improvement_max_budget_usd=_float(
            e, "SELF_IMPROVEMENT_MAX_BUDGET_USD", 40.0,
            _si_legacy("SELF_IMPROVEMENT_MAX_BUDGET_USD")),
        self_improvement_timeout_seconds=_int(
            e, "SELF_IMPROVEMENT_TIMEOUT_SECONDS", 1500,
            _si_legacy("SELF_IMPROVEMENT_TIMEOUT_SECONDS")),
        self_improvement_verify_cmd=_str(
            e, "SELF_IMPROVEMENT_VERIFY_CMD", "just check",
            _si_legacy("SELF_IMPROVEMENT_VERIFY_CMD")),
        self_improvement_allow_code_changes=_flag(
            e, "SELF_IMPROVEMENT_ALLOW_CODE_CHANGES", True,
            _si_legacy("SELF_IMPROVEMENT_ALLOW_CODE_CHANGES")),
        self_improvement_allow_deploy=_flag(
            e, "SELF_IMPROVEMENT_ALLOW_DEPLOY", True,
            _si_legacy("SELF_IMPROVEMENT_ALLOW_DEPLOY")),
        self_improvement_proposal_candidates=_int(
            e, "SELF_IMPROVEMENT_PROPOSAL_CANDIDATES", 2,
            _si_legacy("SELF_IMPROVEMENT_PROPOSAL_CANDIDATES")),
        self_improvement_outcomes=frozenset(
            _csv(e, "SELF_IMPROVEMENT_OUTCOMES",
                 tuple(sorted(DEFAULT_SELF_IMPROVEMENT_OUTCOMES)),
                 _si_legacy("SELF_IMPROVEMENT_OUTCOMES"))),

        docs_dir=_raw(e, "DOCS_DIR"),
    )


_settings: Settings | None = None


def settings() -> Settings:
    """The process-wide Settings, loaded once from os.environ on first use."""
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings


def reload_settings() -> Settings:
    """Re-read os.environ (test hook; production processes restart instead)."""
    global _settings
    _settings = load_settings()
    return _settings


_REDACTED_FIELDS = {"deepseek_api_key"}


def main() -> int:
    s = settings()
    for f in fields(s):
        value = getattr(s, f.name)
        if f.name in _REDACTED_FIELDS and value:
            value = "(set, redacted)"
        if isinstance(value, frozenset):
            value = ",".join(sorted(value))
        print(f"{f.name} = {value!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
