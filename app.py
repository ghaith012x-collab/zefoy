# -*- coding: utf-8 -*-
from flask import Flask, render_template, request, jsonify, Response
from playwright.sync_api import sync_playwright
import threading, time, re, sys, difflib, json, base64, os, resource, gc
from PIL import Image, ImageOps
from io import BytesIO
from collections import Counter, deque
import numpy as np

def is_dead(e):
    """Return True if the exception means the browser/page is gone."""
    s = str(e).lower()
    t = type(e).__name__.lower()
    return ("target page" in s or "browser has been closed" in s or
            "target closed" in s or "crash" in s or "disposed" in s or
            "connection closed" in s or "browser disconnected" in s or
            "frame was detached" in s or "err_aborted" in s or
            "net::err" in s or "page crash" in s or
            "eagain" in s or "resource temporarily unavailable" in s or
            "failed to launch" in s or "spawn" in s or
            "targetclosed" in t)

_tab_prefix = threading.local()

# ------------------------------------------------------------------------
#  CONCURRENCY LIMITS
# ------------------------------------------------------------------------
CAPTCHA_CONCURRENCY = int(os.environ.get("CAPTCHA_CONCURRENCY", "2"))
_captcha_semaphore = threading.Semaphore(CAPTCHA_CONCURRENCY)
_ocr_semaphore = threading.Semaphore(CAPTCHA_CONCURRENCY)
MAX_GLOBAL_BROWSERS = int(os.environ.get("MAX_GLOBAL_BROWSERS", "9"))
_browser_semaphore = threading.Semaphore(MAX_GLOBAL_BROWSERS)
_active_browsers = 0
_active_browsers_lock = threading.Lock()

try:
    soft, hard = resource.getrlimit(resource.RLIMIT_NPROC)
    resource.setrlimit(resource.RLIMIT_NPROC, (hard, hard))
except:
    pass
try:
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (hard, hard))
except:
    pass

app = Flask(__name__)
ZEFOY = "https://zefoy.com"

def parse_proxy(raw):
    raw = raw.strip()
    if not raw:
        return ""
    if raw.startswith("http://") or raw.startswith("https://") or raw.startswith("socks"):
        return raw
    parts = raw.split(":")
    if len(parts) == 4:
        return f"http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
    if len(parts) == 2:
        return f"http://{parts[0]}:{parts[1]}"
    return raw

PROXY_URL = parse_proxy(os.environ.get("PROXY_URL", ""))
USE_TOR = os.environ.get("USE_TOR", "true").strip().lower() in ("true", "1", "yes")
if not PROXY_URL and USE_TOR:
    PROXY_URL = "socks5://127.0.0.1:9050"
    USING_TOR = True
else:
    USING_TOR = False

def renew_tor_circuit():
    import socket
    try:
        cookie_path = "/tmp/tor-data/control_auth_cookie"
        if not os.path.exists(cookie_path):
            return False
        with open(cookie_path, "rb") as f:
            cookie = f.read()
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(10)
        s.connect(("127.0.0.1", 9060))
        s.send(b"AUTHENTICATE " + cookie.hex().encode() + b"\r\n")
        resp = s.recv(256)
        if b"250" not in resp:
            s.close()
            return False
        s.send(b"SIGNAL NEWNYM\r\n")
        resp = s.recv(256)
        s.close()
        if b"250" in resp:
            time.sleep(5)
            return True
        else:
            return False
    except Exception:
        return False

