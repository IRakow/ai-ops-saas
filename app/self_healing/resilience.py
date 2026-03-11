"""
AI OPS - PRODUCTION RESILIENCE LAYER
=====================================
Circuit breakers, retry logic, and graceful degradation

Drop into your Flask app and wrap critical calls.

Usage:
    from .resilience import circuit_breaker, retry_with_backoff, resilience_manager

    # Wrap a Supabase call with circuit breaker + retry
    @retry_with_backoff(max_retries=3)
    @circuit_breaker("supabase")
    def get_tenant(tenant_id):
        return supabase.table("tenants").select("*").eq("id", tenant_id).execute()

    # Or use inline
    result = resilience_manager.execute(
        name="supabase",
        func=lambda: supabase.table("units").select("*").execute(),
        fallback=lambda: cached_units
    )
"""

import time
import logging
import threading
import functools
import traceback
from datetime import datetime, timezone
from collections import defaultdict
from enum import Enum

logger = logging.getLogger("ai_ops.resilience")


# =============================================================================
# CIRCUIT BREAKER
# =============================================================================

class CircuitState(Enum):
    CLOSED = "closed"       # Healthy - requests flow through
    OPEN = "open"           # Tripped - requests blocked, fallback used
    HALF_OPEN = "half_open" # Testing - one request allowed through


class CircuitBreaker:
    """
    Prevents cascade failures by stopping calls to a failing service.

    States:
        CLOSED  → Normal operation. Failures are counted.
        OPEN    → Service is down. All calls return fallback immediately.
        HALF_OPEN → Cooldown expired. One test request is allowed through.
                    If it succeeds → CLOSED. If it fails → OPEN again.
    """

    def __init__(self, name, failure_threshold=5, reset_timeout=60, 
                 half_open_max_calls=1):
        self.name = name
        self.failure_threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self.half_open_max_calls = half_open_max_calls

        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.success_count = 0
        self.last_failure_time = None
        self.last_state_change = datetime.now(timezone.utc)
        self.half_open_calls = 0
        self._lock = threading.Lock()

        # Metrics
        self.total_calls = 0
        self.total_failures = 0
        self.total_fallbacks = 0
        self.trip_count = 0

    def _should_attempt_reset(self):
        if self.state != CircuitState.OPEN:
            return False
        if self.last_failure_time is None:
            return False
        elapsed = time.time() - self.last_failure_time
        return elapsed >= self.reset_timeout

    def _trip(self):
        self.state = CircuitState.OPEN
        self.last_state_change = datetime.now(timezone.utc)
        self.trip_count += 1
        logger.warning(
            f"🔴 Circuit [{self.name}] TRIPPED (failures: {self.failure_count}, "
            f"total trips: {self.trip_count})"
        )

    def _reset(self):
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.half_open_calls = 0
        self.last_state_change = datetime.now(timezone.utc)
        logger.info(f"🟢 Circuit [{self.name}] RESET - service recovered")

    def _record_success(self):
        with self._lock:
            self.success_count += 1
            if self.state == CircuitState.HALF_OPEN:
                self._reset()
            elif self.state == CircuitState.CLOSED:
                self.failure_count = 0  # Reset consecutive failures

    def _record_failure(self, error):
        with self._lock:
            self.failure_count += 1
            self.total_failures += 1
            self.last_failure_time = time.time()

            if self.state == CircuitState.HALF_OPEN:
                self._trip()
            elif self.failure_count >= self.failure_threshold:
                self._trip()

    def call(self, func, *args, fallback=None, **kwargs):
        """Execute func through the circuit breaker."""
        self.total_calls += 1

        with self._lock:
            if self.state == CircuitState.OPEN:
                if self._should_attempt_reset():
                    self.state = CircuitState.HALF_OPEN
                    self.half_open_calls = 0
                    self.last_state_change = datetime.now(timezone.utc)
                    logger.info(f"🟡 Circuit [{self.name}] HALF_OPEN - testing...")
                else:
                    self.total_fallbacks += 1
                    if fallback:
                        logger.debug(f"Circuit [{self.name}] OPEN - using fallback")
                        return fallback() if callable(fallback) else fallback
                    return None

            if self.state == CircuitState.HALF_OPEN:
                if self.half_open_calls >= self.half_open_max_calls:
                    self.total_fallbacks += 1
                    if fallback:
                        return fallback() if callable(fallback) else fallback
                    return None
                self.half_open_calls += 1

        try:
            result = func(*args, **kwargs)
            self._record_success()
            return result
        except Exception as e:
            self._record_failure(e)
            logger.error(f"Circuit [{self.name}] failure #{self.failure_count}: {e}")
            if fallback:
                self.total_fallbacks += 1
                return fallback() if callable(fallback) else fallback
            raise

    @property
    def status(self):
        return {
            "name": self.name,
            "state": self.state.value,
            "failure_count": self.failure_count,
            "total_calls": self.total_calls,
            "total_failures": self.total_failures,
            "total_fallbacks": self.total_fallbacks,
            "trip_count": self.trip_count,
            "last_state_change": self.last_state_change.isoformat(),
        }


