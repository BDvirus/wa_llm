# 📱 WhatsApp Group Summary Bot

![release version](https://img.shields.io/github/v/release/ilanbenb/wa_llm)
![Build Image](https://github.com/ilanbenb/wa_llm/actions/workflows/docker.yml/badge.svg)
![Release](https://github.com/ilanbenb/wa_llm/actions/workflows/release.yml/badge.svg)

AI-powered WhatsApp bot that **joins any group, tracks conversations, and generates intelligent summaries**.

---

## Features

- 🤖 Automated group chat responses (when mentioned)
- 📝 Smart **LLM-based conversation summaries**
- 📚 Knowledge base integration for context-aware answers
- 📂 Persistent message history with PostgreSQL + `pgvector`
- 🔗 Support for multiple message types (text, media, links)
- 👥 Group management & customizable settings
- 🔕 **Opt-out feature**: Users can opt-out of being tagged in summaries/answers via DM.
- ⚡ REST API with Swagger docs (`localhost:8000/docs`)

---

## 🐳 Docker Compose Configurations

This project includes multiple Docker Compose files for different environments:

| File                           | Purpose                                                                        | Usage                                                  |
| ------------------------------ | ------------------------------------------------------------------------------ | ------------------------------------------------------ |
| `docker-compose.yml`           | **Default/Development**. Builds the application from source code.              | `docker compose up -d`                                 |
| `docker-compose.prod.yml`      | **Production**. Uses pre-built images from GHCR. Recommended for deployment.   | `docker compose -f docker-compose.prod.yml up -d`      |
| `docker-compose.local-run.yml` | **Local Execution**. For running the app on host while services run in Docker. | `docker compose -f docker-compose.local-run.yml up -d` |
| `docker-compose.base.yml`      | **Base Configuration**. Contains shared service definitions.                   | ❌ **Do not use directly**                             |

---

## 📋 Prerequisites

- 🐳 Docker and Docker Compose
- 🐍 Python 3.13+
- 🗄️ PostgreSQL with `pgvector` extension
- 🔑 Voyage AI API key
- 📲 WhatsApp account for the bot

## Quick Start

### 1. Clone & Configure

`git clone https://github.com/YOUR_USER/wa_llm.git
cd wa_llm`

### 2. Create .env file

- Copy `.env.example` to `.env` and fill in required values.

```
cp .env.example .env
```

#### Environment Variables

<div style="font-size: 10px;">

| Variable                       | Description                                                                        | Default                                                      |
| ------------------------------ | ---------------------------------------------------------------------------------- | ------------------------------------------------------------ |
| `WHATSAPP_HOST`                | WhatsApp Web API URL                                                               | `http://localhost:3000`                                      |
| `WHATSAPP_BASIC_AUTH_USER`     | WhatsApp API user                                                                  | `admin`                                                      |
| `WHATSAPP_BASIC_AUTH_PASSWORD` | WhatsApp API password                                                              | `admin`                                                      |
| `VOYAGE_API_KEY`               | Voyage AI key                                                                      | –                                                            |
| `DB_URI`                       | PostgreSQL URI                                                                     | `postgresql+asyncpg://user:password@localhost:5432/postgres` |
| `LOG_LEVEL`                    | Log level (`DEBUG`, `INFO`, `ERROR`)                                               | `INFO`                                                       |
| `MODEL_NAME`                   | LLM to use, as `provider:model`. Selects which API key is required.                | `anthropic:claude-sonnet-4-6`                                |
| `ANTHROPIC_API_KEY`            | Anthropic API key (starts with `sk-`). Required when `MODEL_NAME` uses `anthropic:`. | –                                                            |
| `OPENROUTER_API_KEY`           | OpenRouter API key (starts with `sk-or-`). Required when `MODEL_NAME` uses `openrouter:`. | –                                                            |
| `LOGFIRE_TOKEN`                | Logfire monitoring key, You need to have a real logfire key here                   | –                                                            |
| `DM_AUTOREPLY_ENABLED`         | Enable auto-reply for direct messages                                              | `False`                                                      |
| `DM_AUTOREPLY_MESSAGE`         | Message to send as auto-reply                                                      | `Hello, I am not designed to answer to personal messages.`   |

</div>

#### Choosing a model provider

`MODEL_NAME` decides which LLM the bot talks to, and only the API key for that provider is required.

- **Anthropic (default)** — `MODEL_NAME=anthropic:claude-sonnet-4-6` with `ANTHROPIC_API_KEY`.
- **OpenRouter** — `MODEL_NAME=openrouter:<vendor>/<model>` (e.g. `openrouter:anthropic/claude-sonnet-4.6`) with `OPENROUTER_API_KEY`. `ANTHROPIC_API_KEY` is then not needed at all.

A mismatch — an `openrouter:` model with no `OPENROUTER_API_KEY`, say — fails at startup with an explicit error rather than on the first message.

### 3. Starting the Services

**Option A: Development (Build from source)**

```bash
docker compose up -d
```

**Option B: Production (Use pre-built images)**

```bash
docker compose -f docker-compose.prod.yml up -d
```

### 4. Connect your device

1. Open http://localhost:3003 (gowa's UI; in production it is behind Traefik at `https://$GOWA_HOST`)
2. Scan the QR code with your WhatsApp mobile app.
3. Invite the bot device to any target groups you want to summarize.
4. Restart service: `docker compose restart wa_llm-web-server`

### 5. Activating the Bot for a Group

Open the admin page at `http://localhost:8000/admin` (in production: `https://$WA_LLM_HOST/admin`, behind Authentik). It lists every group the bot is in, with per-group stats, and lets you:

- **Turn the bot on or off** for a group. Turning it on opens a confirmation showing how many messages have piled up. Keep **"Start from now"** checked unless you want that history summarized and ingested: the bot stores messages from unmanaged groups too, so a group it has sat in for months would otherwise send its whole backlog to the LLM in the first summary.
- **Toggle spam alerts** (`notify_on_spam`). Only possible when the group has an owner to tag.
- **Edit community keys.** Groups sharing a key receive each other's summaries *and* share one knowledge-base search.
- **Run a knowledge-base ingest or send a summary** for one group. Jobs run in the background and report their real outcome — including "skipped: fewer than 15 new messages".

No restart is needed. The same settings are plain columns on the `"group"` table if you ever need SQL.

### 6. API usage

Swagger docs available at: `http://localhost:8000/docs`

#### Key Endpoints

- <b>/load_new_kbtopic (POST)</b> Loads a new knowledge base topic, prepares content for summarization.
- <b>/trigger_summarize_and_send_to_groups (POST)</b> Generates & dispatches summaries, Sends summaries to all managed groups

### 7. Opt-Out Feature

Users can control whether they are tagged in bot-generated messages (summaries, answers) by sending Direct Messages (DMs) to the bot:

| Command   | Description                                                                        |
| :-------- | :--------------------------------------------------------------------------------- |
| `opt-out` | Opt-out of being tagged. Your name will be displayed as text instead of a mention. |
| `opt-in`  | Opt-in to being tagged (default).                                                  |
| `status`  | Check your current opt-out status.                                                 |

> **Note:** This only affects messages generated by the bot. It does not prevent other users from tagging you manually.

### 8. Private Chat with the Bot

Members can message the bot privately about the groups they belong to. It is **off by default**: turn on **"שאלות פרטיות"** for a group in the admin page (only possible for managed groups).

| Message              | What happens                                                                  |
| :------------------- | :---------------------------------------------------------------------------- |
| any question         | Answered from the knowledge of *your* groups, citing the group when relevant  |
| `חפש <words>`        | Full-text search of your groups' messages (also `/search`)                    |
| `מה פספסתי`          | Personal catch-up of your groups since you last asked (also `/missed`)        |
| `עזרה`               | Lists your groups and these commands (also `/help`)                           |

- **Access** — only members of a managed group with private questions enabled, and only that group's knowledge. Membership is synced from gowa (by phone JID and LID) at startup and on every group event; the admin page shows the member count per group, and "חברים לא סונכרנו" means gowa returned none for it.
- **Everyone else** gets the existing behaviour (the DM auto-reply if enabled, otherwise nothing) — the bot doesn't reveal which groups exist.
- **Privacy** — answers never contain phone numbers; people appear by their WhatsApp name.
- **Quota** — `DM_DAILY_QUOTA` requests per user per rolling 24 hours (default 20).
- If the bot leaves a group, its members stay on record until you un-manage the group.

---

## 🚀 Production Deployment

To deploy in a production environment using the optimized configuration:

1. **Create Production Environment File**:
   Copy `.env.example` to `.env.prod` and configure your production secrets.

   ```bash
   cp .env.example .env.prod
   ```

2. **Start Services**:
   ```bash
   docker compose -f docker-compose.prod.yml up -d
   ```

This configuration includes:

- Automatic restart policies (`restart: always`)
- **No published ports for the web server or gowa.** Both are reached only through [Traefik](https://traefik.io/) with [Authentik](https://goauthentik.io/) forward-auth in front. Postgres is still published on `5432`.

It expects an existing Traefik instance with an Authentik forward-auth middleware. Set these in the environment compose reads (for Komodo, the stack's Environment):

| Variable               | Description                                      | Default             |
| ---------------------- | ------------------------------------------------ | ------------------- |
| `WA_LLM_HOST`          | Hostname for the web server / admin page         | – (required)        |
| `GOWA_HOST`            | Hostname for gowa's UI (QR pairing)              | – (required)        |
| `TRAEFIK_NETWORK`      | Traefik's external docker network                | `proxy`             |
| `TRAEFIK_ENTRYPOINT`   | Traefik entrypoint with TLS                      | `websecure`         |
| `AUTHENTIK_MIDDLEWARE` | Traefik middleware doing Authentik forward-auth  | `authentik@docker`  |
| `FORWARDED_ALLOW_IPS`  | Proxies uvicorn trusts for `X-Forwarded-*`. Narrow it to Traefik's IP/subnet; `*` trusts any container on the network | `*` |

Because port 8000 is no longer published, run the helper scripts in `app/` inside the container:

```bash
docker compose -f docker-compose.prod.yml exec web-server python app/load_new_kbtopics_task.py
```

See [`docs/plans/security-hardening.md`](docs/plans/security-hardening.md) for the remaining hardening (webhook signatures, secrets, gowa credentials).

---

## Developing

### Setup

Install dependencies using `uv`:

```bash
uv sync --all-extras --dev
```

### Development Commands

The project uses **Poe the Poet** for task automation with parallel execution:

```bash
# Run all checks (format, then parallel lint/typecheck/test)
uv run poe check

# Individual tasks
uv run poe format     # Format code with ruff
uv run poe lint       # Lint code with ruff
uv run poe typecheck  # Type check with pyright
uv run poe test       # Run tests with pytest

# List all available tasks
uv run poe
```

The `check` command runs formatting first, then executes linting, type checking, and testing **in parallel** for faster execution.

### Key Files

- Main application: `app/main.py`
- WhatsApp client: `src/whatsapp/client.py`
- Message handler: `src/handler/__init__.py`
- Database models: `src/models/`

---

## Architecture

The project consists of several key components:

- FastAPI backend for webhook handling
- WhatsApp Web API client for message interaction
- PostgreSQL database with vector storage for knowledge base
- AI-powered message processing and response generation

---

## Contributing

1. Fork the repository
2. Create a feature branch
3. Submit a pull request

---

## License

[LICENCE](CODE_OF_CONDUCT.md)
