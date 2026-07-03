FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browser deps manually (Debian Trixie renamed some font packages)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    fonts-unifont \
    fonts-ubuntu \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxrandr2 libgbm1 libpango-1.0-0 \
    libcairo2 libasound2t64 libatspi2.0-0 libxshmfence1 && \
    playwright install chromium && \
    apt-get install -y --no-install-recommends tor && \
    rm -rf /var/lib/apt/lists/*

COPY torrc /etc/tor/torrc
COPY start.sh .
RUN chmod +x start.sh

COPY . .

EXPOSE 5000

CMD ["./start.sh"]
