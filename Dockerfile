FROM python:3.11-slim

# Install Node.js, npm, git (needed for mcporter and git sync)
RUN apt-get update && apt-get install -y \
    nodejs npm git curl \
    && rm -rf /var/lib/apt/lists/*

# Install mcporter (Senpi MCP client)
RUN npm install -g mcporter

# Clone senpi-skills (DSL runner, watchdog, SM flip scripts live here)
RUN git clone --depth 1 https://github.com/Senpi-ai/senpi-skills.git /opt/senpi/senpi-skills

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the repo
COPY . .

# Default: run the worker (Railway can override with dashboard start command)
CMD ["python3", "worker.py"]
