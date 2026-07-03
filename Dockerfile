FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install browser dependencies + Tor
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    wamerican \
    libnss3 \
    libnspr4 \
    libdbus-1-3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libatspi2.0-0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2t64 \
    fonts-unifont \
    fonts-liberation \
    fonts-noto-color-emoji \
    tor \
    && rm -rf /var/lib/apt/lists/*

# Install chromium browser only (no --with-deps)
RUN playwright install chromium

COPY . .

EXPOSE 8080
CMD bash -c "tor & sleep 3 && exec gunicorn -w 1 --threads 4 --timeout 600 -b 0.0.0.0:8080 app:app"
