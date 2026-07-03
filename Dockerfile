FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install ALL Chromium deps + Tor + Tesseract OCR in one layer
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxrandr2 libgbm1 libpango-1.0-0 \
    libcairo2 libasound2t64 libatspi2.0-0 libxshmfence1 libxfixes3 \
    libx11-6 libx11-xcb1 libxcb1 libxext6 libxi6 libxtst6 \
    libglib2.0-0 libdbus-1-3 libexpat1 libgcc-s1 libstdc++6 \
    fonts-liberation fonts-noto-color-emoji \
    tesseract-ocr tesseract-ocr-eng wamerican \
    ffmpeg \
    tor && \
    playwright install chromium && \
    rm -rf /var/lib/apt/lists/*

COPY torrc /etc/tor/torrc
COPY . .

EXPOSE 5000

CMD ["python", "start_app.py"]
