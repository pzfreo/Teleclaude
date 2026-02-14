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
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI globally
RUN npm install -g @anthropic-ai/claude-code

# Create non-root user (claude CLI refuses --dangerously-skip-permissions as root)
# Give them a proper home for pip/npm caches and local installs
RUN useradd -m -s /bin/bash teleclaude \
    && mkdir -p /home/teleclaude/.local/bin \
    && chown -R teleclaude:teleclaude /home/teleclaude

# App directory
WORKDIR /app

# Install Python deps first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY *.py VERSION ./

# Pre-create volume mount points owned by teleclaude so fresh volumes work
RUN mkdir -p /app/data /app/workspaces && chown -R teleclaude:teleclaude /app

USER teleclaude
ENV PATH="/home/teleclaude/.local/bin:${PATH}"

CMD ["python", "bot.py"]
