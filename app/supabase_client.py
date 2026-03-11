"""
Supabase Client - Wrapper for database access with retry logic and connection resilience
"""

from supabase import create_client, Client
import os
import time
import logging
from functools import wraps
from threading import Lock
from httpx import TimeoutException, ConnectError, HTTPStatusError, RemoteProtocolError, LocalProtocolError

logger = logging.getLogger(__name__)

# Thread-safe client management
_client = None
_client_lock = Lock()

# Supabase configuration
SUPABASE_URL = os.getenv("SUPABASE_URL", "")

# Resolve key with explicit empty-string handling
_service_role_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or ""
_service_key = os.getenv("SUPABASE_SERVICE_KEY") or ""
_anon_key = os.getenv("SUPABASE_KEY") or ""
SUPABASE_KEY = _service_role_key.strip() or _service_key.strip() or _anon_key.strip()

MAX_RETRIES = 3
RETRY_DELAY_BASE = 0.5
RETRY_DELAY_MAX = 5.0


def get_supabase_client() -> Client:
    """Get or create Supabase client with connection validation (thread-safe)"""
    global _client
    with _client_lock:
        if _client is None:
            if not SUPABASE_URL or not SUPABASE_KEY:
                logger.error("Supabase URL or key not configured")
                raise ValueError("Supabase configuration missing")
            _client = create_client(SUPABASE_URL, SUPABASE_KEY)
        return _client


def reset_supabase_client():
    """Reset the Supabase client to force a new connection"""
    global _client
    with _client_lock:
        _client = None
        logger.info("Supabase client reset")


def with_retry(max_retries=MAX_RETRIES, delay_base=RETRY_DELAY_BASE):
    """Decorator to add retry logic to Supabase operations"""

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except (TimeoutException, ConnectError, RemoteProtocolError, LocalProtocolError) as e:
                    last_exception = e
                    delay = min(delay_base * (2**attempt), RETRY_DELAY_MAX)
                    logger.warning(
                        f"Supabase connection error (attempt {attempt + 1}/{max_retries}): {e}. "
                        f"Retrying in {delay:.1f}s..."
                    )
                    time.sleep(delay)
                    reset_supabase_client()
                except HTTPStatusError as e:
                    if e.response.status_code == 429:
                        last_exception = e
                        delay = min(delay_base * (2**attempt), RETRY_DELAY_MAX)
                        logger.warning(
                            f"Supabase rate limit hit (attempt {attempt + 1}/{max_retries}). "
                            f"Retrying in {delay:.1f}s..."
                        )
                        time.sleep(delay)
                    elif e.response.status_code >= 500:
                        last_exception = e
                        delay = min(delay_base * (2**attempt), RETRY_DELAY_MAX)
                        logger.warning(
                            f"Supabase server error {e.response.status_code} "
                            f"(attempt {attempt + 1}/{max_retries}). Retrying in {delay:.1f}s..."
                        )
                        time.sleep(delay)
                    else:
                        raise
                except Exception as e:
                    error_msg = str(e).lower()
                    if any(kw in error_msg for kw in (
                        "connectionterminated", "server disconnected",
                        "eof occurred", "deque mutated", "protocol_error",
                    )):
                        last_exception = e
                        delay = min(delay_base * (2**attempt), RETRY_DELAY_MAX)
                        logger.warning(
                            f"Transient Supabase error (attempt {attempt + 1}/{max_retries}): {e}. "
                            f"Retrying in {delay:.1f}s..."
                        )
                        time.sleep(delay)
                        reset_supabase_client()
                    else:
                        logger.error(f"Unexpected error in Supabase operation: {e}")
                        raise
            logger.error(f"All {max_retries} retry attempts failed")
            raise last_exception

        return wrapper

    return decorator


def execute_with_retry(query_func, max_retries=MAX_RETRIES):
    """Execute a Supabase query with retry logic

    Usage:
        result = execute_with_retry(
            lambda: supabase.table("units").select("*").eq("id", unit_id).execute()
        )
    """
    last_exception = None
    for attempt in range(max_retries):
        try:
            return query_func()
        except (TimeoutException, ConnectError, RemoteProtocolError, LocalProtocolError) as e:
            last_exception = e
            delay = min(RETRY_DELAY_BASE * (2**attempt), RETRY_DELAY_MAX)
            logger.warning(
                f"Supabase connection error (attempt {attempt + 1}/{max_retries}): {e}. "
                f"Retrying in {delay:.1f}s..."
            )
            time.sleep(delay)
            reset_supabase_client()
        except HTTPStatusError as e:
            if e.response.status_code == 429:
                last_exception = e
                delay = min(RETRY_DELAY_BASE * (2**attempt), RETRY_DELAY_MAX)
                logger.warning(
                    f"Supabase rate limit hit (attempt {attempt + 1}/{max_retries}). "
                    f"Retrying in {delay:.1f}s..."
                )
                time.sleep(delay)
            elif e.response.status_code >= 500:
                last_exception = e
                delay = min(RETRY_DELAY_BASE * (2**attempt), RETRY_DELAY_MAX)
                logger.warning(
                    f"Supabase server error {e.response.status_code} "
                    f"(attempt {attempt + 1}/{max_retries}). Retrying in {delay:.1f}s..."
                )
                time.sleep(delay)
            else:
                raise
        except Exception as e:
            error_msg = str(e).lower()
            if any(kw in error_msg for kw in (
                "connectionterminated", "server disconnected",
                "eof occurred", "deque mutated", "protocol_error",
            )):
                last_exception = e
                delay = min(RETRY_DELAY_BASE * (2**attempt), RETRY_DELAY_MAX)
                logger.warning(
                    f"Transient Supabase error (attempt {attempt + 1}/{max_retries}): {e}. "
                    f"Retrying in {delay:.1f}s..."
                )
                time.sleep(delay)
                reset_supabase_client()
            else:
                logger.error(f"Unexpected error in Supabase query: {e}")
                raise
    logger.error(f"All {max_retries} retry attempts failed for Supabase query")
    raise last_exception
