FROM ubuntu:24.04

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
    fonts-noto-cjk \
    intel-media-va-driver \
    i965-va-driver \
    pciutils \
    ocl-icd-libopencl1 \
    intel-opencl-icd \
    libze-intel-gpu1 \
    libze1 \
    clinfo \
  && python3 -m venv "${VIRTUAL_ENV}" \
  && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./

ARG INSTALL_ASR=0
ARG YTDLP_VERSION=latest
ARG TORCH_CPU_INDEX_URL=https://download.pytorch.org/whl/cpu

# Install dependencies in a cache-friendly layer so editing source code doesn't
# force re-downloading everything on every docker build.
RUN INSTALL_ASR="$INSTALL_ASR" python -c "import os, tomllib; from pathlib import Path; data=tomllib.loads(Path('pyproject.toml').read_text('utf-8')); deps=list(data.get('project', {}).get('dependencies', []) or []); opt=data.get('project', {}).get('optional-dependencies', {}) or {}; deps += list(opt.get('asr', []) or []) if os.getenv('INSTALL_ASR','0')=='1' else []; Path('/tmp/requirements.txt').write_text('\\n'.join(deps) + '\\n', encoding='utf-8')" \
  && pip install --no-cache-dir -U pip \
  && pip install --no-cache-dir --index-url "$TORCH_CPU_INDEX_URL" "torch>=2.3,<3" \
  && pip install --no-cache-dir -r /tmp/requirements.txt

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

RUN sed -i 's/\r$//' /app/docker/entrypoint.sh \
  && chmod +x /app/docker/entrypoint.sh

CMD ["/app/docker/entrypoint.sh"]
