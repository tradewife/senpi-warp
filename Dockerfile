FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    git curl \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g mcporter

RUN git clone --depth 1 https://github.com/Senpi-ai/senpi-skills.git /opt/senpi/senpi-skills

RUN git clone --depth 1 https://github.com/tradewife/hermes-apollo.git /opt/hermes-apollo

WORKDIR /opt/hermes-apollo
RUN pip install uv && \
    uv venv venv --python 3.11 && \
    . venv/bin/activate && \
    uv pip install -e ".[all]" && \
    uv pip install -e "./mini-swe-agent" || true && \
    uv pip install -e "./tinker-atropos" || true

RUN mkdir -p /root/.local/bin && \
    ln -sf /opt/hermes-apollo/venv/bin/hermes /usr/local/bin/hermes

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY scripts/install-apollo.sh /app/scripts/install-apollo.sh
COPY config/hermes-soul.md /app/config/hermes-soul.md
RUN bash /app/scripts/install-apollo.sh && \
    cp /app/config/hermes-soul.md /root/.hermes/SOUL.md

COPY . .

ENV PYTHONUNBUFFERED=1
CMD ["python3", "worker.py"]
