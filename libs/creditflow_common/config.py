"""
config.py — environment-driven settings shared across services.

Each service reads what it needs from the environment (injected by
docker-compose). No secrets are hard-coded.
"""
from __future__ import annotations

import os


def env(key: str, default: str | None = None, required: bool = False) -> str:
    val = os.getenv(key, default)
    if required and not val:
        raise RuntimeError(f"Missing required environment variable: {key}")
    return val or ""


# --- Infrastructure URLs ---
DATABASE_URL = env("DATABASE_URL", "postgresql+psycopg://creditflow:creditflow@postgres:5432/creditflow")
REDIS_URL = env("REDIS_URL", "redis://redis:6379/0")
RABBITMQ_URL = env("RABBITMQ_URL", "amqp://creditflow:creditflow@rabbitmq:5672/")
MONGO_URL = env("MONGO_URL", "mongodb://creditflow:creditflow@mongo:27017/")

# --- JWT (RS256) ---
JWT_PUBLIC_KEY_PATH = env("JWT_PUBLIC_KEY_PATH", "/keys/jwt_public.pem")
JWT_PRIVATE_KEY_PATH = env("JWT_PRIVATE_KEY_PATH", "/keys/jwt_private.pem")
JWT_ISSUER = env("JWT_ISSUER", "creditflow-auth")
JWT_ALGORITHM = "RS256"
JWT_ACCESS_TTL_SECONDS = int(env("JWT_ACCESS_TTL_SECONDS", "900"))
JWT_REFRESH_TTL_SECONDS = int(env("JWT_REFRESH_TTL_SECONDS", "1209600"))
