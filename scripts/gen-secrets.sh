#!/usr/bin/env sh
# Print fresh random secrets for the production .env. Nothing is written; paste what you need.
#
#   sh scripts/gen-secrets.sh
#
# JWT_SECRET and POSTGRES_PASSWORD are hex (safe inside the database URL); ENCRYPTION_KEY is a Fernet key
# (url-safe base64 of 32 random bytes), which encrypts the credentials users store. Keep it: without it
# those stored credentials can't be decrypted.
set -eu
command -v openssl >/dev/null 2>&1 || { echo "openssl is needed" >&2; exit 1; }
echo "JWT_SECRET=$(openssl rand -hex 32)"
echo "POSTGRES_PASSWORD=$(openssl rand -hex 24)"
echo "ENCRYPTION_KEY=$(openssl rand -base64 32 | tr '+/' '-_')"
echo "FLOWER_PASSWORD=$(openssl rand -hex 12)"
