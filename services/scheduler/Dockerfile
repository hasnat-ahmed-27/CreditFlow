# Scheduler service image. IMPORTANT: build with the REPO ROOT as
# context so the shared libs/ folder is available, e.g. in docker-compose:
#
#   build:
#     context: .
#     dockerfile: services/scheduler/Dockerfile
#
# ONE image, THREE processes (docker-compose overrides the command):
#   API    (default CMD)  uvicorn main:app
#   worker                celery -A celery_app worker --loglevel=info
#   beat                  celery -A celery_app beat --loglevel=info
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

# 1) Install the shared library (creditflow_common + its deps).
COPY libs /tmp/libs
RUN pip install --no-cache-dir /tmp/libs

# 2) Install service-specific deps.
COPY services/scheduler/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# 3) Copy the service code.
COPY services/scheduler/ ./

# Build-time smoke test: fail the build if the app, tasks, or celery wiring
# can't import (covers all three process entrypoints).
RUN python -c "import main, celery_app, tasks"

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
