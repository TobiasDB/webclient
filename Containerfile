# webclient -- OCI image (podman / docker / kubernetes compatible).
#
#   podman build -t webclient -f Containerfile .                    # service + browser
#   podman build -t webclient:slim --target service -f Containerfile .   # no browser
#   podman run -p 8000:8000 -e WEBCLIENT_SERVICE_TOKEN=secret webclient
#
# Stages: `base` (the package + local parsing), `service` (FastAPI/uvicorn, no browser --
# the horizontally-scalable static tier) and `browser` (adds Playwright's Chromium on the
# official Playwright base image so headless renders work out of the box).

ARG PYTHON=3.12
ARG PLAYWRIGHT=1.47.0

# ---------------------------------------------------------------- base
FROM python:${PYTHON}-slim AS base
ENV PIP_NO_CACHE_DIR=1 PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    WEBCLIENT_LOG_LEVEL=INFO
WORKDIR /app
COPY pyproject.toml README.md ./
COPY webclient ./webclient
RUN pip install -e ".[local]"

# ---------------------------------------------------------------- service (no browser)
FROM base AS service
RUN pip install -e ".[service]"
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health').status==200 else 1)"
CMD ["python", "-m", "webclient.service"]

# ---------------------------------------------------------------- browser (Playwright + Chromium)
FROM mcr.microsoft.com/playwright/python:v${PLAYWRIGHT}-jammy AS browser
ENV PIP_NO_CACHE_DIR=1 PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    WEBCLIENT_LOG_LEVEL=INFO WEBCLIENT_BROWSER__HEADLESS=true
WORKDIR /app
COPY pyproject.toml README.md ./
COPY webclient ./webclient
RUN pip install -e ".[local,browser,service]"
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health').status==200 else 1)"
CMD ["python", "-m", "webclient.service"]
