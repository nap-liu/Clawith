ARG CLAWITH_IMAGE_MIRROR=docker.m.daocloud.io
FROM --platform=$BUILDPLATFORM ${CLAWITH_IMAGE_MIRROR}/library/node:20-alpine AS build
WORKDIR /app
COPY package*.json ./
RUN npm ci --registry https://registry.npmmirror.com
COPY . .
RUN npm run build

FROM ${CLAWITH_IMAGE_MIRROR}/library/nginx:alpine
COPY --from=build /app/dist /usr/share/nginx/html
# Source assets can arrive with owner-only permissions from macOS.  Nginx
# serves them as an unprivileged worker, so normalize the copied artifact tree.
RUN chmod -R a+rX /usr/share/nginx/html
COPY nginx.conf.template /etc/nginx/templates/default.conf.template
EXPOSE 3000
CMD ["nginx", "-g", "daemon off;"]
