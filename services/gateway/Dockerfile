# Gateway image. Built with the REPO ROOT as context to match every other
# service's compose convention — though unlike them the gateway does NOT
# install libs/ (no creditflow_common: it holds no DB, broker, or JWT logic).
#
#   build:
#     context: .
#     dockerfile: services/gateway/Dockerfile
#
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY services/gateway/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY services/gateway/ ./

# Build-time smoke test: fail the build if the app can't import.
RUN python -c "import main"

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
