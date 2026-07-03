"""Startup script: starts Tor in background and launches Flask."""
import subprocess, threading, time, os

def monitor_tor_log():
    """Watch Tor's log file for bootstrap completion."""
    print("🧅 Waiting for Tor to bootstrap...", flush=True)
    for _ in range(120):
        try:
            with open("/tmp/tor.log") as f:
                content = f.read()
                if "Bootstrapped 100%" in content:
                    print("✅ Tor fully bootstrapped!", flush=True)
                    open("/tmp/tor_ready", "w").close()
                    return
                if "Reading config failed" in content:
                    print(f"❌ Tor config failed! Log:\n{content}", flush=True)
                    return
        except FileNotFoundError:
            pass
        time.sleep(1)
    print("⚠️ Tor did NOT bootstrap within 2 minutes.", flush=True)

# Fix Tor data directory permissions (container runs as root, dir owned by debian-tor)
os.makedirs("/tmp/tor-data", exist_ok=True)
os.system("chown -R root:root /var/lib/tor 2>/dev/null || true")
os.system("chmod 700 /var/lib/tor 2>/dev/null || true")

# Write a fresh torrc at runtime to bypass any Docker cache issues
with open("/tmp/torrc", "w") as f:
    for i in range(10):
        f.write(f"SocksPort 9{50+i:03d} SessionGroup={i}\n")
    f.write("ControlPort 9060\n")
    f.write("CookieAuthentication 1\n")
    f.write("CookieAuthFile /tmp/tor-data/control_auth_cookie\n")
    f.write("DataDirectory /tmp/tor-data\n")
    f.write("RunAsDaemon 0\n")

print("📝 Generated /tmp/torrc", flush=True)

# Start Tor with the runtime-generated config
log_file = open("/tmp/tor.log", "w")
tor_proc = subprocess.Popen(
    ["tor", "-f", "/tmp/torrc"],
    stdout=log_file,
    stderr=log_file,
    preexec_fn=os.setpgrp
)
print(f"🧅 Tor started (PID {tor_proc.pid})", flush=True)

# Monitor bootstrap in background thread
threading.Thread(target=monitor_tor_log, daemon=True).start()

# Start Flask app (blocking)
print("🚀 Starting Flask app...", flush=True)
flask_proc = subprocess.run(["python", "app.py"])
