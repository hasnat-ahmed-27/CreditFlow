"""Request bodies for the Social Publishing service (responses are plain
dicts, same convention as the other services)."""
from __future__ import annotations

from pydantic import BaseModel, Field

# Single source of truth for the publish-job vocabulary (jobs are terminal:
# written published|failed in the transaction that concludes the attempt).
JOB_STATUSES = ("published", "failed")
JOB_SOURCES = ("manual", "scheduled")
CONNECTION_STATUSES = ("connected", "disconnected")


class OAuthCallback(BaseModel):
    """The code+state LinkedIn appended to the redirect — relayed to us by
    the frontend along with the user's own bearer token."""
    code: str = Field(min_length=1, max_length=2000)
    state: str = Field(min_length=1, max_length=200)


class PublishRequest(BaseModel):
    content_id: str = Field(min_length=1, max_length=36)
