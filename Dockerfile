FROM python:3.11-slim

WORKDIR /app

# Chromium system libraries (manual install avoids unavailable font packages on Debian trixie)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdbus-1-3 libxkbcommon0 \
    libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libpango-1.0-0 libcairo2 \
    fonts-liberation \
 && { apt-get install -y --no-install-recommends libasound2t64 2>/dev/null \
      || apt-get install -y --no-install-recommends libasound2 2>/dev/null \
      || true; } \
 && rm -rf /var/lib/apt/lists/*

# Install deps first (layer cache)
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && playwright install chromium

# Copy source
COPY backend/ ./backend/
COPY frontend/ ./frontend/

WORKDIR /app/backend

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
