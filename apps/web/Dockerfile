# Development image: runs `next dev` against the bind-mounted source.
FROM node:24-alpine

ENV NEXT_TELEMETRY_DISABLED=1

WORKDIR /app
RUN chown node:node /app
USER node

COPY --chown=node:node package.json package-lock.json ./
RUN npm ci

COPY --chown=node:node . .
# Pre-create so the anonymous volume mounted here is owned by `node`, not root.
RUN mkdir -p .next

EXPOSE 3000

CMD ["npm", "run", "dev", "--", "--hostname", "0.0.0.0", "--port", "3000"]