# ------------------------------------------------------------------------
#  OVERLAY REMOVAL HELPER
# ------------------------------------------------------------------------
def remove_overlays(page):
    try:
        page.evaluate("""() => {
            document.querySelectorAll('iframe').forEach(el => el.remove());
            document.querySelectorAll('.fc-dialog-overlay, .fc-monetization-dialog-container, .fc-message-root, .fc-consent-root').forEach(el => el.remove());
            document.querySelectorAll('.adsbygoogle, .ad-container, iframe[src*="googleads"], iframe[src*="ads"], iframe.adsbygoogle').forEach(el => el.remove());
            document.querySelectorAll('[style*="position: fixed"], [style*="position: absolute"]').forEach(el => {
                if (el.style.zIndex && parseInt(el.style.zIndex) > 9000) {
                    if (el.querySelector('#captcha-img, input[name*="captcha"]') ||
                        el.closest('.wrapper-capth, .captcha-container, form')) return;
                    el.remove();
                }
            });
            document.querySelectorAll('button').forEach(btn => {
                if (btn.textContent.includes('Consent') && btn.offsetParent !== null) btn.click();
            });
        }""")
    except:
        pass

# ------------------------------------------------------------------------
#  ANTI-DETECTION SCRIPTS
# ------------------------------------------------------------------------
DISMISS_ALERTS_JS = "window.alert = function() { return true; }; window.confirm = function() { return true; };"
BLOCK_FC_POPUPS_JS = """(() => {
    const cleanPage = () => {
        document.querySelectorAll('iframe').forEach(el => el.remove());
        document.querySelectorAll('.fc-monetization-dialog-container, .fc-message-root, .fc-dialog-overlay, .fc-consent-root').forEach(el => el.remove());
        document.querySelectorAll('.adsbygoogle').forEach(el => el.remove());
        document.querySelectorAll('button').forEach(btn => {
            if (btn.textContent.includes('Consent') && btn.offsetParent !== null) btn.click();
        });
    };
    setTimeout(cleanPage, 800);
    const observer = new MutationObserver(cleanPage);
    if (document.body) observer.observe(document.body, { childList: true, subtree: true });
})();"""
MOUSE_SIMULATION_K9X_JS = """(() => {
    function generateK9xMouseData() {
        const points = [];
        const numPoints = Math.floor(Math.random() * 16) + 12;
        for (let i = 0; i < numPoints; i++) {
            const x = Math.floor(Math.random() * 1850) + 50;
            const y = Math.floor(Math.random() * 950) + 50;
            const d = (Math.random() * 2.75 + 0.05).toFixed(4);
            const g = Math.random() > 0.65 ? "True" : "False";
            points.push(`x=${x}&y=${y}&d=${d}&g=${g}`);
        }
        const raw = points.join("|");
        let xored = "";
        for (let i = 0; i < raw.length; i++) {
            xored += String.fromCharCode(raw.charCodeAt(i) ^ ((i % 5) + 77));
        }
        const wrapped = "K9x!" + xored + "K9x!";
        const encoded = btoa(wrapped);
        let reversed = encoded.split("").reverse().join("");
        while (reversed.length % 4 !== 0) reversed += "=";
        return reversed;
    }
    function injectMouseData() {
        const mouseData = generateK9xMouseData();
        document.querySelectorAll('input[type="hidden"]').forEach(input => {
            if (!input.value && input.name !== 'captcha_encoded') input.value = mouseData;
        });
        window.__zefoyMouseData = mouseData;
    }
    setTimeout(injectMouseData, 500);
    document.addEventListener('submit', function(e) { injectMouseData(); }, true);
})();"""
GENERATE_CF_OB_TE_JS = """(() => {
    function generateCfObTeCookie() {
        const source = "HTMLButtonElement.onclick@https://zefoy.com/:1:1";
        const kod = "DOMContentLoaded";
        const payload = `Kod: ${kod}\\nsource: ${source}`;
        const cookieValue = btoa(payload);
        const expiry = new Date(Date.now() + 5 * 60 * 60 * 1000).toUTCString();
        document.cookie = `cf_ob_te=${cookieValue}; Path=/; Expires=${expiry}`;
    }
    generateCfObTeCookie();
})();"""

def inject_anti_detection(page):
    try:
        for script in [DISMISS_ALERTS_JS, BLOCK_FC_POPUPS_JS, MOUSE_SIMULATION_K9X_JS, GENERATE_CF_OB_TE_JS]:
            page.evaluate(script)
    except:
        pass

