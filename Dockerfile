FROM python:3.12-slim

# System deps: git (for workspace clones), Node.js (for claude CLI)
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        curl \
        ca-certificates \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI globally
RUN npm install -g @anthropic-ai/claude-code

# Create non-root user (claude CLI refuses --dangerously-skip-permissions as root)
RUN useradd -m -s /bin/bash teleclaude

# App directory
WORKDIR /app

# Install Python deps first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY *.py VERSION ./

# Ensure app owns its directories
RUN chown -R teleclaude:teleclaude /app

# Persistent volumes mounted at runtime
VOLUME ["/app/data", "/app/workspaces"]

USER teleclaude

CMD ["python", "bot.py"]
