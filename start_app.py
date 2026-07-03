"""Startup script: waits for Tor to be ready, then launches Flask app."""
import subprocess, socket, time, os, sys

# Start Tor in background
print("🧅 Starting Tor...", flush=True)
tor_proc = subprocess.Popen(["tor", "-f", "/etc/tor/torrc"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

# Wait for Tor to be ready (check SOCKS port 9050)
for i in range(60):
    try:
        s = socket.socket()
        s.settimeout(1)
        s.connect(("127.0.0.1", 9050))
        s.close()
        print(f"✅ Tor is ready! (took {i+1}s)", flush=True)
        break
    except:
        time.sleep(1)
else:
    print("⚠️ Tor took too long, starting app anyway...", flush=True)

# Launch the Flask app (replace this process)
os.execvp("python", ["python", "app.py"])
