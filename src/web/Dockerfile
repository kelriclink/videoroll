FROM node:20-alpine AS build

WORKDIR /app

ARG NPM_REGISTRY=https://registry.npmmirror.com
ENV NPM_CONFIG_REGISTRY=$NPM_REGISTRY

ARG VITE_ORCHESTRATOR_URL
ARG VITE_SUBTITLE_SERVICE_URL
ARG VITE_YOUTUBE_INGEST_URL
ARG VITE_BILIBILI_PUBLISHER_URL
ENV VITE_ORCHESTRATOR_URL=$VITE_ORCHESTRATOR_URL
ENV VITE_SUBTITLE_SERVICE_URL=$VITE_SUBTITLE_SERVICE_URL
ENV VITE_YOUTUBE_INGEST_URL=$VITE_YOUTUBE_INGEST_URL
ENV VITE_BILIBILI_PUBLISHER_URL=$VITE_BILIBILI_PUBLISHER_URL

COPY package*.json ./
RUN if [ -f package-lock.json ]; then npm ci --no-fund --no-audit; else npm install --no-fund --no-audit; fi

COPY . .
RUN npm run build

FROM nginx:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
