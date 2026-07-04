FROM python:3.11-slim

WORKDIR /app

# Install system dependencies: Tor and Tesseract OCR
RUN apt-get update && apt-get install -y --no-install-recommends \
    tor \
    tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright Chromium browser and its system dependencies
RUN playwright install --with-deps chromium

COPY . .

EXPOSE 8080

CMD ["python", "app.py"]
