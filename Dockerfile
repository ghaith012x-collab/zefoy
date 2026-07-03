FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN apt-get update && \
    apt-get install -y --no-install-recommends tor && \
    rm -rf /var/lib/apt/lists/*

RUN playwright install --with-deps chromium

COPY torrc /etc/tor/torrc
COPY start.sh .
RUN chmod +x start.sh

COPY . .

EXPOSE 5000

CMD ["./start.sh"]
