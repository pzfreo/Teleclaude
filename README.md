# Teleclaude

A Telegram bot that connects you to Claude with GitHub, web search, Google Tasks, Google Calendar, and Gmail. Code against your repos, search the web, manage tasks and calendar, and send emails — all from Telegram.

## Setup

### 1. Create a Telegram Bot

1. Open Telegram and message [@BotFather](https://t.me/BotFather)
2. Send `/newbot` and follow the prompts
3. Copy the bot token you receive

### 2. Get an Anthropic API Key

1. Go to [console.anthropic.com](https://console.anthropic.com/)
2. Create an API key

### 3. Create a GitHub Personal Access Token

1. Go to [github.com/settings/tokens](https://github.com/settings/tokens)
2. Create a token with `repo` scope (for full access to your repositories)

### 4. Google integration (optional — Tasks, Calendar, Gmail send)

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a project and enable **Google Tasks API**, **Google Calendar API**, and **Gmail API**
3. Go to Credentials > Create Credentials > **OAuth 2.0 Client ID** (Desktop app)
4. Run the setup script locally:

```bash
pip install google-auth-oauthlib google-api-python-client
python setup_google.py
```

5. It will open a browser for you to authorize. Copy the three values it prints.

### 5. Configure

```bash
cp .env.example .env
```

Edit `.env` and fill in your tokens:

```
TELEGRAM_BOT_TOKEN=your-telegram-bot-token
ANTHROPIC_API_KEY=sk-ant-...
GITHUB_TOKEN=ghp_...
```

Optional settings:
- `CLAUDE_MODEL` — which Claude model to use (default: `claude-sonnet-4-20250514`)
- `ALLOWED_USER_IDS` — comma-separated Telegram user IDs to restrict access

### 6. Run Locally

```bash
pip install -r requirements.txt
python bot.py
```

## Deploy to DigitalOcean

The included `deploy.sh` script handles creating, deploying, and managing a droplet.

```bash
# First time: create a droplet and deploy
./deploy.sh create

# Update an existing deployment
./deploy.sh

# Other commands
./deploy.sh logs      # Tail container logs
./deploy.sh ssh       # SSH into the droplet
./deploy.sh env       # Push updated .env files
./deploy.sh destroy   # Tear down the droplet
```

**Prerequisites:**
- `doctl` CLI authenticated with your DigitalOcean account
- An SSH key added to your DigitalOcean account
- `.env` file with your tokens (copy `.env.example`)

CI/CD is also configured via `.github/workflows/deploy.yml` — pushes to `main` auto-deploy to the droplet. Set `DROPLET_SSH_KEY` and `DROPLET_IP` as GitHub Actions secrets.

## Usage

### Commands

- `/repo owner/name` — set the active GitHub repo
- `/repo` — show the current repo
- `/new` — clear conversation history and start fresh
- `/model` — show which Claude model is active
- `/help` — show available commands

### Capabilities

All tools are optional — the bot enables whatever is configured.

**GitHub** (with `/repo`): read files, edit code, create branches, open PRs, view issues, search code

**Web search**: look up docs, error messages, or current info

**Google Tasks**: list, create, complete, update, and delete tasks

**Google Calendar**: view upcoming events, create events, manage calendars

**Gmail (send only)**: compose and send emails — Claude always confirms before sending. No read access.

### Example workflow

```
You: /repo myuser/myproject
Bot: Active repo set to: myuser/myproject (default branch: main)

You: What does the main entry point look like?
Bot: [reads and explains main.py]

You: Add input validation to the parse_config function
Bot: [creates branch, reads file, edits it, commits, opens PR]
```

## Security

- Set `ALLOWED_USER_IDS` to restrict who can use the bot
- Your `GITHUB_TOKEN` controls what repos Claude can access — use a fine-grained token scoped to specific repos if you want to limit access
- Gmail uses the `gmail.send` scope only — Claude cannot read your emails
- Claude will always confirm email details with you before sending
- Find your Telegram user ID by messaging [@userinfobot](https://t.me/userinfobot)
