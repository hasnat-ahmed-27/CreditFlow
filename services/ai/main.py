"""
CreditFlow AI Generation service — wraps OpenRouter for streaming chat
completions, fans tokens out via SSE, and stores prompt/response history.

One sentence of design: the OpenRouter stream is consumed once by a worker
task that pushes every token through a Redis pub/sub channel keyed by job_id
— the SSE endpoint (and later the Gateway) subscribes there, so client
disconnects never kill a paid generation and the final response + token
counts always land in Postgres and on the usage_events exchange
(see store.py for the full argument).
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import database
import routes

SERVICE_NAME = os.getenv("SERVICE_NAME", "ai")


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()  # create schema + tables on startup (spec-sanctioned simple path)
    yield


app = FastAPI(title=f"CreditFlow — {SERVICE_NAME}", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(routes.router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": SERVICE_NAME}