HEARTS_BTN_SEL = "button.wbutton.btn-dark"

# ------------------------------------------------------------------------
#  SERVICES
# ------------------------------------------------------------------------
SERVICES = {
    "hearts": {"name": "Hearts", "emoji": "Hearts", "button_class": "t-hearts-button", "menu_class": "t-hearts-menu", "unit": "hearts"},
    "views": {"name": "Views", "emoji": "Views", "button_class": "t-views-button", "menu_class": "t-views-menu", "unit": "views"},
    "comment_hearts": {"name": "Comment Hearts", "emoji": "Comment Hearts", "button_class": "t-chearts-button", "menu_class": "t-chearts-menu", "unit": "hearts"},
    "shares": {"name": "Shares", "emoji": "Shares", "button_class": "t-shares-button", "menu_class": "t-shares-menu", "unit": "shares"},
    "favorites": {"name": "Favorites", "emoji": "Favorites", "button_class": "t-favorites-button", "menu_class": "t-favorites-menu", "unit": "favorites"},
    "followers": {"name": "Followers", "emoji": "Followers", "button_class": "t-followers-button", "menu_class": "t-followers-menu", "unit": "followers"},
}
ANY_SERVICE_BUTTON = ", ".join(f".{s['button_class']}" for s in SERVICES.values())

# ------------------------------------------------------------------------
#  DICTIONARY
# ------------------------------------------------------------------------
WORD_LIST = []
def load_dictionary():
    global WORD_LIST
    try:
        import urllib.request
        url = "https://raw.githubusercontent.com/dwyl/english-words/master/words_alpha.txt"
        data = urllib.request.urlopen(url, timeout=10).read().decode()
        WORD_LIST = [w.strip().lower() for w in data.splitlines() if 2 <= len(w.strip()) <= 10]
    except: pass
threading.Thread(target=load_dictionary, daemon=True).start()

# ------------------------------------------------------------------------
#  CAPTCHA SOLVER
# ------------------------------------------------------------------------
def remove_small_components(binary_arr, min_size=30):
    h, w = binary_arr.shape
    visited = np.zeros((h, w), dtype=bool)
    result = np.zeros((h, w), dtype=np.uint8)
    for y in range(h):
        for x in range(w):
            if binary_arr[y, x] == 1 and not visited[y, x]:
                component = []
                q = deque([(y, x)])
                visited[y, x] = True
                while q:
                    cy, cx = q.popleft()
                    component.append((cy, cx))
                    for dy, dx in [(-1,0),(1,0),(0,-1),(0,1),(-1,-1),(-1,1),(1,-1),(1,1)]:
                        ny, nx = cy + dy, cx + dx
                        if 0 <= ny < h and 0 <= nx < w and binary_arr[ny, nx] == 1 and not visited[ny, nx]:
                            visited[ny, nx] = True
                            q.append((ny, nx))
                if len(component) >= min_size:
                    for cy, cx in component:
                        result[cy, cx] = 1
    return result

def solve_captcha(img_bytes):
    with _ocr_semaphore:
        time.sleep(1)
        return _solve_captcha_inner(img_bytes)

def _solve_captcha_inner(img_bytes):
    import pytesseract
    img = Image.open(BytesIO(img_bytes))
    if img.mode != 'RGB': img = img.convert('RGB')
    gray = ImageOps.grayscale(img)
    w, h = gray.size
    big = gray.resize((w * 4, h * 4), Image.LANCZOS)
    arr = np.array(big)
    results = []
    def run_ocr(pil_img):
        found = []
        for psm in [7, 8]:
            config = f'--psm {psm} --oem 3 -c tessedit_char_whitelist=abcdefghijklmnopqrstuvwxyz'
            try:
                text = pytesseract.image_to_string(pil_img, config=config).strip()
                text = re.sub(r'[^a-z]', '', text.lower())
                if 3 <= len(text) <= 12: found.append(text)
            except: pass
        return found
    for thresh_val in [100, 150, 200]:
        binary_img = Image.fromarray(((arr >= thresh_val) * 255).astype('uint8'))
        results.extend(run_ocr(binary_img))
    if not results: return ""
    if WORD_LIST:
        word_set = set(WORD_LIST)
        exact = [r for r in results if r in word_set]
        if exact: return Counter(exact).most_common(1)[0][0]
    return Counter(results).most_common(1)[0][0]

