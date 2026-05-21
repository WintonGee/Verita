# Ops console SPA — Vite dev server, containerized so the whole demo comes up
# with `docker compose up`. (Dev server, not a production build: it provides the
# API proxy that keeps the session cookie + CSRF same-origin.)
FROM node:20-alpine
WORKDIR /app

COPY package.json package-lock.json* ./
RUN npm install

COPY . .

EXPOSE 5174
CMD ["npm", "run", "dev"]
