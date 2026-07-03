FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    wget gnupg libgconf-2-4 libxss1 libnss3 libasound2 \
    libatk-bridge2.0-0 libgtk-3-0 libdrm2 libxcomposite1 \
    tesseract-ocr libtesseract-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install chromium

COPY . .

CMD ["gunicorn", "-w", "1", "-b", "0.0.0.0:5000", "app:app"]
