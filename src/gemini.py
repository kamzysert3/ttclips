"""Shared Gemini helpers: retry with backoff + model fallback chain + probe CLI.

Usage:
    python -m src.gemini --probe [model...]
"""
from __future__ import annotations

import logging
import random
import sys
import threading
import time

from src.config import load_env

log = logging.getLogger(__name__)
ENV = load_env()

MAX_ATTEMPTS = int(ENV.get("GEMINI_MAX_RETRIES", "6"))
BASE_DELAY = float(ENV.get("GEMINI_RETRY_DELAY", "3"))
MAX_DELAY = float(ENV.get("GEMINI_RETRY_MAX_DELAY", "45"))
RETRY_TIMEOUT = float(ENV.get("GEMINI_RETRY_TIMEOUT", "90"))

DEFAULT_MODEL = ENV.get("GEMINI_MODEL", "gemini-3.6-flash")
DEFAULT_FALLBACKS = [
    m.strip() for m in ENV.get("GEMINI_FALLBACK_MODELS", "").split(",") if m.strip()
] or ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]

TRANSIENT_HINTS = ("503", "429", "500", "408", "unavailable", "high demand",
                   "rate limit", "busy", "try again later", "temporarily")

MODEL_CANDIDATES = DEFAULT_FALLBACKS + [DEFAULT_MODEL]


def _transient(e: Exception) -> bool:
    msg = str(e).lower()
    return any(h in msg for h in TRANSIENT_HINTS)


# probe-healthy ordering cache
_models_cache = None


def resolve_models() -> list[str]:
    """Return candidate models in order of preference, probe-healthy first.

    Config order is [GEMINI_MODEL, *GEMINI_FALLBACK_MODELS]. A cheap bounded
    probe promotes models that respond OK so the healthiest is tried first.
    If the probe itself fails or stalls, the configured order is returned
    untouched.
    """
    global _models_cache
    if _models_cache is not None:
        return _models_cache

    ordered = [DEFAULT_MODEL] + [m for m in DEFAULT_FALLBACKS if m != DEFAULT_MODEL]
    try:
        results = probe_models(ordered, per_model_timeout=6.0)
        ok = [m for m, s in results if s == "ok"]
        _models_cache = ok + [m for m in ordered if m not in ok] if ok else ordered
    except Exception:
        _models_cache = ordered
    log.info("Resolved model order: %s", _models_cache)
    return _models_cache


def _probe_one(client, model: str) -> tuple[str, str]:
    try:
        client.models.generate_content(model=model, contents="ping")
        return (model, "ok")
    except Exception as e:
        status = "unavailable" if _transient(e) else "error"
        return (model, status)


def probe_models(models: list[str], *, per_model_timeout: float = 15.0) -> list[tuple[str, str]]:
    """Return [(model, status)] where status is 'ok', 'unavailable' or 'error'.

    Probes run concurrently on daemon threads; the caller waits at most
    ``per_model_timeout`` then treats stragglers as 'error'. Daemon threads
    never block process exit, so a stalled model cannot delay the pipeline.
    """
    from google import genai

    key = ENV.get("GEMINI_API_KEY", "")
    if not key:
        log.warning("GEMINI_API_KEY missing from .env — probe will report error")
        return [(m, "error") for m in models]

    client = genai.Client(api_key=key)
    results: dict[str, str] = {}
    lock = threading.Lock()

    def _run(m: str) -> None:
        _, status = _probe_one(client, m)
        with lock:
            results[m] = status

    for m in models:
        t = threading.Thread(target=_run, args=(m,), daemon=True)
        t.start()

    deadline = time.monotonic() + max(0.0, per_model_timeout)
    while True:
        with lock:
            missing = [m for m in models if m not in results]
        if not missing or time.monotonic() >= deadline:
            break
        time.sleep(0.05)
    with lock:
        for m in models:
            results.setdefault(m, "error")
    out = [(m, results[m]) for m in models]
    for model, status in out:
        log.info("Probe %-24s %s", model, status)
    return out


def call_with_retry(fn, *, attempts: int = MAX_ATTEMPTS, base_delay: float = BASE_DELAY,
                    max_delay: float = MAX_DELAY, timeout: float = RETRY_TIMEOUT):
    """Call ``fn()``, retrying with jittered exponential backoff on transient errors.

    ``timeout`` caps the total time spent retrying (across attempts and sleeps),
    so a persistently failing model cannot burn the whole slot. Permanent
    errors (e.g. 404 unknown model) propagate immediately.
    """
    deadline = time.monotonic() + max(0.0, timeout)
    delay = base_delay
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as e:
            is_transient = _transient(e)
            remaining = deadline - time.monotonic()
            if (not is_transient and attempt < attempts) or attempt == attempts or remaining <= 0:
                raise
            sleep = min(delay, remaining) * random.uniform(0.5, 1.5)
            log.warning("Gemini transient error (attempt %d/%d, retry in %.0fs): %s",
                        attempt, attempts, sleep, e)
            time.sleep(sleep)
            delay = min(delay * 2, max_delay)
    raise RuntimeError("unreachable")


def call_with_model_retry(make_fn, models: list[str], *, attempts: int = 3,
                          timeout: float = RETRY_TIMEOUT):
    """Try ``make_fn(model)`` across each model in order.

    Each model gets ``attempts`` chances (graceful for transient overload),
    then the next model is tried. If every model fails, the last error is
    re-raised.
    """
    last_err: Exception | None = None
    budget = timeout
    for model in models:
        try:
            start = time.monotonic()
            return call_with_retry(lambda: make_fn(model), attempts=attempts, timeout=budget)
        except Exception as e:
            last_err = e
            log.warning("All retries exhausted for %s — trying next model (%s)", model, e)
            budget -= time.monotonic() - start
            if budget <= 0:
                break
    if last_err is not None:
        raise last_err
    raise RuntimeError("no models to try")


def main() -> None:
    parser = __import__("argparse").ArgumentParser(description="Gemini helper")
    parser.add_argument("--probe", action="store_true", help="Ping candidate models and report status")
    parser.add_argument("models", nargs="*", help="Optional model list to probe")
    args = parser.parse_args()

    if args.probe:
        logging.basicConfig(level=logging.INFO, format="%(levelname)-7s  %(name)s  %(message)s")
        models = args.models or MODEL_CANDIDATES
        results = probe_models(models)
        for model, status in results:
            print(f"{model:24s} {status}")
        sys.exit(0 if any(s == "ok" for _, s in results) else 1)
    parser.error("nothing to do (use --probe)")


if __name__ == "__main__":
    main()