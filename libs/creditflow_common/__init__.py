"""
creditflow_common — shared utilities used by every CreditFlow service.

Kept intentionally small and dependency-light so each service can install it
and reuse the cross-cutting concerns (auth, messaging, idempotency, db) instead
of re-implementing them 13 times.
"""
__version__ = "0.1.0"
