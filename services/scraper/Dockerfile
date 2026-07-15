# Scraper service image. IMPORTANT: build with the REPO ROOT as context so
# the shared libs/ folder is available, e.g. in docker-compose:
#
#   build:
#     context: .
#     dockerfile: services/scraper/Dockerfile
#
# ONE image, THREE processes (same pattern as the Scheduler): the API (CMD
# below), plus scraper-worker / scraper-beat overriding the command in
# docker-compose. Only the worker actually launches Chromium, but the
# browser is baked into the shared image so any process could.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

# 1) Install the shared library (creditflow_common + its deps).
COPY libs /tmp/libs
RUN pip install --no-cache-dir /tmp/libs

# 2) Install service-specific deps, then the Chromium browser Playwright
#    drives (--with-deps pulls the system libraries headless Chromium needs).
#    This layer is heavy and exists ONLY here — CI/pytest never runs it
#    (tests mock the engine and never launch a browser).
COPY services/scraper/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium

# 3) Copy the service code.
COPY services/scraper/ ./

# Build-time smoke test: fail the build if the app, the engine, or the task
# modules can't import (no secrets/infra required — env is read at call time
# and playwright is imported lazily inside fetch_page).
RUN python -c "import main, scraper_engine, tasks, consumer"

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
