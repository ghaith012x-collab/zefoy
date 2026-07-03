"""Startup script: starts Tor in background and launches Flask immediately."""
import subprocess, threading, time, os

def run_tor():
    """Start Tor and monitor its bootstrap, logging to /tmp/tor.log."""
    print("🧅 Starting Tor...", flush=True)
    
    log_file = open("/tmp/tor.log", "w")
    
    tor_proc = subprocess.Popen(
        ["tor", "-f", "/etc/tor/torrc"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1
    )
    
    bootstrapped = False
    for line in tor_proc.stdout:
        line = line.strip()
        if line:
            print(f"[TOR] {line}", flush=True)
            log_file.write(line + "\n")
            log_file.flush()
        if "Bootstrapped 100%" in line:
            bootstrapped = True
            print("✅ Tor fully bootstrapped!", flush=True)
            open("/tmp/tor_ready", "w").close()
            break
        if "Permission denied" in line or "Could not bind" in line:
            print(f"❌ Tor failed: {line}", flush=True)
            break
    
    if not bootstrapped:
        # Check if Tor exited
        ret = tor_proc.poll()
        print(f"⚠️ Tor did NOT bootstrap. Exit code: {ret}", flush=True)
        log_file.write(f"FAILED: exit code {ret}\n")
        log_file.flush()
        # Do NOT create ready flag - Tor is broken
    
    # Keep reading remaining output
    try:
        for line in tor_proc.stdout:
            line = line.strip()
            if line:
                log_file.write(line + "\n")
                log_file.flush()
    except:
        pass
    
    log_file.close()

# Run Tor in background thread
threading.Thread(target=run_tor, daemon=True).start()

# Launch Flask immediately
os.execvp("python", ["python", "app.py"])
