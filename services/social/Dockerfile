# Social Publishing service image. IMPORTANT: build with the REPO ROOT as
# context so the shared libs/ folder is available, e.g. in docker-compose:
#
#   build:
#     context: .
#     dockerfile: services/social/Dockerfile
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

# 1) Install the shared library (creditflow_common + its deps).
COPY libs /tmp/libs
RUN pip install --no-cache-dir /tmp/libs

# 2) Install service-specific deps.
COPY services/social/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# 3) Copy the service code.
COPY services/social/ ./

# Build-time smoke test: fail the build if the app or either outbound client
# can't import (no secrets required — env is read at call time).
RUN python -c "import main, linkedin, content_client"

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