# =============================================================================
# RETRY WITH BACKOFF
# =============================================================================

class RetryConfig:
    """Configuration for retry behavior."""

    def __init__(self, max_retries=3, base_delay=1.0, max_delay=30.0,
                 exponential=True, jitter=True, 
                 retryable_exceptions=None):
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.exponential = exponential
        self.jitter = jitter
        self.retryable_exceptions = retryable_exceptions or (
            ConnectionError, TimeoutError, OSError
        )


def retry_with_backoff(max_retries=3, base_delay=1.0, max_delay=30.0,
                       exponential=True, jitter=True, 
                       retryable_exceptions=None, on_retry=None):
    """
    Decorator: retry a function with exponential backoff.

    Usage:
        @retry_with_backoff(max_retries=3)
        def call_supabase():
            return supabase.table("tenants").select("*").execute()
    """
    config = RetryConfig(
        max_retries=max_retries,
        base_delay=base_delay,
        max_delay=max_delay,
        exponential=exponential,
        jitter=jitter,
        retryable_exceptions=retryable_exceptions,
    )

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None

            for attempt in range(config.max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_exception = e

                    # Don't retry non-retryable exceptions
                    if config.retryable_exceptions and \
                       not isinstance(e, config.retryable_exceptions):
                        raise

                    if attempt >= config.max_retries:
                        logger.error(
                            f"Retry exhausted for {func.__name__} after "
                            f"{config.max_retries + 1} attempts: {e}"
                        )
                        raise

                    # Calculate delay
                    if config.exponential:
                        delay = min(config.base_delay * (2 ** attempt), config.max_delay)
                    else:
                        delay = config.base_delay

                    if config.jitter:
                        import random
                        delay = delay * (0.5 + random.random())

                    logger.warning(
                        f"Retry {attempt + 1}/{config.max_retries} for "
                        f"{func.__name__} in {delay:.1f}s: {e}"
                    )

                    if on_retry:
                        on_retry(attempt, e, delay)

                    time.sleep(delay)

            raise last_exception

        return wrapper
    return decorator


# =============================================================================
# STRUCTURED ERROR RECOVERY
# =============================================================================

class ErrorRecovery:
    """
    Maps specific error types to specific recovery actions.
    Instead of generic try/except, handle each failure mode properly.
    """

    def __init__(self):
        self._handlers = {}  # error_type -> recovery_func
        self._default_handler = None

    def register(self, exception_type, handler):
        """
        Register a recovery handler for a specific exception type.

        Usage:
            recovery = ErrorRecovery()
            recovery.register(ConnectionError, lambda e, ctx: reconnect_db())
            recovery.register(TokenExpiredError, lambda e, ctx: refresh_token())
        """
        self._handlers[exception_type] = handler
        return self

    def set_default(self, handler):
        """Handler for unregistered exception types."""
        self._default_handler = handler
        return self

    def execute(self, func, *args, context=None, **kwargs):
        """Run func with automatic error recovery."""
        try:
            return func(*args, **kwargs)
        except Exception as e:
            # Find the most specific handler (check subclasses)
            handler = None
            for exc_type, h in self._handlers.items():
                if isinstance(e, exc_type):
                    handler = h
                    break

            if handler:
                logger.info(f"Recovering from {type(e).__name__}: {e}")
                return handler(e, context or {})
            elif self._default_handler:
                logger.warning(f"Default recovery for {type(e).__name__}: {e}")
                return self._default_handler(e, context or {})
            else:
                raise


# =============================================================================
# RESILIENCE MANAGER (Ties it all together)
# =============================================================================

class ResilienceManager:
    """
    Central manager for all circuit breakers, retries, and recovery.
    Provides a /health endpoint and status dashboard.

    Usage:
        resilience = ResilienceManager()

        # Register services
        resilience.register_service("supabase", failure_threshold=5, reset_timeout=60)
        resilience.register_service("gcs", failure_threshold=3, reset_timeout=120)
        resilience.register_service("valor_payments", failure_threshold=2, reset_timeout=300)

        # Execute with full protection
        result = resilience.execute(
            name="supabase",
            func=lambda: supabase.table("units").select("*").execute(),
            fallback=lambda: get_cached_units(),
            max_retries=3
        )
    """

    def __init__(self):
        self.circuits = {}
        self.error_log = []  # Recent errors for the triage agent
        self._max_error_log = 1000
        self._lock = threading.Lock()

    def register_service(self, name, failure_threshold=5, reset_timeout=60):
        """Register a service with a circuit breaker."""
        self.circuits[name] = CircuitBreaker(
            name=name,
            failure_threshold=failure_threshold,
            reset_timeout=reset_timeout,
        )
        logger.info(f"Registered circuit breaker: {name}")
        return self

    def execute(self, name, func, *args, fallback=None, max_retries=2, 
                base_delay=1.0, **kwargs):
        """
        Execute a function with circuit breaker + retry protection.
        This is the main entry point for protected calls.
        """
        if name not in self.circuits:
            self.register_service(name)

        circuit = self.circuits[name]

        def attempt():
            """Single attempt wrapped in retry logic."""
            last_error = None
            for attempt_num in range(max_retries + 1):
                try:
                    return circuit.call(func, *args, **kwargs)
                except Exception as e:
                    last_error = e
                    if attempt_num < max_retries:
                        delay = base_delay * (2 ** attempt_num)
                        time.sleep(delay)
                    else:
                        # Log for the triage agent to pick up
                        self._log_error(name, e)
                        raise
            raise last_error

        try:
            return attempt()
        except Exception as e:
            if fallback:
                return fallback() if callable(fallback) else fallback
            raise

    def _log_error(self, service_name, error):
        """Store error for the triage agent to analyze."""
        with self._lock:
            entry = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "service": service_name,
                "error_type": type(error).__name__,
                "error_message": str(error),
                "traceback": traceback.format_exc(),
                "circuit_state": self.circuits.get(service_name, {}).state.value
                    if service_name in self.circuits else "unknown",
            }
            self.error_log.append(entry)
            if len(self.error_log) > self._max_error_log:
                self.error_log = self.error_log[-self._max_error_log:]

    def get_recent_errors(self, limit=50, service=None):
        """Get recent errors - used by the triage agent."""
        errors = self.error_log
        if service:
            errors = [e for e in errors if e["service"] == service]
        return errors[-limit:]

    def health_check(self):
        """
        Returns health status for all registered services.
        Mount this on a /health endpoint.
        """
        status = {
            "status": "healthy",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "services": {},
            "recent_errors": len(self.error_log),
        }

        for name, circuit in self.circuits.items():
            svc_status = circuit.status
            status["services"][name] = svc_status
            if circuit.state == CircuitState.OPEN:
                status["status"] = "degraded"

        return status


