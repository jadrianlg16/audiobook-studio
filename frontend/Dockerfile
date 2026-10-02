# Build context = ./frontend (see docker-compose.yml).
FROM node:22-alpine AS build
WORKDIR /app
# Copy the lockfile too and use `npm ci` for reproducible, lockfile-honoring
# installs (npm install can silently resolve different transitive versions).
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM nginx:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
