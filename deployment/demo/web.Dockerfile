FROM node:22.16.0-bookworm-slim AS shop
WORKDIR /src
COPY mall-app-web/package*.json ./
RUN --mount=type=cache,target=/root/.npm npm ci --no-audit --no-fund
COPY mall-app-web/ ./
RUN VITE_API_BASE_URL=/mall-portal VITE_AGENT_GATEWAY_URL=/ VITE_USE_ALIPAY=false npm run build:h5

FROM node:22.16.0-bookworm-slim AS admin
WORKDIR /src
COPY mall-admin-web/package*.json ./
RUN --mount=type=cache,target=/root/.npm npm ci --no-audit --no-fund
COPY mall-admin-web/ ./
RUN VITE_BASE_SERVER_URL=/mall-admin VITE_USE_OSS=false npm run build

FROM nginx:1.28.0-alpine
COPY --from=shop /src/dist/build/h5 /srv/shop
COPY --from=admin /src/dist /srv/admin
COPY deployment/demo/assets /srv/shared/demo-assets
COPY deployment/demo/web.conf /etc/nginx/conf.d/default.conf
COPY deployment/demo/proxy.conf /etc/nginx/demo-proxy.conf
