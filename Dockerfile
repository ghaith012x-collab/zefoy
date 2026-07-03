FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install tesseract for captcha OCR + playwright browser
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr && rm -rf /var/lib/apt/lists/*
RUN playwright install --with-deps chromium

COPY . .

EXPOSE 8080
CMD ["gunicorn", "-w", "1", "--threads", "4", "--timeout", "600", "-b", "0.0.0.0:8080", "app:app"]
