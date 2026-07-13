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
    """Strip ad iframes and consent dialogs that can intercept clicks (2026 DOM)."""
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
    else document.addEventListener('DOMContentLoaded', () => observer.observe(document.body, { childList: true, subtree: true }));
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
    setTimeout(injectMouseData, 1500);
    setTimeout(injectMouseData, 3000);
    document.addEventListener('submit', function(e) { injectMouseData(); }, true);
    document.addEventListener('click', function(e) {
        if (e.target.tagName === 'BUTTON' || e.target.closest('button')) setTimeout(injectMouseData, 50);
    }, true);
    const observer = new MutationObserver(function(mutations) {
        mutations.forEach(function(m) { if (m.addedNodes.length > 0) setTimeout(injectMouseData, 100); });
    });
    if (document.body) observer.observe(document.body, { childList: true, subtree: true });
    else document.addEventListener('DOMContentLoaded', function() {
        observer.observe(document.body, { childList: true, subtree: true }); injectMouseData();
    });
    window.generateK9xMouseData = generateK9xMouseData;
    window.injectMouseData = injectMouseData;
})();"""

GENERATE_CF_OB_TE_JS = """(() => {
    function generateCfObTeCookie() {
        const source = "HTMLButtonElement.onclick@https://zefoy.com/:1:1";
        const kod = "DOMContentLoaded";
        const payload = `Kod: ${kod}\\nsource: ${source}`;
        const cookieValue = btoa(payload);
        const expiry = new Date(Date.now() + 5 * 60 * 60 * 1000).toUTCString();
        document.cookie = `cf_ob_te=${cookieValue}; Path=/; Expires=${expiry}`;
        return cookieValue;
    }
    generateCfObTeCookie();
    setInterval(generateCfObTeCookie, 60000);
    window.generateCfObTeCookie = generateCfObTeCookie;
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
    "hearts": {
        "name": "Hearts",
        "emoji": "Hearts",
        "button_class": "t-hearts-button",
        "menu_class": "t-hearts-menu",
        "unit": "hearts",
        "engine": "zefoy",
    },
    "views": {
        "name": "Views",
        "emoji": "Views",
        "button_class": "t-views-button",
        "menu_class": "t-views-menu",
        "unit": "views",
        "engine": "zefoy",
    },
    "comment_hearts": {
        "name": "Comment Hearts",
        "emoji": "Comment Hearts",
        "button_class": "t-chearts-button",
        "menu_class": "t-chearts-menu",
        "unit": "hearts",
        "engine": "zefoy",
    },
    "shares": {
        "name": "Shares",
        "emoji": "Shares",
        "button_class": "t-shares-button",
        "menu_class": "t-shares-menu",
        "unit": "shares",
        "engine": "zefoy",
    },
    "favorites": {
        "name": "Favorites",
        "emoji": "Favorites",
        "button_class": "t-favorites-button",
        "menu_class": "t-favorites-menu",
        "unit": "favorites",
        "engine": "zefoy",
    },
    "followers": {
        "name": "Followers",
        "emoji": "Followers",
        "button_class": "t-followers-button",
        "menu_class": "t-followers-menu",
        "unit": "followers",
        "engine": "zefoy",
    },
}

ANY_SERVICE_BUTTON = ", ".join(f".{s['button_class']}" for s in SERVICES.values() if 'button_class' in s)

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
    except:
        pass

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
    from PIL import ImageFilter, ImageEnhance
    img = Image.open(BytesIO(img_bytes))
    if img.mode != 'RGB':
        img = img.convert('RGB')
    gray = ImageOps.grayscale(img)
    w, h = gray.size
    big = gray.resize((w * 4, h * 4), Image.LANCZOS)
    arr = np.array(big)
    results = []

    def run_ocr(pil_img):
        found = []
        for psm in [7, 8, 13, 6]:
            config = f'--psm {psm} --oem 3 -c tessedit_char_whitelist=abcdefghijklmnopqrstuvwxyz'
            try:
                text = pytesseract.image_to_string(pil_img, config=config).strip()
                text = re.sub(r'[^a-z]', '', text.lower())
                if 3 <= len(text) <= 12:
                    found.append(text)
            except:
                pass
        return found

    for thresh_val in [100, 120, 140, 160, 180, 200]:
        binary_img = Image.fromarray(((arr >= thresh_val) * 255).astype('uint8'))
        results.extend(run_ocr(binary_img))

    for thresh_val in [100, 130, 160]:
        binary_img = Image.fromarray(((arr < thresh_val) * 255).astype('uint8'))
        results.extend(run_ocr(binary_img))

    for thresh_val in [110, 130, 150, 170]:
        binary = (arr < thresh_val).astype(np.uint8)
        cleaned = remove_small_components(binary, min_size=25)
        clean_img = Image.fromarray(((1 - cleaned) * 255).astype('uint8'))
        results.extend(run_ocr(clean_img))

    print(f"[BOT] OCR candidates: {results}", flush=True)
    if not results:
        return ""

    if WORD_LIST:
        word_set = set(WORD_LIST)
        exact_matches = [r for r in results if r in word_set]
        if exact_matches:
            best = Counter(exact_matches).most_common(1)[0][0]
            return best
        best_match = None
        best_score = 0
        for candidate in set(results):
            freq = results.count(candidate)
            matches = difflib.get_close_matches(candidate, WORD_LIST, n=1, cutoff=0.6)
            if matches:
                sim = difflib.SequenceMatcher(None, candidate, matches[0]).ratio()
                score = freq * sim
                if score > best_score:
                    best_score = score
                    best_match = matches[0]
        if best_match:
            return best_match

    return Counter(results).most_common(1)[0][0]

def parse_wait_time(text):
    mins = re.search(r'(\d+)\s*minute', text)
    secs = re.search(r'(\d+)\s*second', text)
    total = 0
    if mins: total += int(mins.group(1)) * 60
    if secs: total += int(secs.group(1))
    return total

def resolve_comment_link(url):
    if not url:
        return None
    try:
        import urllib.request
        from urllib.parse import urlparse, parse_qs, unquote
        final_url = url
        try:
            req = urllib.request.Request(url)
            req.add_header('User-Agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
            response = urllib.request.urlopen(req, timeout=15)
            final_url = response.url
        except:
            final_url = url
        parsed = urlparse(final_url)
        params = parse_qs(parsed.query)
        comment_id = params.get('comment', [None])[0] or params.get('reply_comment_id', [None])[0]
        path_parts = parsed.path.strip('/').split('/')
        video_creator = path_parts[0].lstrip('@') if path_parts else None
        video_id = None
        if 'video' in path_parts:
            idx = path_parts.index('video')
            if idx + 1 < len(path_parts):
                video_id = path_parts[idx + 1]
        return {
            'final_url': final_url,
            'comment_id': comment_id,
            'video_creator': video_creator,
            'video_id': video_id,
        }
    except:
        return None

# ------------------------------------------------------------------------
#  LIVE VIDEO STREAMING
# ------------------------------------------------------------------------
class FrameBuffer:
    def __init__(self, max_frames=20):
        self.buffer = deque(maxlen=max_frames)
        self.lock = threading.Lock()
    def add_frame(self, frame_bytes):
        with self.lock:
            self.buffer.append(frame_bytes)
    def get_latest(self):
        with self.lock:
            return self.buffer[-1] if self.buffer else None

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

    @property
    def svc(self):
        return SERVICES.get(self.service, SERVICES["views"])

    def log(self, msg):
        pre = getattr(_tab_prefix, 'value', '')
        full = f"{pre}{msg}"
        self.logs.append(full)
        self.countdown = ""
        print(f"[S{self.id}] {full}", flush=True)

    def set_countdown(self, text):
        self.countdown = text

    def add_count(self, count):
        with self.count_lock:
            self.total_count += count
            return self.total_count

    def add_cycle(self):
        with self.count_lock:
            self.cycles += 1
            return self.cycles

    def to_dict(self):
        return {
            "id": self.id,
            "url": self.video_url,
            "username": self.username,
            "service": self.service,
            "serviceName": self.svc["name"],
            "status": self.status,
            "count": self.total_count,
            "unit": self.svc["unit"],
            "cycles": self.cycles,
            "countdown": self.countdown,
            "numTabs": self.num_tabs,
            "activeTabs": self.active_tabs,
        }

sessions = {}
sessions_lock = threading.Lock()

# ------------------------------------------------------------------------
#  BOT LOOP
# ------------------------------------------------------------------------
def capture_screenshot(page, quality=60, max_width=1280):
    try:
        screenshot_bytes = page.screenshot(type='jpeg', quality=quality)
        img = Image.open(BytesIO(screenshot_bytes))
        if img.width > max_width:
            ratio = max_width / img.width
            new_height = int(img.height * ratio)
            img = img.resize((max_width, new_height), Image.Resampling.LANCZOS)
            buf = BytesIO()
            img.save(buf, format='JPEG', quality=quality)
            return buf.getvalue()
        return screenshot_bytes
    except:
        return None

def generate_mjpeg_stream(frame_buffer, fps=1):
    while True:
        frame_data = frame_buffer.get_latest()
        if frame_data is None:
            time.sleep(0.5)
            continue
        yield (b'--FRAME\r\n'
               b'Content-Type: image/jpeg\r\n'
               b'Content-Length: ' + str(len(frame_data)).encode() + b'\r\n'
               b'\r\n' + frame_data + b'\r\n')
        time.sleep(1)

def run_session(session):
    session.status = "running"
    svc_name = session.svc["name"]
    nt = session.num_tabs
    session.log(f"Launching {nt} tabs ({svc_name} mode)...")
    threads = []
    for tab_id in range(nt):
        t = threading.Thread(target=run_tab, args=(session, tab_id), daemon=True)
        t.start()
        threads.append(t)
        time.sleep(5)
    for t in threads:
        t.join()
    session.status = "stopped"

def run_tab(session, tab_id):
    if tab_id not in session.video_buffers:
        session.video_buffers[tab_id] = FrameBuffer()

    def z_sleep(seconds):
        if session.stop_event.is_set():
            raise Exception("Session stopped")
        end_t = time.time() + seconds
        while time.time() < end_t:
            if session.stop_event.is_set():
                raise Exception("Session stopped")
            time.sleep(0.5)

    svc = session.svc
    svc_name = svc["name"]
    btn_cls = svc["button_class"]
    menu_cls = svc.get("menu_class", "")
    unit = svc["unit"]
    multi = session.num_tabs > 1
    _tab_prefix.value = f"[T{tab_id+1}] " if multi else ""

    with session.count_lock:
        session.active_tabs += 1
    try:
        while not session.stop_event.is_set():
            gc.collect()
            browser = None
            got_slot = False
            try:
                if not _browser_semaphore.acquire(timeout=1):
                    continue
                got_slot = True
                time.sleep(2) # CPU launch staggering
                with sync_playwright() as p:
                    launch_opts = {
                        "headless": True,
                        "args": ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
                    }
                    if USING_TOR:
                        tor_port = 9050 + (tab_id % 10)
                        launch_opts["proxy"] = {"server": f"socks5://127.0.0.1:{tor_port}"}
                    elif PROXY_URL:
                        launch_opts["proxy"] = {"server": PROXY_URL}

                    browser = p.chromium.launch(**launch_opts)
                    page = browser.new_page(viewport={"width": 800, "height": 600})
                    page.on("dialog", lambda d: d.accept())

                    def safe_check(pg):
                        try:
                            pg.title()
                            return True
                        except:
                            return False

                    session.log("Loading zefoy.com...")
                    page.goto(ZEFOY, timeout=60000)
                    z_sleep(5)
                    inject_anti_detection(page)

                    # --- Captcha solving ---
                    captcha_img = page.locator("#captcha-img, img[src*='captcha']").first
                    if captcha_img.is_visible(timeout=10000):
                        _captcha_semaphore.acquire()
                        try:
                            for _ in range(15):
                                session.log("Solving captcha...")
                                ans = solve_captcha(captcha_img.screenshot())
                                if not ans:
                                    page.reload()
                                    z_sleep(5)
                                    continue
                                page.locator("input[name='captchalogin']").fill(ans)
                                page.locator("button.submit-captcha").click()
                                z_sleep(5)
                                if not page.locator("#captcha-img").is_visible(timeout=3000):
                                    session.log("Captcha solved!")
                                    break
                        finally:
                            _captcha_semaphore.release()

                    # --- Open Service ---
                    page.locator(f".{btn_cls}").first.click()
                    z_sleep(2)

                    while not session.stop_event.is_set():
                        if not safe_check(page): break
                        cycle = session.add_cycle()
                        session.log(f"Cycle {cycle}")
                        
                        input_sel = 'input[placeholder*="URL"]'
                        url_input = page.locator(input_sel).first
                        url_input.wait_for(state="visible", timeout=10000)
                        
                        # 100% Accurate injection
                        page.evaluate("""(args) => {
                            const el = document.querySelector(args.sel);
                            if (el) {
                                el.value = args.val;
                                el.dispatchEvent(new Event('input', { bubbles: true }));
                                el.dispatchEvent(new Event('change', { bubbles: true }));
                            }
                        }""", {"sel": input_sel, "val": session.video_url})
                        
                        page.locator("button[type='submit']").first.click()
                        z_sleep(4)
                        
                        body = page.inner_text("body").lower()
                        if "successfully" in body:
                            session.add_count(100)
                            session.log(f"Success! +100 {unit}")
                        
                        wait_secs = parse_wait_time(body)
                        if wait_secs > 0:
                            session.log(f"Waiting {wait_secs}s...")
                            for r in range(wait_secs, 0, -1):
                                session.set_countdown(f"Wait: {r}s")
                                z_sleep(1)
                            session.set_countdown("")
                            page.locator("button[type='submit']").first.click()
                            z_sleep(3)
                        
                        # Stream frame
                        frm = capture_screenshot(page)
                        if frm: session.video_buffers[tab_id].add_frame(frm)
                        z_sleep(5)

            except Exception as e:
                if not is_dead(e): session.log(f"Error: {str(e)}")
            finally:
                if browser: browser.close()
                if got_slot: _browser_semaphore.release()
                z_sleep(5)
    finally:
        with session.count_lock:
            session.active_tabs -= 1

# ------------------------------------------------------------------------
#  ROUTES
# ------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/sessions")
def list_sessions():
    with sessions_lock:
        data = [s.to_dict() for s in sessions.values()]
    return jsonify({"sessions": data, "browsers": _active_browsers, "maxBrowsers": MAX_GLOBAL_BROWSERS})

@app.route("/start", methods=["POST"])
def start():
    data = request.get_json()
    session = Session(data.get("url"), service=data.get("service", "views"), num_tabs=int(data.get("tabs", 1)), username=data.get("username"))
    with sessions_lock:
        sessions[session.id] = session
    threading.Thread(target=run_session, args=(session,), daemon=True).start()
    return jsonify(session.to_dict())

@app.route("/stop/<int:sid>", methods=["POST"])
def stop(sid):
    with sessions_lock:
        session = sessions.get(sid)
    if session: session.stop_event.set()
    return jsonify({"ok": True})

@app.route("/remove/<int:sid>", methods=["POST"])
def remove_session(sid):
    with sessions_lock:
        if sid in sessions:
            sessions[sid].stop_event.set()
            del sessions[sid]
    return jsonify({"ok": True})

@app.route("/stream/video/<int:sid>/<int:tab_id>")
def stream_video(sid, tab_id):
    with sessions_lock:
        session = sessions.get(sid)
    if not session or tab_id not in session.video_buffers:
        return "Not available", 404
    frame = session.video_buffers[tab_id].get_latest()
    if not frame: return "No frame", 404
    return Response(frame, mimetype="image/jpeg")

if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=8080)
