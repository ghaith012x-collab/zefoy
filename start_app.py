"""Startup script: starts Tor in background and launches Flask immediately."""
import subprocess, threading, socket, time, os

def wait_for_tor():
    """Wait for Tor to bootstrap, runs in background thread."""
    for i in range(60):
        try:
            s = socket.socket()
            s.settimeout(1)
            s.connect(("127.0.0.1", 9050))
            s.close()
            print(f"✅ Tor is ready! (took {i+1}s)", flush=True)
            # Signal readiness by creating a flag file
            open("/tmp/tor_ready", "w").close()
            return
        except:
            time.sleep(1)
    print("⚠️ Tor may not be fully ready", flush=True)
    open("/tmp/tor_ready", "w").close()

# Start Tor in background
print("🧅 Starting Tor...", flush=True)
subprocess.Popen(["tor", "-f", "/etc/tor/torrc"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

# Wait for Tor in background thread
threading.Thread(target=wait_for_tor, daemon=True).start()

# Launch Flask immediately so Railway sees a healthy app
os.execvp("python", ["python", "app.py"])
