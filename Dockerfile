# Playwright ki official image — Python + Chromium + saare system deps pehle se installed.
# (python:3.11-slim pe `playwright install --with-deps` fail hota hai kyunki uske
#  package naam naye Debian me exist nahi karte.)
FROM mcr.microsoft.com/playwright/python:v1.49.1-jammy

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

CMD ["sh", "-c", "gunicorn --workers 1 --threads 4 --timeout 120 --bind 0.0.0.0:${PORT:-10000} app:app"]
