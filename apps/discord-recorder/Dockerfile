FROM node:22-bookworm-slim

ENV NODE_ENV=production

WORKDIR /app

# opusscript is pure JavaScript and @snazzah/davey ships prebuilt binaries: no compiler needed.
COPY package.json package-lock.json ./
RUN npm ci --omit=dev

COPY src ./src

# uid 1000 owns the shared files volume (the API image's appuser has the same uid).
USER node

CMD ["node", "src/index.js"]
