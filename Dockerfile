# Playwright image already contains Chromium + all system libraries.
# The image tag version MUST match the playwright version in requirements.txt.
FROM mcr.microsoft.com/playwright/python:v1.49.1-jammy

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT','8080'))" || exit 1

# ONE worker on purpose: one shared Chromium, one in-memory cache.
# Shell form so $PORT is expanded.
CMD hypercorn app:app --bind 0.0.0.0:${PORT} --workers 1 --graceful-timeout 10
