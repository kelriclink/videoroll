FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_DEFAULT_TIMEOUT=300
ENV PIP_RETRIES=20
ENV PYTHONPATH=/app/src

WORKDIR /app

COPY docker/workflow-requirements.txt /tmp/workflow-requirements.txt
RUN pip install --no-cache-dir -U pip \
  && pip install --no-cache-dir -r /tmp/workflow-requirements.txt

COPY src/videoroll/__init__.py ./src/videoroll/__init__.py
COPY src/videoroll/workflows ./src/videoroll/workflows

ARG APP_UID=10001
ARG APP_GID=10001
RUN groupadd --non-unique --gid "$APP_GID" videoroll \
  && useradd --non-unique --uid "$APP_UID" --gid videoroll --create-home --shell /usr/sbin/nologin videoroll

USER videoroll

CMD ["python", "-m", "videoroll.workflows.worker"]
