FROM node:22-alpine AS builder

WORKDIR /build
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build
# The API address nginx proxies to. The default is the in-cluster Service of the
# local Kubernetes manifests; Azure Container Apps passes its internal app URL.
ARG API_UPSTREAM=http://incident-resolution-ai:8000
RUN sed "s#http://incident-resolution-ai:8000#${API_UPSTREAM}#g" nginx.conf > /build/nginx.conf

FROM nginxinc/nginx-unprivileged:stable-alpine
COPY --from=builder /build/dist /usr/share/nginx/html
COPY --from=builder /build/nginx.conf /etc/nginx/nginx.conf

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD wget -q -O /dev/null http://127.0.0.1:8080/ || exit 1