def parse_wait_time(text):
    mins = re.search(r'(\d+)\s*minute', text)
    secs = re.search(r'(\d+)\s*second', text)
    total = 0
    if mins: total += int(mins.group(1)) * 60
    if secs: total += int(secs.group(1))
    return total

# ------------------------------------------------------------------------
#  SESSIONS
# ------------------------------------------------------------------------
class FrameBuffer:
    def __init__(self, max_frames=20):
        self.buffer = deque(maxlen=max_frames)
        self.lock = threading.Lock()
    def add_frame(self, frame_bytes):
        with self.lock: self.buffer.append(frame_bytes)
    def get_latest(self):
        with self.lock: return self.buffer[-1] if self.buffer else None

class Session:
    _counter = 0
    _lock = threading.Lock()
    def __init__(self, video_url, service="views", num_tabs=1, username=""):
        with Session._lock:
            Session._counter += 1
            self.id = Session._counter
        self.video_url = video_url
        self.service = service
        self.username = username
        self.num_tabs = max(1, min(num_tabs, 20))
        self.status = "starting"
        self.total_count = 0
        self.cycles = 0
        self.logs = []
        self.countdown = ""
        self.stop_event = threading.Event()
        self.count_lock = threading.Lock()
        self.active_tabs = 0
        self.video_buffers = {}
    def log(self, msg):
        self.logs.append(msg)
        print(f"[S{self.id}] {msg}", flush=True)
    def to_dict(self):
        svc = SERVICES.get(self.service, SERVICES["views"])
        return {
            "id": self.id, "url": self.video_url, "username": self.username,
            "service": self.service, "serviceName": svc["name"], "status": self.status,
            "count": self.total_count, "unit": svc["unit"], "cycles": self.cycles,
            "countdown": self.countdown, "numTabs": self.num_tabs, "activeTabs": self.active_tabs
        }

sessions = {}
sessions_lock = threading.Lock()

def capture_screenshot(page, quality=30):
    try:
        return page.screenshot(type='jpeg', quality=quality)
    except: return None

def generate_mjpeg_stream(frame_buffer):
    while True:
        frame_data = frame_buffer.get_latest()
        if frame_data:
            yield (b'--FRAME\\r\\nContent-Type: image/jpeg\\r\\nContent-Length: ' + str(len(frame_data)).encode() + b'\\r\\n\\r\\n' + frame_data + b'\\r\\n')
        time.sleep(1)

def run_session(session):
    session.status = "running"
    threads = []
    for tab_id in range(session.num_tabs):
        t = threading.Thread(target=run_tab, args=(session, tab_id), daemon=True)
        t.start()
        threads.append(t)
        time.sleep(5)
    for t in threads: t.join()
    session.status = "stopped"

