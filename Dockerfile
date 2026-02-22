FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
  && apt-get install -y --no-install-recommends ffmpeg fonts-noto-cjk \
  && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./

ARG INSTALL_ASR=0

# Install dependencies in a cache-friendly layer so editing source code doesn't
# force re-downloading everything on every docker build.
RUN INSTALL_ASR="$INSTALL_ASR" python -c "import os, tomllib; from pathlib import Path; data=tomllib.loads(Path('pyproject.toml').read_text('utf-8')); deps=list(data.get('project', {}).get('dependencies', []) or []); opt=data.get('project', {}).get('optional-dependencies', {}) or {}; deps += list(opt.get('asr', []) or []) if os.getenv('INSTALL_ASR','0')=='1' else []; Path('/tmp/requirements.txt').write_text('\\n'.join(deps) + '\\n', encoding='utf-8')" \
  && pip install --no-cache-dir -U pip \
  && pip install --no-cache-dir -r /tmp/requirements.txt

COPY src ./src
COPY docs ./docs
COPY docker ./docker

RUN pip install --no-cache-dir -e . --no-deps

RUN chmod +x /app/docker/entrypoint.sh

CMD ["/app/docker/entrypoint.sh"]
