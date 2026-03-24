FROM python:3.11-slim

# Install git, curl, Node.js (required for mcporter CLI)
RUN apt-get update && apt-get install -y \
    git curl \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install mcporter globally
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
ENV PYTHONUNBUFFERED=1
CMD ["python3", "worker.py"]
