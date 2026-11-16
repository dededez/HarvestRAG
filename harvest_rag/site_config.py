"""Access V1's versioned JSON configuration from V2.

V2 originally hardcoded most fetch parameters and only used environment
variables. Restoring V1's behaviour means reusing the same schema-validated
config files instead of duplicating values:

    config/fetch.json          UA, timeouts, redirects, settle, pagination
    config/obstacles.json      cookie/popup/load-more/captcha/auth policies
    config/orchestrator.json   retry budget/backoff
    config/embeddings.json     embedding model + fallback
"""

from __future__ import annotations

from functools import lru_cache

_FETCH_DEFAULTS = {
    "rate_limit_min_interval_seconds": 1.0,
    "timeout_seconds": 30,
    "extended_timeout_seconds": 90,
    "empty_content_threshold_chars": 100,
    "user_agent": "WebScrapperV2/2.0",
    "max_redirects": 10,
    "browser_settle_seconds": 5,
    "pagination_max_clicks": 5,
    "backoff_base_seconds": 1.0,
}

_OBSTACLE_DEFAULTS = {
    "popup_modal": {"enabled": True, "policy": "dismiss", "detection_method": "detect_overlay_widget"},
    "cookie_banner": {"enabled": True, "policy": "dismiss", "detection_method": "detect_consent_banner"},
    "captcha_gate": {"enabled": True, "policy": "terminal_blocked", "detection_method": "detect_challenge_widget"},
    "server_errors": {"enabled": True, "policy": "retry_with_backoff", "max_retries": 3,
                      "detection_method": "http_status_5xx"},
    "rate_limit": {"enabled": True, "policy": "retry_with_backoff", "max_retries": 3,
                   "detection_method": "http_status_429"},
    "slow_responses": {"enabled": True, "policy": "extend_timeout", "detection_method": "response_time_exceeded"},
    "unexpected_redirect": {"enabled": True, "policy": "follow_and_log",
                            "detection_method": "redirect_chain_detected"},
    "dom_drift": {"enabled": True, "policy": "requery", "detection_method": "locator_mismatch"},
    "blocked_clicks": {"enabled": True, "policy": "retry_with_backoff", "max_retries": 2,
                       "detection_method": "click_no_op"},
    "data_integrity": {"enabled": True, "policy": "flag_and_skip",
                       "detection_method": "schema_validation_failed"},
    "stale_element": {"enabled": True, "policy": "requery", "detection_method": "stale_element_reference"},
    "session_expiry": {"enabled": False, "policy": "terminal_blocked",
                       "detection_method": "auth_challenge_detected"},
    "random_logout": {"enabled": False, "policy": "terminal_blocked",
                      "detection_method": "auth_redirect_detected"},
    "load_more": {"enabled": True, "policy": "paginate", "detection_method": "pagination_control_present"},
}


@lru_cache(maxsize=1)
def fetch_config() -> dict:
    try:
        from fetchers.config_loader import load_fetch_config
        return load_fetch_config()
    except Exception:
        return dict(_FETCH_DEFAULTS)


@lru_cache(maxsize=1)
def obstacle_config() -> dict:
    try:
        from fetchers.config_loader import load_obstacle_config
        return load_obstacle_config()
    except Exception:
        return {key: dict(value) for key, value in _OBSTACLE_DEFAULTS.items()}


@lru_cache(maxsize=1)
def orchestrator_config() -> dict:
    try:
        from orchestrator.config_loader import load_orchestrator_config
        return load_orchestrator_config()
    except Exception:
        return {"retry": {"max_attempts": 2, "backoff_seconds": 5}}


@lru_cache(maxsize=1)
def embeddings_config() -> dict:
    try:
        from pipeline.config_loader import load_pipeline_config
        return load_pipeline_config()
    except Exception:
        return {"embed": {"model": "BAAI/bge-m3",
                          "fallback_model": "all-MiniLM-L6-v2",
                          "auto_fallback_on_load_failure": True,
                          "batch_size": 32},
                "chunk": {"max_chunk_chars": 2000}}
