"""
AI Ops - Self-Healing + Feature Planning
========================================
Unified agent system with two modes:
  ERROR:   Diagnostician + Engineer + Reviewer → diagnose & fix
  FEATURE: Architect + Engineer + QA Agent → plan & implement

Both use 3-agent Q&A interrogation with Claude Opus 4.6.
"""

from .resilience import (
    ResilienceManager, CircuitBreaker, FallbackCache,
    retry_with_backoff, circuit_breaker,
    init_flask_resilience, resilience_manager,
)
from .consensus_engine import ConsensusEngine, ConsensusResult
from .notifications import NotificationManager, NotificationConfig
from .triage_agent import AIOpsAgent, init_flask_agent

__all__ = [
    "ResilienceManager", "CircuitBreaker", "FallbackCache",
    "retry_with_backoff", "circuit_breaker",
    "init_flask_resilience", "resilience_manager",
    "ConsensusEngine", "ConsensusResult",
    "NotificationManager", "NotificationConfig",
    "AIOpsAgent", "init_flask_agent",
]
