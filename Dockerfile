FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Chromium deps + Tor in one layer
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxrandr2 libgbm1 libpango-1.0-0 \
    libcairo2 libasound2t64 libatspi2.0-0 libxshmfence1 \
    fonts-liberation fonts-noto-color-emoji \
    tor && \
    playwright install chromium && \
    rm -rf /var/lib/apt/lists/*

COPY torrc /etc/tor/torrc
COPY . .

EXPOSE 5000

CMD tor -f /etc/tor/torrc & sleep 3 && python app.py
