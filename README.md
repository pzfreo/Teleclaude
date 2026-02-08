# Teleclaude

A Telegram bot that connects you to Claude with GitHub, web search, and Google Tasks. Chat with Claude, code against your repos, search the web, and manage your tasks — all from Telegram.

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

### 4. Google Tasks (optional)

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a project and enable the **Google Tasks API**
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

## Deploy to Railway

1. Push this repo to GitHub
2. Go to [railway.app](https://railway.app) and create a new project from your repo
3. Add environment variables in the Railway dashboard:
   - `TELEGRAM_BOT_TOKEN`
   - `ANTHROPIC_API_KEY`
   - `GITHUB_TOKEN`
   - `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN` (if using Tasks)
   - (and any optional ones from `.env.example`)
4. Railway will auto-detect the `Procfile` and deploy

## Usage

### Commands

- `/repo owner/name` — set the active GitHub repo
- `/repo` — show the current repo
- `/new` — clear conversation history and start fresh
- `/model` — show which Claude model is active
- `/help` — show available commands

### GitHub capabilities

Once you set a repo with `/repo`, Claude can:

- **Read files** — browse directories, read source code
- **Edit files** — create or update files with commits
- **Manage branches** — create feature branches
- **Open PRs** — create pull requests with descriptions
- **View issues** — list and read issues
- **Search code** — find code by keyword
- **Web search** — look up docs, error messages, or current info (always available, no repo needed)
- **Google Tasks** — list, create, complete, update, and delete tasks

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
- Find your Telegram user ID by messaging [@userinfobot](https://t.me/userinfobot)
