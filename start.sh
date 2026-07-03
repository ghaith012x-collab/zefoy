#!/bin/bash
# Start Tor in the background, then launch the Flask app
tor -f /etc/tor/torrc &
sleep 3
exec python app.py
