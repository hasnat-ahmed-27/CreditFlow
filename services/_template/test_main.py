"""
Example pytest for the service template. Every backend service must ship tests
like these — the CI workflow runs them and blocks merges on failure.
"""
from fastapi.testclient import TestClient

import main


client = TestClient(main.app)


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_me_requires_token():
    r = client.get("/me")
    assert r.status_code == 401
