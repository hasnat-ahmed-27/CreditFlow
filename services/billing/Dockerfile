# Billing service image. IMPORTANT: build with the REPO ROOT as context so
# the shared libs/ folder is available, e.g. in docker-compose:
#
#   build:
#     context: .
#     dockerfile: services/billing/Dockerfile
#
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

# 1) Install the shared library (creditflow_common + its deps).
COPY libs /tmp/libs
RUN pip install --no-cache-dir /tmp/libs

# 2) Install service-specific deps.
COPY services/billing/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# 3) Copy the service code.
COPY services/billing/ ./

# Build-time smoke test: fail the build if the app can't import.
RUN python -c "import main"

EXPOSE 8000
# The outbox poller starts as a daemon thread inside the app (lifespan).
# To run it as its own container instead: set BILLING_POLLER_ENABLED=0 on the
# web container and run `python -m poller` as the second container's command.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
