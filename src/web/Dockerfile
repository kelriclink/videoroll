FROM node:22-alpine AS build

WORKDIR /app

ARG NPM_REGISTRY=https://registry.npmmirror.com
ENV NPM_CONFIG_REGISTRY=$NPM_REGISTRY

ARG VITE_ORCHESTRATOR_URL
ENV VITE_ORCHESTRATOR_URL=$VITE_ORCHESTRATOR_URL

ARG VITE_FFPLAYOUT_URL
ENV VITE_FFPLAYOUT_URL=$VITE_FFPLAYOUT_URL

ARG VITE_FFPLAYOUT_PORT=3003
ENV VITE_FFPLAYOUT_PORT=$VITE_FFPLAYOUT_PORT

COPY package*.json ./
RUN if [ -f package-lock.json ]; then npm ci --no-fund --no-audit; else npm install --no-fund --no-audit; fi

COPY . .
RUN npm run build

FROM nginx:1.27-alpine
ENV NGINX_ENVSUBST_FILTER="PLAYOUT_HOST|VIDEOROLL_PUBLIC_ORIGIN" \
    VIDEOROLL_PUBLIC_ORIGIN=http://localhost:3000
COPY nginx.conf /etc/nginx/templates/default.conf.template
COPY --from=build /app/dist /usr/share/nginx/html
