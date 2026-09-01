ARG CLAWITH_IMAGE_MIRROR=docker.m.daocloud.io
ARG CLAWITH_NODE_BASE_DIGEST=sha256:fb4cd12c85ee03686f6af5362a0b0d56d50c58a04632e6c0fb8363f609372293
ARG CLAWITH_NGINX_BASE_DIGEST=sha256:db35bfc6b2951e7f8a72db5db120288c127ffaeeb4a6d4b95a26fead017d5913
FROM --platform=$BUILDPLATFORM ${CLAWITH_IMAGE_MIRROR}/library/node:20-alpine@${CLAWITH_NODE_BASE_DIGEST} AS build
WORKDIR /app
COPY package*.json ./
RUN npm ci --registry https://registry.npmmirror.com
COPY . .
RUN npm run build

FROM ${CLAWITH_IMAGE_MIRROR}/library/nginx:alpine@${CLAWITH_NGINX_BASE_DIGEST}
COPY --from=build /app/dist /usr/share/nginx/html
# Source assets can arrive with owner-only permissions from macOS.  Nginx
# serves them as an unprivileged worker, so normalize the copied artifact tree.
RUN chmod -R a+rX /usr/share/nginx/html
COPY nginx.conf.template /etc/nginx/templates/default.conf.template
EXPOSE 3000
CMD ["nginx", "-g", "daemon off;"]
