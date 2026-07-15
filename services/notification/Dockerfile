# Notification service image. IMPORTANT: build with the REPO ROOT as
# context so the shared libs/ folder is available, e.g. in docker-compose:
#
#   build:
#     context: .
#     dockerfile: services/notification/Dockerfile
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

# 1) Install the shared library (creditflow_common + its deps).
COPY libs /tmp/libs
RUN pip install --no-cache-dir /tmp/libs

# 2) Install service-specific deps.
COPY services/notification/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# 3) Copy the service code.
COPY services/notification/ ./

# Build-time smoke test: fail the build if the app, the provider layer, or
# the templates can't import (no secrets required — env is read at call time).
RUN python -c "import main, mailer, templates"

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
