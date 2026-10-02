#!/usr/bin/env sh
# Regenerate packages/shared from the backend's OpenAPI document.
#
#   scripts/generate-shared-types.sh
#
# 1. the API exports its OpenAPI document (needs the `api` service: `docker compose up -d api`)
# 2. openapi-typescript turns it into packages/shared/src/api.ts (run in a throwaway Node container,
#    so no Node is needed on the host)
# Commit both files. The backend test `test_shared_openapi_is_current` fails when openapi.json is stale.
set -eu
cd "$(dirname "$0")/.."
docker compose exec -T api python -m app.openapi_export > packages/shared/openapi.json
MSYS_NO_PATHCONV=1 docker run --rm -v "$(pwd)/packages/shared:/shared" -w /shared node:22-bookworm-slim \
  sh -c "npm install --no-audit --no-fund --silent && npx openapi-typescript openapi.json -o src/api.ts"
echo "packages/shared/src/api.ts regenerated"
