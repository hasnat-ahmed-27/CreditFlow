# Gateway image. Built with the REPO ROOT as context (like every other
# service) so the shared libs/ folder is available: the gateway now installs
# creditflow_common for RS256 verification, the RabbitMQ publisher, and the
# Redis config.
#
#   build:
#     context: .
#     dockerfile: services/gateway/Dockerfile
#
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

# 1) Install the shared library (creditflow_common + its deps).
COPY libs /tmp/libs
RUN pip install --no-cache-dir /tmp/libs

# 2) Install service-specific deps.
COPY services/gateway/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# 3) Copy the service code.
COPY services/gateway/ ./

# Build-time smoke test: fail the build if the app can't import.
RUN python -c "import main"

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
