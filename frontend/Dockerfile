FROM node:20-slim@sha256:2cf067cfed83d5ea958367df9f966191a942351a2df77d6f0193e162b5febfc0 AS builder
WORKDIR /app
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
ARG VITE_GATEWAY_URL=/api
ARG VITE_SANDBOX_MODE=false
ARG VITE_LOCAL_MODE=false
ENV VITE_GATEWAY_URL=${VITE_GATEWAY_URL}
ENV VITE_SANDBOX_MODE=${VITE_SANDBOX_MODE}
ENV VITE_LOCAL_MODE=${VITE_LOCAL_MODE}
RUN npm run build

FROM nginx:alpine@sha256:4a73073bd557c65b759505da037898b61f1be6cbcc3c2c3aeac22d2a470c1752
COPY --from=builder /app/dist /usr/share/nginx/html
COPY deploy/sandbox/frontend-nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 3000
CMD ["nginx", "-g", "daemon off;"]
