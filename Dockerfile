FROM ubuntu:24.04

ARG INSTALL_SUBTITLE=0
ARG INSTALL_ASR=0

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_DEFAULT_TIMEOUT=300
ENV PIP_RETRIES=20
ENV VIRTUAL_ENV=/opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

WORKDIR /app

RUN apt-get update \
  && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-venv \
    python-is-python3 \
    ffmpeg \
  && if [ "$INSTALL_SUBTITLE" = "1" ]; then \
       apt-get install -y --no-install-recommends \
         fonts-noto-cjk \
         intel-media-va-driver-non-free \
         i965-va-driver \
         pciutils \
         ocl-icd-libopencl1 \
         intel-opencl-icd \
         libze-intel-gpu1 \
         libze1 \
         clinfo; \
     fi \
  && python3 -m venv "${VIRTUAL_ENV}" \
  && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml requirements.lock README.md ./

ARG YTDLP_VERSION=2026.8.19
ARG TORCH_CPU_INDEX_URL=https://download.pytorch.org/whl/cpu

# Install dependencies in a cache-friendly layer so editing source code doesn't
# force re-downloading everything on every docker build.
RUN INSTALL_SUBTITLE="$INSTALL_SUBTITLE" INSTALL_ASR="$INSTALL_ASR" python -c "import os, tomllib; from pathlib import Path; data=tomllib.loads(Path('pyproject.toml').read_text('utf-8')); deps=list(data.get('project', {}).get('dependencies', []) or []); opt=data.get('project', {}).get('optional-dependencies', {}) or {}; deps += list(opt.get('subtitle', []) or []) if os.getenv('INSTALL_SUBTITLE','0')=='1' else []; deps += list(opt.get('asr', []) or []) if os.getenv('INSTALL_ASR','0')=='1' else []; Path('/tmp/requirements.txt').write_text('\\n'.join(deps) + '\\n', encoding='utf-8')" \
  && pip install --no-cache-dir -U pip \
  && if [ "$INSTALL_SUBTITLE" = "1" ] || [ "$INSTALL_ASR" = "1" ]; then \
       pip install --no-cache-dir --index-url "$TORCH_CPU_INDEX_URL" "torch==2.14.0"; \
     fi \
  && pip install --no-cache-dir -c requirements.lock -r /tmp/requirements.txt

COPY src/videoroll ./src/videoroll
COPY alembic.ini ./alembic.ini
COPY migrations ./migrations
COPY docs ./docs
COPY docker ./docker

RUN pip install --no-cache-dir -e . --no-deps

RUN if [ -n "$YTDLP_VERSION" ]; then \
      if [ "$YTDLP_VERSION" = "latest" ]; then \
        pip install --no-cache-dir -U "yt-dlp[default]"; \
      else \
        pip install --no-cache-dir -U "yt-dlp[default]==${YTDLP_VERSION}"; \
    fi; \
  fi

ARG APP_UID=10001
ARG APP_GID=10001
# Ubuntu may already have the host's numeric UID/GID; keep our named account
# while assigning the same IDs used by Compose and the mounted directories.
RUN groupadd --non-unique --gid "$APP_GID" videoroll \
  && useradd --non-unique --uid "$APP_UID" --gid videoroll --create-home --shell /usr/sbin/nologin videoroll \
  && install -d --owner=videoroll --group=videoroll --mode=0700 /models /secrets /storage /work /state /work/render-worker

RUN sed -i 's/\r$//' /app/docker/entrypoint.sh \
  && chmod +x /app/docker/entrypoint.sh

USER videoroll

ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["uvicorn", "videoroll.apps.orchestrator_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
