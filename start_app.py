"""Startup script: starts Tor in background and launches Flask."""
import subprocess, threading, time, os, signal

def monitor_tor_log():
    """Watch Tor's log file for bootstrap completion."""
    print("🧅 Waiting for Tor to bootstrap...", flush=True)
    for _ in range(120):  # up to 2 minutes
        try:
            with open("/tmp/tor.log") as f:
                content = f.read()
                if "Bootstrapped 100%" in content:
                    print("✅ Tor fully bootstrapped!", flush=True)
                    open("/tmp/tor_ready", "w").close()
                    return
                if "Permission denied" in content or "Could not bind" in content:
                    print(f"❌ Tor failed! Check /tmp/tor.log", flush=True)
                    return
        except FileNotFoundError:
            pass
        time.sleep(1)
    print("⚠️ Tor did NOT bootstrap within 2 minutes.", flush=True)

# Start Tor as a subprocess with output going to a FILE (not a pipe)
log_file = open("/tmp/tor.log", "w")
tor_proc = subprocess.Popen(
    ["tor", "-f", "/etc/tor/torrc"],
    stdout=log_file,
    stderr=log_file,
    preexec_fn=os.setpgrp  # detach from parent process group
)
print(f"🧅 Tor started (PID {tor_proc.pid})", flush=True)

# Monitor bootstrap in background thread
threading.Thread(target=monitor_tor_log, daemon=True).start()

# Start Flask app (blocking - keeps this process alive so Tor stays alive too)
print("🚀 Starting Flask app...", flush=True)
flask_proc = subprocess.run(["python", "app.py"])
