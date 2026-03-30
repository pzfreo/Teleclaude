FROM python:3.12-slim

# System deps: git, Node.js, GitHub CLI, build tools for native packages
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        curl \
        ca-certificates \
        build-essential \
        pkg-config \
        libffi-dev \
        jq \
        docker.io \
        docker-cli \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI into user-writable npm prefix so the non-root
# teleclaude user can self-update at runtime via /new and /update.
ENV NPM_CONFIG_PREFIX=/home/teleclaude/.npm-global
RUN npm install -g @anthropic-ai/claude-code

# Install agent-browser (Vercel) + its MCP wrapper and Playwright/Chromium.
# Browsers are installed to /opt/playwright-browsers so the non-root user can read them.
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright-browsers
RUN npm install -g agent-browser agent-browser-mcp \
    && npx --yes playwright install --with-deps chromium \
    && chmod -R a+rx /opt/playwright-browsers

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Create non-root user (claude CLI refuses --dangerously-skip-permissions as root)
# Give them a proper home for pip/npm caches and local installs.
# ~/.claude/ is volume-mounted; we symlink ~/.claude.json into that directory so
# the Claude Code MCP configuration also survives container rebuilds.
RUN useradd -m -s /bin/bash teleclaude \
    && mkdir -p /home/teleclaude/.local/bin /home/teleclaude/.claude \
    && ln -s /home/teleclaude/.claude/claude.json /home/teleclaude/.claude.json \
    && chown -R teleclaude:teleclaude /home/teleclaude /home/teleclaude/.npm-global

# App directory
WORKDIR /app

# Install Python deps first (layer caching via lock file)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-editable

# Copy application code
COPY *.py VERSION ./

# Pre-create volume mount points owned by teleclaude so fresh volumes work
RUN mkdir -p /app/data /app/workspaces && chown -R teleclaude:teleclaude /app

USER teleclaude
ENV PATH="/app/.venv/bin:/home/teleclaude/.npm-global/bin:/home/teleclaude/.local/bin:${PATH}"

CMD ["python", "bot.py"]
