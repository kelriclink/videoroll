FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    SAU_RUNTIME_DIR=/opt/social-auto-upload \
    SAU_COOKIES_DIR=/opt/social-auto-upload/cookies \
    DISPLAY=:99 \
    DOUYIN_COOKIE_AUTH_HEADLESS=true

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends fluxbox novnc websockify x11vnc xauth \
    && rm -rf /var/lib/apt/lists/*

COPY docker/social-publisher-requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -U pip \
    && pip install --no-cache-dir -r /tmp/requirements.txt

COPY pyproject.toml README.md ./
COPY src/videoroll ./src/videoroll
COPY alembic.ini ./alembic.ini
COPY migrations ./migrations
RUN pip install --no-cache-dir -e . --no-deps

COPY social-auto-upload /opt/social-auto-upload
RUN cp /opt/social-auto-upload/conf.example.py /opt/social-auto-upload/conf.py \
    && pip install --no-cache-dir -e /opt/social-auto-upload \
    && mkdir -p /ms-playwright /work/social-publisher /opt/social-auto-upload/cookies \
    && patchright install --with-deps chromium \
    && chromium_bin="$(find /ms-playwright -type f -path '*/chrome-linux64/chrome' -print -quit)" \
    && test -n "$chromium_bin" \
    && mkdir -p /opt/google/chrome \
    && ln -sf "$chromium_bin" /opt/google/chrome/chrome

COPY docker/social-publisher-entrypoint.sh /app/docker/social-publisher-entrypoint.sh
COPY docker/verify-social-browser.py /app/docker/verify-social-browser.py
RUN chmod +x /app/docker/social-publisher-entrypoint.sh

RUN useradd --create-home --uid 10001 videoroll \
    && chown -R videoroll:videoroll /ms-playwright /work/social-publisher /opt/social-auto-upload \
    && install -d --owner=videoroll --group=videoroll --mode=0700 /secrets /tmp/videoroll-vnc \
    && install -d --owner=videoroll --group=videoroll --mode=0700 /tmp/videoroll-home

ENV HOME=/tmp/videoroll-home
USER videoroll

RUN python /app/docker/verify-social-browser.py

ENTRYPOINT ["/app/docker/social-publisher-entrypoint.sh"]
CMD ["uvicorn", "videoroll.apps.social_publisher.main:app", "--host", "0.0.0.0", "--port", "8010"]