# =============================================================================
# FLASK INTEGRATION
# =============================================================================

def init_flask_resilience(app, resilience_manager):
    """
    Register resilience endpoints with your Flask app.

    Usage:
        from .resilience import ResilienceManager, init_flask_resilience

        resilience = ResilienceManager()
        resilience.register_service("supabase")
        resilience.register_service("gcs")

        init_flask_resilience(app, resilience)
    """

    @app.route("/health")
    def health():
        from flask import jsonify
        health = resilience_manager.health_check()
        status_code = 200 if health["status"] == "healthy" else 503
        return jsonify(health), status_code

    @app.route("/health/circuits")
    def circuit_status():
        from flask import jsonify
        return jsonify({
            name: cb.status for name, cb in resilience_manager.circuits.items()
        })

    @app.route("/health/errors")
    def recent_errors():
        from flask import jsonify, request
        service = request.args.get("service")
        limit = int(request.args.get("limit", 50))
        return jsonify(resilience_manager.get_recent_errors(limit, service))

    @app.errorhandler(Exception)
    def handle_unhandled(error):
        from flask import jsonify
        resilience_manager._log_error("flask_unhandled", error)
        logger.error(f"Unhandled exception: {error}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


# =============================================================================
# DECORATOR SHORTCUTS
# =============================================================================

# Global resilience manager instance
resilience_manager = ResilienceManager()


def circuit_breaker(service_name, failure_threshold=5, reset_timeout=60):
    """
    Decorator to wrap a function with a circuit breaker.

    Usage:
        @circuit_breaker("supabase")
        def get_units():
            return supabase.table("units").select("*").execute()
    """
    if service_name not in resilience_manager.circuits:
        resilience_manager.register_service(service_name, failure_threshold, reset_timeout)

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            return resilience_manager.circuits[service_name].call(func, *args, **kwargs)
        return wrapper
    return decorator


# =============================================================================
# CACHE LAYER (for fallback data)
# =============================================================================

class FallbackCache:
    """
    Simple in-memory cache for fallback data.
    Stores the last known good response for each key.

    Usage:
        cache = FallbackCache()

        def get_units():
            result = supabase.table("units").select("*").execute()
            cache.set("units_list", result.data)  # Cache good response
            return result.data

        # Use as fallback
        resilience_manager.execute(
            name="supabase",
            func=get_units,
            fallback=lambda: cache.get("units_list", default=[])
        )
    """

    def __init__(self, max_age=300):
        self._store = {}
        self._timestamps = {}
        self.max_age = max_age
        self._lock = threading.Lock()

    def set(self, key, value):
        with self._lock:
            self._store[key] = value
            self._timestamps[key] = time.time()

    def get(self, key, default=None):
        with self._lock:
            if key not in self._store:
                return default
            age = time.time() - self._timestamps.get(key, 0)
            if age > self.max_age:
                logger.warning(f"Cache [{key}] stale ({age:.0f}s old), using anyway")
            return self._store[key]

    def invalidate(self, key):
        with self._lock:
            self._store.pop(key, None)
            self._timestamps.pop(key, None)
