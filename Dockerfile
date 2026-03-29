FROM python:3.11-slim

# Install Node.js
RUN apt-get update && apt-get install -y curl && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y nodejs && \
    apt-get clean

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

# Install claude CLI and Slack MCP server globally
RUN npm install -g @anthropic-ai/claude-code @notionhq/notion-mcp-server slack-mcp-server

COPY . .

RUN useradd -m appuser && chown -R appuser /app
USER appuser

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
