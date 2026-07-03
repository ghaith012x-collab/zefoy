FROM python:3.11-slim

# Install Playwright + Tesseract dependencies for Debian Trixie
RUN apt-get update && apt-get install -y \
    wget gnupg ca-certificates \
    libxss1 libnss3 libasound2 \
    libatk-bridge2.0-0 libatk1.0-0 libcups2 libdbus-1-3 \
    libdrm2 libgtk-3-0 libgbm1 libxcomposite1 libxdamage1 \
    libxfixes3 libxkbcommon0 libxrandr2 libpango-1.0-0 \
    libcairo2 libfontconfig1 libfreetype6 libgcc-s1 \
    tesseract-ocr libtesseract-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install chromium

COPY . .

ENV PORT=8080
EXPOSE 8080

CMD ["gunicorn", "-w", "1", "--threads", "4", "--timeout", "600", "-b", "0.0.0.0:8080", "app:app"]
