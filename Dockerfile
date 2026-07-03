FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN playwright install --with-deps chromium

RUN apt-get update && \
    apt-get install -y --no-install-recommends tor && \
    rm -rf /var/lib/apt/lists/*

COPY torrc /etc/tor/torrc
COPY start.sh .
RUN chmod +x start.sh

COPY . .

EXPOSE 5000

CMD ["./start.sh"]
