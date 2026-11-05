FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# System libs for headless Chromium + Python deps, then the browser itself.
COPY requirements.txt .
RUN pip install --upgrade pip \
    && pip install -r requirements.txt \
    && playwright install --with-deps chromium

COPY . .

# Dry-run by default; add --commit (and approve the trunk) to write to the portal.
# Works for both CPU-only and Playwright (the portal steps are only used on --commit).
CMD ["python", "orchestrator.py", "--loop", "--interval", "300"]
