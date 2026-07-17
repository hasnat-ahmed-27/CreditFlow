# ---- build: compile the Vite app ------------------------------------------
FROM node:22-alpine AS build
WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY . .
# The gateway URL is baked at build time (static bundle). Default targets the
# compose-published gateway port on the host the browser runs on.
ARG VITE_GATEWAY_URL=http://localhost:8080
ENV VITE_GATEWAY_URL=${VITE_GATEWAY_URL}
RUN npm run build

# ---- serve: static files via nginx -----------------------------------------
FROM nginx:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
HEALTHCHECK --interval=10s --timeout=3s --retries=10 \
  CMD wget -q -O /dev/null http://localhost/ || exit 1