def run_tab(session, tab_id):
    session.video_buffers[tab_id] = FrameBuffer()
    def z_sleep(seconds):
        end_t = time.time() + seconds
        while time.time() < end_t:
            if session.stop_event.is_set(): raise Exception("Stopped")
            time.sleep(0.5)

    with session.count_lock: session.active_tabs += 1
    try:
        while not session.stop_event.is_set():
            gc.collect()
            got_slot = False
            browser = None
            try:
                if not _browser_semaphore.acquire(timeout=1): continue
                got_slot = True
                time.sleep(2)
                with sync_playwright() as p:
                    launch_opts = {"headless": True, "args": ["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"]}
                    if USING_TOR:
                        tor_port = 9050 + (tab_id % 10)
                        launch_opts["proxy"] = {"server": f"socks5://127.0.0.1:{tor_port}"}
                    elif PROXY_URL:
                        launch_opts["proxy"] = {"server": PROXY_URL}
                    
                    browser = p.chromium.launch(**launch_opts)
                    page = browser.new_page(viewport={"width": 800, "height": 600})
                    page.goto(ZEFOY, timeout=60000)
                    z_sleep(5)
                    inject_anti_detection(page)

                    # --- Captcha Logic ---
                    captcha_img = page.locator("#captcha-img, img[src*='captcha']").first
                    if captcha_img.is_visible(timeout=5000):
                        _captcha_semaphore.acquire()
                        try:
                            for _ in range(10):
                                ans = solve_captcha(captcha_img.screenshot())
                                if not ans: break
                                page.locator("input[name='captchalogin']").fill(ans)
                                page.locator("button.submit-captcha").click()
                                z_sleep(5)
                                if not captcha_img.is_visible(timeout=2000): break
                        finally: _captcha_semaphore.release()

                    # --- Service Logic ---
                    svc = SERVICES.get(session.service)
                    btn = page.locator(f".{svc['button_class']}").first
                    btn.click()
                    z_sleep(2)

                    while not session.stop_event.is_set():
                        session.cycles += 1
                        url_input = page.locator("input[placeholder*='URL']").first
                        page.evaluate("(args) => { const el = document.querySelector(args.sel); if(el) { el.value = args.val; el.dispatchEvent(new Event('input', {bubbles:true})); } }", {"sel": "input[placeholder*='URL']", "val": session.video_url})
                        page.locator("button[type='submit']").first.click()
                        z_sleep(5)
                        
                        body = page.inner_text("body").lower()
                        if "successfully" in body:
                            with session.count_lock: session.total_count += 100
                            session.log("Success! +100")
                        
                        wait_secs = parse_wait_time(body)
                        if wait_secs > 0:
                            for r in range(wait_secs, 0, -1):
                                session.countdown = f"Wait: {r}s"
                                z_sleep(1)
                            session.countdown = ""
                            page.locator("button[type='submit']").first.click()
                            z_sleep(3)
                        
                        # Background screenshot for stream
                        frm = capture_screenshot(page)
                        if frm: session.video_buffers[tab_id].add_frame(frm)
                        z_sleep(10)

            except Exception as e:
                session.log(f"Error: {str(e)}")
                z_sleep(5)
            finally:
                if browser: browser.close()
                if got_slot: _browser_semaphore.release()
    finally:
        with session.count_lock: session.active_tabs -= 1

# ------------------------------------------------------------------------
#  ROUTES
# ------------------------------------------------------------------------
@app.route("/")
def index(): return render_template("index.html")

@app.route("/sessions")
def list_sessions():
    with sessions_lock: data = [s.to_dict() for s in sessions.values()]
    return jsonify({"sessions": data, "browsers": _active_browsers, "maxBrowsers": MAX_GLOBAL_BROWSERS})

@app.route("/start", methods=["POST"])
def start():
    data = request.get_json()
    session = Session(data.get("url"), service=data.get("service"), num_tabs=int(data.get("tabs", 1)), username=data.get("username"))
    with sessions_lock: sessions[session.id] = session
    threading.Thread(target=run_session, args=(session,), daemon=True).start()
    return jsonify(session.to_dict())

@app.route("/stop/<int:sid>", methods=["POST"])
def stop(sid):
    with sessions_lock: sess = sessions.get(sid)
    if sess: sess.stop_event.set()
    return jsonify({"ok": True})

@app.route("/remove/<int:sid>", methods=["POST"])
def remove(sid):
    with sessions_lock:
        if sid in sessions:
            sessions[sid].stop_event.set()
            del sessions[sid]
    return jsonify({"ok": True})

if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=8080)
