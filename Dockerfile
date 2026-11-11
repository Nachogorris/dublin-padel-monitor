FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8501

WORKDIR /app

# Install Linux packages required for Playwright / Chromium execution
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# Install Python requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright Chromium with system shared libraries
RUN playwright install --with-deps chromium

# Copy project source code
COPY . .

# Seed existing database so newly attached persistent volumes start with historical data
RUN mkdir -p /app/data_seed && cp data/dublin_padel.db /app/data_seed/dublin_padel.db 2>/dev/null || true

EXPOSE 8501

CMD ["python", "entrypoint.py"]

