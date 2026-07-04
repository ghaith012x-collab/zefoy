FROM python:3.11-slim-bookworm

WORKDIR /app

# Install system dependencies: Tor, Tesseract, and Chromium runtime libs
RUN apt-get update && apt-get install -y --no-install-recommends \
    tor \
    tesseract-ocr \
    # Chromium runtime dependencies
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libdbus-1-3 \
    libxkbcommon0 \
    libatspi2.0-0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    # Fonts
    fonts-liberation \
    fonts-noto-color-emoji \
    fonts-unifont \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Chromium browser only (system deps already installed above)
RUN playwright install chromium

COPY . .

EXPOSE 8080

CMD ["python", "app.py"]
