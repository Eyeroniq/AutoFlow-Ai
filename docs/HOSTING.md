# Hosting FlowForge AI

A standalone guide for putting FlowForge on the internet. It assumes you have forgotten everything. Nothing here has
been deployed yet: the production stack (`compose.prod.yaml`) was built and run end to end on a laptop with a
dummy domain (`localhost`, Caddy's local certificate authority), and every service started healthy. The first real
deployment is the one thing still untested; the [troubleshooting](#7-troubleshooting) section is written for it.

## 1. Quick summary

1. Rent a small Linux server (a VPS) with 4 GB RAM or more. Install Docker.
2. Buy a domain; point an `A` record at the server's IP.
3. On the server: `git clone`, `cp .env.production.example .env`, fill in `DOMAIN`, `PUBLIC_URL`, and the secrets
   (`sh scripts/gen-secrets.sh` prints them).
4. `docker compose -f compose.prod.yaml up -d --build`
5. Open `https://your-domain/health`. Caddy fetches the HTTPS certificate by itself.
6. Create your account at `https://your-domain/register`, and if this is public, set `PUBLIC_DEMO=true`
   with `DEMO_OWNER_EMAIL` set to that account.

Backups run daily by themselves. Costs: about 5-25 USD a month for the server, a domain about 10-15 USD a year,
AI keys free (with rate limits).

## 2. What runs where

`compose.prod.yaml` starts these containers (project name `flowforge-prod`):

| Service | What it does | Published port |
| --- | --- | --- |
| `caddy` | The only public entry point. HTTPS, routes `/api`, `/ws`, `/health`, `/docs` to the API and everything else to the web app | 80, 443 |
| `web` | The Next.js app (production build) | none |
| `api` | FastAPI; runs database migrations on start | none |
| `worker-default`, `worker-llm`, `worker-ocr`, `worker-audio` | Celery workers, one per queue | none |
| `beat` | Fires schedules and email triggers once a minute | none |
| `postgres` (with pgvector), `redis` | Data and queues | **none** (never exposed) |
| `backup` | Daily verified `pg_dump` | none |

Optional, switched on with a Compose profile: `monitoring` (Flower) and `bots` (`telegram-listener`, `discord-bot`,
`discord-recorder`). The bots refuse to run without their tokens, so leave the profile off until you have them.

Because Caddy serves the web app and the API on the **same origin**, the browser needs no cross-origin requests, and
the live-run WebSocket is `wss://your-domain/ws/...` automatically.

## 3. Choosing where to host

| | VPS (DigitalOcean, Hetzner, Vultr...) | Railway / Render (managed) |
| --- | --- | --- |
| Fits this repo | Yes, directly: it is a Compose stack | Awkward: one service per container, you re-create the topology by hand, Postgres needs the pgvector extension, volumes and the Celery workers each bill separately |
| Rough monthly cost* | Hetzner CX22-class (2 vCPU, 4 GB): about 5 EUR. DigitalOcean 2 vCPU / 4 GB droplet: about 24 USD. 8 GB tier: 12 EUR Hetzner / 48 USD DigitalOcean | Roughly 8 services + Postgres + Redis, each with memory billed: expect about 40-90 USD |
| Effort | You maintain the OS (updates, firewall) | Less OS work, more configuration work |
| Backups | The included backup service (copy them off the box!) | Use the platform's database backups plus your own dump |

\* Approximate and from memory; prices change, so check the provider's page before buying.

**Recommendation:** a VPS. This stack is already a Compose file, the single biggest cost is memory (several Python
workers plus Tesseract and Whisper), and a VPS gives the most RAM per dollar. Start with 4 GB. If audio transcription
with local faster-whisper or large PDFs make it swap or get OOM-killed, move to 8 GB (or set `WORKER_*_CONCURRENCY`
lower, see [tuning](#environment-variables)).

## 4. Step by step

### 4.1 Domain and DNS

Buy a domain from any registrar (Namecheap, Cloudflare Registrar, Porkbun...). Then, at the registrar's DNS page (or
wherever its nameservers point), add:

| Type | Name / host | Value | Why |
| --- | --- | --- | --- |
| `A` | `@` (the bare domain) or e.g. `flowforge` for a subdomain | your server's IPv4 | Sends browsers to your server |
| `AAAA` | same name | your server's IPv6, only if the server has one | Same for IPv6 clients. Do not add it if you are unsure: a wrong AAAA makes some visitors fail |
| `CAA` (optional) | `@` | `0 issue "letsencrypt.org"` | Only Let's Encrypt may issue certificates for the domain |

Use a subdomain (`flowforge.example.com`) if the bare domain is used for something else. If you use Cloudflare DNS,
set the records to **DNS only** (grey cloud) at first so Caddy can get its certificate, then decide about proxying
later. `DOMAIN` in `.env` must be exactly that hostname.

Caddy asks Let's Encrypt for a certificate the first time someone connects, and Let's Encrypt must reach your server
on port 80 (and 443) **from the internet**. So DNS must already point at the server before you start the stack.

### 4.2 Provision the server

1. Create the server: Ubuntu 24.04 LTS, 4 GB RAM, add your SSH key. Note its public IP (put it in the `A` record).
2. SSH in as root, create a normal user, and keep the system updated:
   ```bash
   adduser deploy && usermod -aG sudo deploy
   apt update && apt -y upgrade
   ```
3. Firewall: only SSH, HTTP and HTTPS.
   ```bash
   ufw allow OpenSSH && ufw allow 80/tcp && ufw allow 443/tcp && ufw enable
   ```
   Note that Docker adds its own rules for published ports and bypasses `ufw`. That is fine here because the
   production Compose file publishes only Caddy's 80 and 443. Postgres and Redis are not published at all; do not add
   `ports:` to them.
4. Install Docker Engine with the Compose plugin (https://docs.docker.com/engine/install/ubuntu/), then
   `usermod -aG docker deploy` and log in again as `deploy`. Check `docker compose version` is v2.24 or newer.

### 4.3 Get the code and fill in `.env`

```bash
git clone https://github.com/Eyeroniq/AutoFlow-Ai.git
cd AutoFlow-Ai
cp .env.production.example .env
sh scripts/gen-secrets.sh        # prints JWT_SECRET, POSTGRES_PASSWORD, ENCRYPTION_KEY, FLOWER_PASSWORD
nano .env                        # paste those, set DOMAIN and PUBLIC_URL, add AI keys
```

**Back up `ENCRYPTION_KEY` somewhere outside the server (a password manager).** It encrypts every credential users
saved. A database backup restored without the matching key still works, but the stored credentials cannot be read.

The API refuses to start with `ENVIRONMENT=production` if something is unsafe (short or default `JWT_SECRET`, missing
`ENCRYPTION_KEY`, wildcard CORS, `PUBLIC_DEMO` without an owner). It prints every problem at once.

### 4.4 Start it

```bash
docker compose -f compose.prod.yaml up -d --build     # first build takes several minutes
docker compose -f compose.prod.yaml ps                # everything should become "healthy" / running
docker compose -f compose.prod.yaml logs -f caddy     # watch the certificate being obtained
```

Using a different env file (for testing) is `ENV_FILE=.env.other docker compose --env-file .env.other -f compose.prod.yaml ...`.

The web image bakes `PUBLIC_URL` in at build time. **If you change `PUBLIC_URL` later, re-run with `--build`.**

Optional profiles:

```bash
docker compose -f compose.prod.yaml --profile monitoring up -d   # Flower
docker compose -f compose.prod.yaml --profile bots up -d         # Telegram listener, Discord bot + recorder
```

Flower is bound to the server's `127.0.0.1` only. Reach it from your laptop with an SSH tunnel and then open
<http://localhost:5555> (log in with `FLOWER_USER` / `FLOWER_PASSWORD`):

```bash
ssh -L 5555:127.0.0.1:5555 deploy@your-server
```

### 4.5 Create your account

Open `https://your-domain/register` and register. There is no seeded demo user in production. If you are going public,
this must be the email you put in `DEMO_OWNER_EMAIL`.

## Environment variables

All of them live in `.env` (template: `.env.production.example`). `REQUIRED` means the stack will not start without it.

**Your site**

| Variable | Meaning |
| --- | --- |
| `DOMAIN` | REQUIRED. The hostname Caddy serves and gets a certificate for (no `https://`). |
| `PUBLIC_URL` | REQUIRED. `https://` + the domain. Baked into the web build as the API address and used as the only allowed CORS origin. |
| `ACME_EMAIL` | Contact for Let's Encrypt expiry notices. Optional, recommended. |
| `CORS_ORIGINS` | Comma-separated exact origins. Defaults to `PUBLIC_URL`. `*` is refused. Only needed if you add another front end. |

**Secrets**

| Variable | Meaning |
| --- | --- |
| `JWT_SECRET` | REQUIRED. 32+ random characters. Signs logins and the session cookie. Changing it signs everyone out. |
| `ENCRYPTION_KEY` | REQUIRED. Fernet key for stored credentials. Never lose it. To rotate, set `NEW,OLD` (new first). |
| `POSTGRES_PASSWORD` | REQUIRED. Database password. Use hex (the generator does) to avoid URL-escaping trouble. |
| `POSTGRES_USER`, `POSTGRES_DB` | Default `flowforge`. Only changeable before the first start (the database already exists afterwards). |

**Public demo** (see section 5)

| Variable | Meaning |
| --- | --- |
| `PUBLIC_DEMO` | `false` (default) or `true`. |
| `DEMO_OWNER_EMAIL` | REQUIRED when `PUBLIC_DEMO=true`. The one account treated as the owner. |
| `DEMO_RUNS_PER_DAY` | Runs per visitor per day (default 25). |
| `DEMO_TOKENS_PER_DAY` | Estimated LLM tokens per visitor per day (default 60000). |

**AI providers.** These keys are what visitors share in demo mode, and your default otherwise: `GEMINI_API_KEY`,
`GROQ_API_KEY`, `OPENROUTER_API_KEY`, `MISTRAL_API_KEY`, `CEREBRAS_API_KEY`, `TAVILY_API_KEY` (web search fallback).
Leave any blank to disable that provider. They are free tiers with rate limits (see section 8).

**Your own accounts** (never lent to visitors in demo mode): `SMTP_USER` / `SMTP_PASSWORD` (Gmail App Password),
`TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`, `DISCORD_WEBHOOK_URL`, `NOTION_API_KEY`, `AIRTABLE_API_KEY`. As owner you
can also connect these in the Integrations page instead of `.env`.

**Backups**

| Variable | Meaning |
| --- | --- |
| `BACKUP_DIR` | Host folder for dumps (default `./backups` next to the compose file). Use an absolute path to put it on another disk. |
| `BACKUP_HOUR_UTC` | Hour the daily backup runs (default 3). |
| `BACKUP_KEEP_DAYS` | Older backups are deleted (default 14). |
| `BACKUP_FILES` | Also archive uploaded files (default `true`). |

**Bots and monitoring (profiles)**: `FLOWER_USER`, `FLOWER_PASSWORD`; `DISCORD_BOT_TOKEN` (the channel comes from a Discord Voice Meeting block in a pipeline; `DISCORD_MONITOR_GUILD_ID` / `DISCORD_MONITOR_CHANNEL_ID` are only a fallback) (see the README's Discord and Telegram sections for creating them).

**Tuning (optional)**: `API_WORKERS`, `WORKER_DEFAULT_CONCURRENCY`, `WORKER_LLM_CONCURRENCY`, `WORKER_OCR_CONCURRENCY`,
`WORKER_AUDIO_CONCURRENCY` (lower these on small servers), `HTTP_PORT` / `HTTPS_PORT` (leave at 80/443 on a real
server), `FLOWER_HOST_PORT`.

Set automatically by `compose.prod.yaml` (do not put in `.env`): `ENVIRONMENT=production`, `COOKIE_SECURE=true`,
`TRUST_PROXY_HEADERS=true` (so the rate limiter sees real client IPs from Caddy).

## 5. PUBLIC_DEMO: off or on

**Off (default).** A private instance for you. Anyone who registers can use the server's AI keys and, as there is no
owner concept, you should leave registration to yourself (do not share the URL). Your `.env` accounts (Gmail, Telegram,
Discord, Notion, Airtable) are usable by every account on the instance, so only do this alone or with people you trust.

**On.** Safe to show to strangers:

- Everyone except `DEMO_OWNER_EMAIL` is a **visitor**.
- Visitors run pipelines with the server's AI keys but are capped per account per day (`DEMO_RUNS_PER_DAY`,
  `DEMO_TOKENS_PER_DAY`). Over the cap a run fails validation with a message saying the daily limit is reached. The
  visitor-facing integrations page hides the server's AI providers and the API refuses visitors who try to
  connect AI keys.
- **Per-node credential safety.** For Gmail, Discord, Telegram, Notion and Airtable the server's own credentials are
  never used for a visitor, whatever the node config says. The node shows an inline **Connect your own [service]**
  button with a slide-over explaining how to get the key, without leaving the editor. Visitors can only use what they
  connect themselves. No endpoint returns the owner's values, and `apps/api/tests/test_public_demo.py` proves it.
- The **Telegram Command Center**, **Discord recorder/bot** and any "act for the owner" automation refuse
  non-owner accounts. (There is no separate spending tracker in the project; the run/token caps are the spend control.)

Because AI keys are shared, a popular demo can exhaust a free quota. See section 8.

To switch: edit `.env` (`PUBLIC_DEMO=true`, `DEMO_OWNER_EMAIL=you@...`), then
`docker compose -f compose.prod.yaml up -d` (services recreate with the new values; no rebuild needed).

## 6. After it is live: verify

Work through in order; each step narrows the problem.

1. `dig +short your-domain` shows the server's IP (DNS has propagated).
2. `curl -I http://your-domain` returns a redirect to `https://`.
3. `curl -fsS https://your-domain/health` returns `{"status":"ok"...}` with database and redis `ok`. (It returns 503
   with the failing component if not.)
4. `docker compose -f compose.prod.yaml ps` shows every service `healthy` or `running`.
5. In a browser: `https://your-domain/login` loads with the padlock; registering works; opening
   `/dashboard` while logged out redirects to the login page.
6. Create a pipeline from a template, press **Run**, and watch the nodes turn green live (this proves the
   WebSocket works through Caddy as `wss`).
7. `https://your-domain/docs` shows the API docs.
8. If `PUBLIC_DEMO=true`: log in as another account; the Integrations page shows no AI providers, and a Gmail node
   shows "Connect your own Gmail" instead of sending.
9. Take a manual backup (next section) and confirm the file appears.
10. Optional: `curl -sS -o /dev/null -w '%{http_code}' ...` against `/api/auth/login` eleven times quickly to see a `429`
    (the rate limiter works through the proxy).

## Backups and restore

The `backup` service writes one verified dump a day at `BACKUP_HOUR_UTC` into `BACKUP_DIR`
(`flowforge-YYYYmmdd-HHMMSS.dump`, plus `flowforge-files-...tar.gz` for uploads). A dump that cannot be read back is
discarded. Old ones are deleted after `BACKUP_KEEP_DAYS`.

```bash
docker compose -f compose.prod.yaml logs backup                     # when it last ran
docker compose -f compose.prod.yaml exec backup /scripts/backup.sh once   # take one now
ls -lh backups/
```

**A backup on the same disk dies with the disk.** Copy `backups/` off the server regularly (for example a nightly
`rclone` or `rsync` to another machine or cloud storage), and keep `ENCRYPTION_KEY` with it.

Restore (replaces the current database; it stops the app, restores, and starts it again):

```bash
sh scripts/restore-db.sh backups/flowforge-20261010-030000.dump
# with uploads:
sh scripts/restore-db.sh backups/flowforge-20261010-030000.dump --files backups/flowforge-files-20261010-030000.tar.gz
```

It asks you to type `restore` first (`ASSUME_YES=1` skips this). Use `ENV_FILE=... COMPOSE_FILE=...` for non-default
files. To restore onto a brand-new server: do sections 4.1-4.3, start only `postgres` (`docker compose -f compose.prod.yaml up -d postgres`),
run the restore script, then start everything. Always test a restore once, early, rather than discovering a problem
on the bad day.

## Updating

```bash
git pull
docker compose -f compose.prod.yaml up -d --build
```

The API runs new migrations when it starts. Take a manual backup first.

## 7. Troubleshooting

**DNS not propagated.** Symptom: browser says the site cannot be found, or Caddy logs show Let's Encrypt cannot
validate. Check `dig +short your-domain` against your server IP; propagation can take minutes to hours. Make sure you
edited the records at the place your nameservers actually point (a registrar vs a DNS provider). Check
https://dnschecker.org. Do not keep restarting Caddy: Let's Encrypt rate-limits repeated failures, so fix DNS first.

**Caddy certificate fails.** `docker compose -f compose.prod.yaml logs caddy`. Usual causes: DNS wrong (above);
ports 80/443 blocked by the provider's firewall or `ufw` (open them, and check the cloud provider's firewall panel
too); something else already listens on 80/443 (`ss -ltnp | grep -E ':80|:443'`); `DOMAIN` has a typo or `https://`
in it; a Cloudflare orange-cloud proxy interfering (switch the record to DNS only). Certificates are stored in the
`caddy_data` volume, so they survive restarts, and `down -v` deletes them (and everything else).

**CORS errors in the browser console.** `PUBLIC_URL` (or `CORS_ORIGINS`) must exactly equal the address in the
address bar: scheme, host and no trailing slash (`https://example.com`, not `http://` and not `https://www.example.com`
unless that is what you use). Fix it in `.env`, run `docker compose -f compose.prod.yaml up -d --build`
(the web image bakes the URL in). Wildcards are refused on purpose.

**A service does not start.** `docker compose -f compose.prod.yaml ps` then `logs <service>`.
- `api` exits at once and lists problems: that is the production safety check, so read the list (weak/missing
  `JWT_SECRET`, missing `ENCRYPTION_KEY`, wildcard CORS, demo without owner). Fix `.env`.
- `api` cannot reach the database: `POSTGRES_PASSWORD` changed after the volume was first created. Postgres keeps the
  old password; either put the old one back or reset the volume (`down -v` destroys data).
- Containers killed or restarting at random: out of memory. `docker stats`, `dmesg | grep -i oom`; add swap, lower
  `WORKER_*_CONCURRENCY`, or use a bigger server.
- `web` build fails or is killed: building Next.js needs memory; build on a 4 GB server with no other load, or add
  swap.
- `caddy` stays `unhealthy` but the site works: its health check uses Caddy's admin port inside the container; look at
  the logs.
- Bot services exit immediately: their tokens are blank. Only start `--profile bots` once they are filled in.

**Everything is up but runs never finish.** Check `worker-*` logs and Flower; Redis must be healthy. Runs that
need OCR sit in the `ocr` queue and need `worker-ocr`.

**Rate limiter blocks everyone / the wrong IP.** Behind Caddy, `TRUST_PROXY_HEADERS=true` makes the limiter use the real
client IP; if you put another proxy (like Cloudflare) in front of Caddy, all clients may appear as one IP.

**Locked out of the secrets.** If `ENCRYPTION_KEY` is lost, saved credentials are unreadable: users must reconnect
them. Logins and data are unaffected.

## 8. Costs, limits, and a reminder about AI keys

| Item | Rough cost |
| --- | --- |
| VPS, 4 GB | 5 EUR (Hetzner) to 24 USD (DigitalOcean) per month |
| VPS, 8 GB | 12 EUR to 48 USD per month |
| Domain | 10-15 USD per year |
| HTTPS certificate (Let's Encrypt via Caddy) | free |
| Backups offsite | free to a few USD, depending on storage |
| AI providers | free tiers, 0 USD |

Prices are approximate; confirm on the provider's site.

**The free AI keys have rate limits.** Gemini, Groq, OpenRouter (`:free` models), Mistral and Cerebras free tiers all
cap requests per minute and per day, and can return "high demand" errors (503) at busy times. In demo mode every
visitor draws on the same quotas, so a few active visitors can exhaust them and runs will fail with provider
errors, not bugs. Mitigations: keep `DEMO_RUNS_PER_DAY` / `DEMO_TOKENS_PER_DAY` low, configure several providers so
pipelines can use a different one, and watch Flower/the logs. If the demo grows, move to a paid key (you
are then responsible for what visitors can spend, so keep the caps).

## 9. Handy commands

```bash
docker compose -f compose.prod.yaml ps                       # status
docker compose -f compose.prod.yaml logs -f api worker-llm   # follow logs
docker compose -f compose.prod.yaml restart api              # restart one service
docker compose -f compose.prod.yaml down                     # stop (keeps data)
docker compose -f compose.prod.yaml exec postgres psql -U flowforge flowforge
```

Never run `down -v` on a server with data you care about: it deletes the database, uploads and certificates.
