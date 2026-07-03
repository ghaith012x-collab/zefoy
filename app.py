from flask import Flask, render_template, request, jsonify, Response, make_response
from playwright.sync_api import sync_playwright
import threading, time, re, sys, difflib, json, base64, os
from PIL import Image, ImageOps
from io import BytesIO
from collections import Counter, deque
import numpy as np

# Thread-local tab prefix for log messages
_tab_prefix = threading.local()

# Global limit: max 3 Chromium browsers across ALL sessions at once
MAX_GLOBAL_BROWSERS = 3
_browser_semaphore = threading.Semaphore(MAX_GLOBAL_BROWSERS)
_active_browsers = 0
_active_browsers_lock = threading.Lock()

app = Flask(__name__)
ZEFOY = "https://zefoy.com"

# Proxy support: set PROXY_URL env var, or leave empty to auto-use Tor (built into container)
# Supports formats: http://user:pass@host:port  OR  host:port:user:pass  OR  host:port
# Set USE_TOR=false to disable Tor fallback
def _parse_proxy(raw):
    raw = raw.strip()
    if not raw:
        return ""
    if raw.startswith("http://") or raw.startswith("https://") or raw.startswith("socks"):
        return raw
    parts = raw.split(":")
    if len(parts) == 4:  # host:port:user:pass
        return f"http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
    if len(parts) == 2:  # host:port
        return f"http://{parts[0]}:{parts[1]}"
    return raw  # try as-is

PROXY_URL = _parse_proxy(os.environ.get("PROXY_URL", ""))
USE_TOR = os.environ.get("USE_TOR", "true").strip().lower() in ("true", "1", "yes")
if not PROXY_URL and USE_TOR:
    PROXY_URL = "socks5://127.0.0.1:9050"
    USING_TOR = True
else:
    USING_TOR = False

def _rotate_tor_circuit():
    """Send NEWNYM signal to Tor control port to get a new IP."""
    try:
        import socket
        cookie = b""
        try:
            with open("/tmp/tor-data/control_auth_cookie", "rb") as f:
                cookie = f.read()
        except:
            pass
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(5)
        s.connect(("127.0.0.1", 9060))
        resp = s.recv(256)
        cookie_hex = cookie.hex()
        auth_cmd = "AUTHENTICATE " + cookie_hex + "\r\n"
        s.send(auth_cmd.encode())
        resp = s.recv(256)
        if b"250" not in resp:
            s.close()
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(5)
            s.connect(("127.0.0.1", 9060))
            s.recv(256)
            s.send(b'AUTHENTICATE ""\r\n')
            resp = s.recv(256)
        s.send(b"SIGNAL NEWNYM\r\n")
        resp = s.recv(256)
        success = b"250" in resp
        s.close()
        if success:
            print("[TOR] Circuit rotated - new IP!", flush=True)
        return success
    except Exception as e:
        print(f"[TOR] Circuit rotation failed: {e}", flush=True)
        return False


# ═══════════════════════════════════════════════════════════════
#  SERVICES
# ═══════════════════════════════════════════════════════════════

SERVICES = {
    "views": {
        "name": "Views",
        "emoji": "👁️",
        "button_class": "t-views-button",
        "menu_class": "t-views-menu",
        "unit": "views",
    },
    "hearts": {
        "name": "Hearts",
        "emoji": "❤️",
        "button_class": "t-hearts-button",
        "menu_class": "t-hearts-menu",
        "unit": "hearts",
    },
    "shares": {
        "name": "Shares",
        "emoji": "🔄",
        "button_class": "t-shares-button",
        "menu_class": "t-shares-menu",
        "unit": "shares",
    },
    "favorites": {
        "name": "Favorites",
        "emoji": "⭐",
        "button_class": "t-favorites-button",
        "menu_class": "t-favorites-menu",
        "unit": "favorites",
    },
    "followers": {
        "name": "Followers",
        "emoji": "👥",
        "button_class": "t-followers-button",
        "menu_class": "t-followers-menu",
        "unit": "followers",
    },
    "qqtube_likes": {
        "name": "QQTube Likes",
        "emoji": "💜",
        "button_class": "",
        "menu_class": "",
        "unit": "likes",
        "engine": "qqtube",
    },
}

# CSS selector that matches ANY service button (used for captcha-solved check)
ANY_SERVICE_BUTTON = ", ".join(f".{s['button_class']}" for s in SERVICES.values())


# ═══════════════════════════════════════════════════════════════
#  DICTIONARY
# ═══════════════════════════════════════════════════════════════

WORD_LIST = []

def load_dictionary():
    global WORD_LIST
    try:
        with open('/usr/share/dict/words') as f:
            WORD_LIST = [w.strip().lower() for w in f if 2 <= len(w.strip()) <= 10]
        print(f"[BOT] Dictionary loaded: {len(WORD_LIST)} words", flush=True)
    except:
        try:
            import urllib.request
            url = "https://raw.githubusercontent.com/dwyl/english-words/master/words_alpha.txt"
            data = urllib.request.urlopen(url, timeout=10).read().decode()
            WORD_LIST = [w.strip().lower() for w in data.splitlines() if 2 <= len(w.strip()) <= 10]
            print(f"[BOT] Online dictionary loaded: {len(WORD_LIST)} words", flush=True)
        except Exception as e:
            print(f"[BOT] Dictionary load failed: {e}", flush=True)

threading.Thread(target=load_dictionary, daemon=True).start()


# ═══════════════════════════════════════════════════════════════
#  CAPTCHA SOLVER
# ═══════════════════════════════════════════════════════════════

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
    import pytesseract
    from PIL import ImageFilter, ImageEnhance
    img = Image.open(BytesIO(img_bytes))
    if img.mode != 'RGB':
        img = img.convert('RGB')
    gray = ImageOps.grayscale(img)
    w, h = gray.size
    # Upscale 4x for better OCR accuracy
    big = gray.resize((w * 4, h * 4), Image.LANCZOS)
    arr = np.array(big)

    results = []

    def run_ocr(pil_img, tag=""):
        """Run tesseract with multiple PSM modes and collect results."""
        found = []
        for psm in [7, 8, 13, 6]:
            config = f'--psm {psm} --oem 3 -c tessedit_char_whitelist=abcdefghijklmnopqrstuvwxyz'
            try:
                text = pytesseract.image_to_string(pil_img, config=config).strip()
                text = re.sub(r'[^a-z]', '', text.lower())
                if 3 <= len(text) <= 12:
                    found.append(text)
            except Exception as ocr_err:
                print(f"[BOT] OCR {tag} psm={psm} error: {ocr_err}", flush=True)
        return found

    # Strategy 1: Direct thresholds (dark text on light bg)
    for thresh_val in [100, 120, 140, 160, 180, 200]:
        binary_img = Image.fromarray(((arr >= thresh_val) * 255).astype('uint8'))
        results.extend(run_ocr(binary_img, f"thresh-{thresh_val}"))

    # Strategy 2: Inverted thresholds (light text on dark bg)
    for thresh_val in [100, 130, 160]:
        binary_img = Image.fromarray(((arr < thresh_val) * 255).astype('uint8'))
        results.extend(run_ocr(binary_img, f"inv-{thresh_val}"))

    # Strategy 3: Dot/noise removal + threshold
    for thresh_val in [110, 130, 150, 170]:
        binary = (arr < thresh_val).astype(np.uint8)
        cleaned = remove_small_components(binary, min_size=25)
        clean_img = Image.fromarray(((1 - cleaned) * 255).astype('uint8'))
        results.extend(run_ocr(clean_img, f"clean-{thresh_val}"))

    # Strategy 4: Contrast enhancement + threshold
    try:
        enhanced = ImageEnhance.Contrast(big).enhance(3.0)
        enhanced_arr = np.array(enhanced)
        for thresh_val in [120, 150, 180]:
            binary_img = Image.fromarray(((enhanced_arr >= thresh_val) * 255).astype('uint8'))
            results.extend(run_ocr(binary_img, f"contrast-{thresh_val}"))
    except:
        pass

    # Strategy 5: Median filter (removes salt-and-pepper noise) + threshold
    try:
        median = big.filter(ImageFilter.MedianFilter(size=3))
        median_arr = np.array(median)
        for thresh_val in [120, 150]:
            binary_img = Image.fromarray(((median_arr >= thresh_val) * 255).astype('uint8'))
            results.extend(run_ocr(binary_img, f"median-{thresh_val}"))
    except:
        pass

    # Strategy 6: Morphological closing (fills gaps in characters)
    try:
        for thresh_val in [130, 160]:
            binary = (arr < thresh_val).astype(np.uint8)
            cleaned = remove_small_components(binary, min_size=20)
            # Dilate then erode (close operation) to connect broken strokes
            from PIL import ImageFilter
            tmp_img = Image.fromarray((cleaned * 255).astype('uint8'))
            tmp_img = tmp_img.filter(ImageFilter.MaxFilter(3))  # dilate
            tmp_img = tmp_img.filter(ImageFilter.MinFilter(3))  # erode
            inv_img = ImageOps.invert(tmp_img)
            results.extend(run_ocr(inv_img, f"morph-{thresh_val}"))
    except:
        pass

    print(f"[BOT] OCR candidates: {results}", flush=True)
    if not results:
        return ""

    # Score candidates using dictionary matching
    if WORD_LIST:
        word_set = set(WORD_LIST)
        # First: check if any candidate is an exact dictionary word
        exact_matches = [r for r in results if r in word_set]
        if exact_matches:
            best = Counter(exact_matches).most_common(1)[0][0]
            print(f"[BOT] OCR exact match: '{best}' (count={Counter(exact_matches)[best]})", flush=True)
            return best

        # Second: fuzzy match each unique candidate and pick the best
        best_match = None
        best_score = 0
        best_raw = ""
        for candidate in set(results):
            freq = results.count(candidate)
            matches = difflib.get_close_matches(candidate, WORD_LIST, n=1, cutoff=0.6)
            if matches:
                # Score = frequency × similarity
                sim = difflib.SequenceMatcher(None, candidate, matches[0]).ratio()
                score = freq * sim
                if score > best_score:
                    best_score = score
                    best_match = matches[0]
                    best_raw = candidate
        if best_match:
            print(f"[BOT] OCR: '{best_raw}' → '{best_match}' (score={best_score:.2f})", flush=True)
            return best_match

    # Fallback: most common raw OCR result
    most_common = Counter(results).most_common(1)[0][0]
    print(f"[BOT] OCR fallback (no dict match): '{most_common}'", flush=True)
    return most_common


def parse_wait_time(text):
    mins = re.search(r'(\d+)\s*minute', text)
    secs = re.search(r'(\d+)\s*second', text)
    total = 0
    if mins: total += int(mins.group(1)) * 60
    if secs: total += int(secs.group(1))
    return total


# ═══════════════════════════════════════════════════════════════
#  SESSION MANAGEMENT
# ═══════════════════════════════════════════════════════════════

class Session:
    _counter = 0
    _lock = threading.Lock()

    def __init__(self, video_url, service="views", num_tabs=1):
        with Session._lock:
            Session._counter += 1
            self.id = Session._counter
        self.video_url = video_url
        self.service = service  # key into SERVICES dict
        self.num_tabs = max(1, min(num_tabs, 3))  # clamp 1-3
        self.status = "starting"
        self.total_count = 0
        self.cycles = 0
        self.logs = []       # List of log message strings
        self.countdown = ""  # Current countdown text (updates in-place on frontend)
        self.stop_event = threading.Event()
        self.thread = None
        self.count_lock = threading.Lock()
        self.active_tabs = 0

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
            "service": self.service,
            "serviceName": self.svc["name"],
            "serviceEmoji": self.svc["emoji"],
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


# ═══════════════════════════════════════════════════════════════
#  BOT LOOP
# ═══════════════════════════════════════════════════════════════

def run_session(session):
    """Orchestrates one or more tabs for this session."""
    session.status = "running"
    svc_name = session.svc["name"]
    nt = session.num_tabs
    engine = session.svc.get("engine", "zefoy")
    tab_func = run_qqtube_tab if engine == "qqtube" else run_tab

    if nt <= 1:
        session.log(f"🚀 Launching browser ({svc_name} mode)...")
        tab_func(session, 0)
    else:
        session.log(f"🚀 Launching {nt} tabs ({svc_name} mode)...")
        threads = []
        for tab_id in range(nt):
            t = threading.Thread(target=tab_func, args=(session, tab_id), daemon=True)
            t.start()
            threads.append(t)
            time.sleep(5)  # stagger launches to reduce memory spikes
        for t in threads:
            t.join()

    if session.status == "running":
        session.log("🛑 Session stopped.")
        session.status = "stopped"


def run_tab(session, tab_id):
    """Runs a single bot tab — each gets its own browser + Tor circuit.
    Wrapped in an outer retry loop so it NEVER permanently dies from crashes."""
    import gc
    svc = session.svc
    svc_name = svc["name"]
    btn_cls = svc["button_class"]
    menu_cls = svc["menu_class"]
    unit = svc["unit"]
    emoji = svc["emoji"]
    multi = session.num_tabs > 1

    # Set thread-local prefix for log messages
    _tab_prefix.value = f"[T{tab_id+1}] " if multi else ""

    MAX_FULL_RESTARTS = 100  # effectively infinite — keep retrying forever
    backoff = 5

    with session.count_lock:
        session.active_tabs += 1

    try:
        for full_restart in range(MAX_FULL_RESTARTS):
            if session.stop_event.is_set():
                return

            if full_restart > 0:
                wait_time = min(int(backoff), 30)
                session.log(f"\u267b\ufe0f Full restart #{full_restart} (waiting {wait_time}s)...")
                time.sleep(wait_time)
                backoff = min(backoff * 1.5, 30)
                gc.collect()
            else:
                if multi:
                    session.log(f"\U0001f680 Starting tab...")

            browser = None
            page = None

            # Acquire a global browser slot (blocks if all 3 are in use)
            got_slot = False
            try:
                if not _browser_semaphore.acquire(timeout=1):
                    session.log("⏳ Waiting for browser slot (max 3 globally)...")
                    _browser_semaphore.acquire()  # block until available
                got_slot = True
                with _active_browsers_lock:
                    global _active_browsers
                    _active_browsers += 1
                    session.log(f"🟢 Browser slot acquired ({_active_browsers}/{MAX_GLOBAL_BROWSERS} in use)")
            except Exception:
                pass  # if acquire fails, still try to launch

            try:
                with sync_playwright() as p:
                    launch_opts = {
                        "headless": True,
                        "args": [
                            "--no-sandbox",
                            "--disable-dev-shm-usage",
                            "--disable-gpu",
                            "--disable-extensions",
                            "--disable-background-networking",
                            "--disable-default-apps",
                            "--disable-sync",
                            "--disable-translate",
                            "--no-first-run",
                            "--disable-background-timer-throttling",
                            "--disable-renderer-backgrounding",
                            "--disable-backgrounding-occluded-windows",
                            "--disable-component-extensions-with-background-pages",
                            "--disable-features=TranslateUI",
                            "--renderer-process-limit=1",
                            "--js-flags=--max-old-space-size=128",
                            "--disable-software-rasterizer",
                            "--disable-logging",
                            "--disable-hang-monitor",
                            "--single-process",
                            "--disable-ipc-flooding-protection",
                            "--memory-pressure-off",
                        ],
                    }
                    if USING_TOR:
                        tor_port = 9050 + (tab_id % 10)
                        if full_restart == 0:
                            session.log(f"\U0001f9c5 Routing through Tor (port {tor_port})...")
                        import os
                        for _tw in range(60):
                            if os.path.exists("/tmp/tor_ready"):
                                break
                            if _tw == 0:
                                session.log("\u23f3 Waiting for Tor to bootstrap...")
                            time.sleep(1)
                        launch_opts["proxy"] = {
                            "server": f"socks5://127.0.0.1:{tor_port}",
                        }
                    elif PROXY_URL:
                        if full_restart == 0:
                            session.log(f"\U0001f310 Using proxy: {PROXY_URL.split('@')[-1] if '@' in PROXY_URL else PROXY_URL}")
                        launch_opts["proxy"] = {"server": PROXY_URL}

                    browser = p.chromium.launch(**launch_opts)
                    page = browser.new_page(viewport={"width": 800, "height": 600})
                    page.on("dialog", lambda d: d.accept())

                    def _safe_check(pg):
                        """Check if page is alive. Returns True if OK, False if crashed."""
                        try:
                            pg.title()
                            return True
                        except:
                            return False

                    # \u2500\u2500 Load zefoy \u2500\u2500
                    session.log("\U0001f310 Loading zefoy.com...")
                    page.goto(ZEFOY, wait_until="domcontentloaded", timeout=60000)
                    time.sleep(5)

                    if not _safe_check(page):
                        session.log("\U0001f4a5 Page crashed on load, restarting...")
                        continue

                    # \u2500\u2500 Check page / Solve captcha \u2500\u2500
                    session.log("\U0001f510 Checking for captcha...")

                    captcha_detected = False
                    page_ready = False

                    for page_attempt in range(10):
                        if session.stop_event.is_set():
                            return

                        if not _safe_check(page):
                            session.log("\U0001f4a5 Crashed during page check, restarting...")
                            break

                        try:
                            page_title = page.title().lower()
                            page_text = page.inner_text("body")[:200].lower()
                            if "502" in page_title or "502 bad gateway" in page_text:
                                session.log(f"\U0001f534 Zefoy is down (502 error), retrying ({page_attempt + 1}/10)...")
                                time.sleep(10 + page_attempt * 3)
                                page.reload(wait_until="domcontentloaded")
                                time.sleep(5)
                                continue
                            if "503" in page_title or "cloudflare" in page_text or "just a moment" in page_text:
                                session.log(f"\U0001f534 Zefoy loading/Cloudflare check ({page_attempt + 1}/10)...")
                                time.sleep(10 + page_attempt * 3)
                                page.reload(wait_until="domcontentloaded")
                                time.sleep(5)
                                continue
                        except:
                            pass

                        try:
                            page.locator("#captcha-img, .wrapper-capth, #captchatoken, img[src*=\"captcha\"], img[src*=\"CAPTCHA\"]").first.wait_for(state="visible", timeout=30000)
                            captcha_detected = True
                            break
                        except:
                            pass

                        try:
                            page.locator(ANY_SERVICE_BUTTON).first.wait_for(timeout=20000)
                            session.log("\u2705 No captcha needed \u2014 service buttons already visible")
                            page_ready = True
                            break
                        except:
                            pass

                        session.log(f"\u26a0\ufe0f Page not ready, reloading (attempt {page_attempt + 1}/10)...")
                        page.reload(wait_until="domcontentloaded")
                        time.sleep(10 + page_attempt * 3)
                    else:
                        session.log("\u26a0\ufe0f Page never became ready, restarting...")
                        continue

                    if not captcha_detected and not page_ready:
                        continue

                    if captcha_detected:
                        session.log("\U0001f510 Captcha detected, solving...")
                        captcha_solved = False
                        for captcha_attempt in range(20):
                            if session.stop_event.is_set():
                                return

                            if not _safe_check(page):
                                session.log("\U0001f4a5 Crashed during captcha, restarting...")
                                break

                            try:
                                captcha_img = page.locator("#captcha-img, img[src*='CAPTCHA'], img[src*='captcha']")
                                try:
                                    captcha_img.first.wait_for(state="visible", timeout=10000)
                                except:
                                    session.log("\u26a0\ufe0f Captcha image not loading, reloading page...")
                                    page.reload(wait_until="domcontentloaded")
                                    time.sleep(5)
                                    continue

                                session.log(f"\U0001f510 Solving captcha (attempt {captcha_attempt + 1})...")
                                time.sleep(2)
                                captcha_bytes = captcha_img.first.screenshot()
                                answer = solve_captcha(captcha_bytes)

                                if not answer:
                                    session.log("\u26a0\ufe0f OCR failed, refreshing captcha...")
                                    try: page.locator(".refresh-capthca-btn-new, [onclick*='refresh'], .captcha-refresh").first.click()
                                    except: page.reload(wait_until="domcontentloaded")
                                    time.sleep(3)
                                    continue

                                session.log(f"\U0001f524 Answer: '{answer}'")
                                captcha_input = page.locator("#captchatoken, input[name='captcha_secure'], input[placeholder*='aptcha']")
                                captcha_input.first.fill(answer)
                                time.sleep(0.5)
                                page.locator("button.submit-captcha, form .btn-primary[type='submit']").first.click()
                                time.sleep(5)

                                try:
                                    page.locator(ANY_SERVICE_BUTTON).first.wait_for(timeout=8000)
                                    session.log("\u2705 Captcha solved!")
                                    captcha_solved = True
                                    break
                                except:
                                    session.log(f"\u274c Wrong answer '{answer}', retrying...")
                                    try: page.locator(".modal .btn-secondary, .modal .close, .swal2-confirm, [class*='close']").first.click()
                                    except: pass
                                    time.sleep(1)
                                    try: page.locator(".refresh-capthca-btn-new, [onclick*='refresh'], .captcha-refresh").first.click()
                                    except: pass
                                    time.sleep(3)
                            except Exception as e:
                                err_str = str(e).lower()
                                if "crash" in err_str or "target closed" in err_str:
                                    session.log(f"\U0001f4a5 Crashed during captcha, restarting...")
                                    break
                                else:
                                    session.log(f"\u26a0\ufe0f Captcha error: {e}")
                                time.sleep(2)

                        if not captcha_solved:
                            continue

                    # \u2500\u2500 Click service button \u2500\u2500
                    session.log(f"{emoji} Looking for {svc_name} button...")
                    try:
                        page.locator(f".{btn_cls}").wait_for(timeout=30000)
                    except:
                        try:
                            btn_el = page.locator(f".{btn_cls}")
                            if btn_el.count() > 0 and btn_el.get_attribute("disabled"):
                                session.log(f"\u274c {svc_name} is currently unavailable on Zefoy. Try a different service.")
                            else:
                                session.log(f"\u274c {svc_name} button not found. Restarting...")
                        except:
                            session.log(f"\u274c {svc_name} button not found. Restarting...")
                        continue

                    page.locator(f".{btn_cls}").click()
                    time.sleep(2)
                    session.log(f"\u2705 {svc_name} panel opened!")

                    backoff = 5

                    # \u2500\u2500 Main loop \u2500\u2500
                    zero_streak = 0
                    no_response_streak = 0
                    MAX_NO_RESPONSE = 5
                    MAX_ZERO_STREAK = 10
                    while not session.stop_event.is_set():
                        if not _safe_check(page):
                            session.log("\U0001f4a5 Page crashed in main loop, restarting...")
                            break

                        cycle = session.add_cycle()
                        session.log(f"\U0001f504 Cycle {cycle}")

                        try:
                            url_input = page.locator(f".{menu_cls} input[type='text'], .{menu_cls} input[placeholder]").first
                            url_input.fill("")
                            time.sleep(0.3)
                            url_input.fill(session.video_url)
                            time.sleep(1)

                            page.locator(f".{menu_cls} button[type='submit']").first.click()
                            time.sleep(3)
                        except Exception as fill_err:
                            err_str = str(fill_err).lower()
                            if "crash" in err_str or "target closed" in err_str or "disposed" in err_str:
                                session.log("\U0001f4a5 Crashed filling URL, restarting...")
                                break
                            session.log(f"\u26a0\ufe0f Error filling URL: {fill_err}")
                            time.sleep(3)
                            continue

                        crashed_in_check = False
                        for check_round in range(120):
                            if session.stop_event.is_set():
                                break

                            try:
                                page_state = page.evaluate("""(menuClass) => {
                                    const body = document.body.innerText || '';
                                    const lower = body.toLowerCase();

                                    const countdown = document.getElementById('login-countdown');
                                    if (countdown && countdown.offsetParent !== null) {
                                        const text = countdown.innerText || '';
                                        if (text && (text.toLowerCase().includes('wait') ||
                                            text.toLowerCase().includes('minute') ||
                                            text.toLowerCase().includes('second'))) {
                                            return {type: 'ratelimit', text: text};
                                        }
                                    }

                                    if (lower.includes('successfully')) {
                                        let count = 0;
                                        const lines = body.split('\\n');
                                        let successLine = '';
                                        for (const line of lines) {
                                            if (line.toLowerCase().includes('successfully')) {
                                                successLine = line;
                                                break;
                                            }
                                        }
                                        // Log the raw success line for debugging
                                        console.log('ZEFOY_SUCCESS_RAW: ' + successLine);
                                        if (successLine) {
                                            const lineNums = successLine.match(/\\d+/g);
                                            if (lineNums) {
                                                // Filter out year-like numbers (2020-2035), month/day (1-31 only if line has date-like pattern)
                                                const hasDate = /\\d{1,2}[\\/-]\\d{1,2}[\\/-]\\d{2,4}|\\d{4}[\\/-]\\d{1,2}|[A-Za-z]+\\s+\\d{1,2},?\\s+\\d{4}/.test(successLine);
                                                const filtered = lineNums.map(Number).filter(n => {
                                                    if (n >= 2020 && n <= 2035) return false; // year
                                                    if (n > 100000) return false; // unreasonably large
                                                    return true;
                                                });
                                                if (filtered.length > 0) {
                                                    count = Math.max(...filtered);
                                                }
                                            }
                                        }
                                        return {type: 'success', count: count, rawLine: successLine};
                                    }

                                    const spinners = document.querySelectorAll('.fa-spinner, .fa-spin, .spinner, [class*="loading"], [class*="spin"]');
                                    for (const s of spinners) {
                                        if (s.offsetParent !== null) return {type: 'loading'};
                                    }

                                    const menu = document.querySelector('.' + menuClass);
                                    if (menu) {
                                        const forms = menu.querySelectorAll('form');
                                        for (const form of forms) {
                                            const action = form.getAttribute('action');
                                            if (action) {
                                                const container = document.getElementById(action);
                                                if (container && container.offsetParent !== null) {
                                                    const btn = container.querySelector('a, button, [onclick]');
                                                    if (btn && btn.offsetParent !== null) {
                                                        const r = btn.getBoundingClientRect();
                                                        if (r.width > 0 && r.height > 0) {
                                                            const sel = container.querySelector('select');
                                                            const selOpts = sel ? Array.from(sel.options).filter(o => o.value).map(o => o.value) : [];
                                                            return {type: 'bar', x: r.x + r.width/2, y: r.y + r.height/2, hasSelect: !!sel, selectOptions: selOpts};
                                                        }
                                                    }
                                                    const divs = container.querySelectorAll('div, span');
                                                    for (const d of divs) {
                                                        const t = d.innerText?.trim();
                                                        if (t && /\\d/.test(t) && t.length < 60 &&
                                                            !t.includes('wait') && !t.includes('minute') &&
                                                            !t.includes('second') && !t.includes('Please')) {
                                                            const r = d.getBoundingClientRect();
                                                            if (r.width > 50 && r.height > 10)
                                                                return {type: 'bar', x: r.x + r.width/2, y: r.y + r.height/2};
                                                        }
                                                    }
                                                }
                                            }
                                        }
                                    }

                                    if (lower.includes('please wait') && (lower.includes('minute') || lower.includes('second'))) {
                                        return {type: 'ratelimit', text: body.substring(0, 500)};
                                    }

                                    return {type: 'waiting'};
                                }""", menu_cls)
                            except Exception as eval_err:
                                err_str = str(eval_err).lower()
                                if "crash" in err_str or "target closed" in err_str or "disposed" in err_str:
                                    session.log("\U0001f4a5 Crashed during page check, restarting...")
                                    crashed_in_check = True
                                    break
                                time.sleep(1)
                                continue

                            state_type = page_state.get('type', 'waiting') if page_state else 'waiting'

                            if state_type == 'ratelimit':
                                no_response_streak = 0
                                timer_text = page_state.get('text', '')
                                wait_secs = parse_wait_time(timer_text)
                                if wait_secs <= 0:
                                    wait_secs = 60
                                wait_secs += 5
                                session.log(f"\u23f3 Rate limited ({wait_secs}s)")

                                for remaining in range(wait_secs, 0, -1):
                                    if session.stop_event.is_set():
                                        break
                                    mins = remaining // 60
                                    secs = remaining % 60
                                    time_str = f"{mins}m {secs:02d}s" if mins > 0 else f"{secs}s"
                                    session.set_countdown(f"\u23f3 {time_str} remaining")
                                    time.sleep(1)

                                session.set_countdown("")
                                session.log("\u2705 Rate limit done, retrying...")

                                try:
                                    time.sleep(1)
                                    url_input.fill("")
                                    time.sleep(0.3)
                                    url_input.fill(session.video_url)
                                    time.sleep(1)
                                    page.locator(f".{menu_cls} button[type='submit']").first.click()
                                    time.sleep(3)
                                except Exception as refill_err:
                                    err_str = str(refill_err).lower()
                                    if "crash" in err_str or "target closed" in err_str or "disposed" in err_str:
                                        crashed_in_check = True
                                        break
                                continue

                            elif state_type == 'success':
                                raw_line = page_state.get('rawLine', '')
                                count = page_state.get('count', 0)
                                if raw_line:
                                    session.log(f"📝 Zefoy raw: {raw_line[:120]}")
                                new_total = session.add_count(count)
                                if count > 0:
                                    zero_streak = 0
                                    no_response_streak = 0
                                    session.log(f"\U0001f389 +{count} {unit}! Total: {new_total:,}")
                                else:
                                    zero_streak += 1
                                    no_response_streak = 0
                                    if zero_streak >= MAX_ZERO_STREAK:
                                        session.log(f"\u26a0\ufe0f {zero_streak} consecutive 0 {unit} \u2014 resetting (not stopping)...")
                                        zero_streak = 0
                                    else:
                                        session.log(f"\u26a0\ufe0f Zefoy returned 0 {unit} (streak: {zero_streak}/{MAX_ZERO_STREAK}) \u2014 retrying...")
                                break

                            elif state_type == 'bar':
                                if page_state.get('hasSelect') and page_state.get('selectOptions'):
                                    try:
                                        best = page_state['selectOptions'][-1]
                                        page.locator("select#selectlimit, select[name='select_lmt'], select.form-select").first.select_option(best)
                                        session.log(f"\U0001f4ca Selected limit: {best}")
                                        time.sleep(0.5)
                                    except Exception as sel_err:
                                        session.log(f"\u26a0\ufe0f Could not set limit dropdown: {sel_err}")

                                x, y = page_state['x'], page_state['y']
                                session.log(f"{emoji} Sending {unit}...")
                                try:
                                    page.mouse.click(x, y)
                                except Exception as click_err:
                                    err_str = str(click_err).lower()
                                    if "crash" in err_str or "target closed" in err_str:
                                        crashed_in_check = True
                                        break
                                time.sleep(2)

                                count = 0
                                for _ in range(30):
                                    try:
                                        body = page.inner_text("body")
                                        if "successfully" in body.lower():
                                            for line in body.split('\n'):
                                                if 'successfully' in line.lower():
                                                    line_nums = [int(n) for n in re.findall(r'\d+', line) if 2020 <= int(n) <= 2035 is False and int(n) < 100000]
                                                    line_nums = [n for n in [int(x) for x in re.findall(r'\d+', line)] if not (2020 <= n <= 2035) and n < 100000]
                                                    if line_nums:
                                                        count = max(line_nums)
                                                break
                                            if count == 0:
                                                all_nums = [int(n) for n in re.findall(r'\d+', body) if not (2020 <= int(n) <= 2035) and int(n) < 100000]
                                                if all_nums:
                                                    count = max(all_nums)
                                            new_total = session.add_count(count)
                                            break
                                    except:
                                        pass
                                    time.sleep(1)

                                if count > 0:
                                    zero_streak = 0
                                    no_response_streak = 0
                                    session.log(f"\U0001f389 +{count} {unit}! Total: {new_total:,}")
                                else:
                                    zero_streak += 1
                                    no_response_streak = 0
                                    if zero_streak >= MAX_ZERO_STREAK:
                                        session.log(f"\u26a0\ufe0f {zero_streak} consecutive 0 {unit} \u2014 resetting (not stopping)...")
                                        zero_streak = 0
                                    else:
                                        session.log(f"\u26a0\ufe0f Zefoy returned 0 {unit} (streak: {zero_streak}/{MAX_ZERO_STREAK}) \u2014 retrying...")
                                break

                            elif state_type == 'loading':
                                time.sleep(1)
                                continue

                            else:
                                if check_round < 30:
                                    time.sleep(1)
                                    continue
                                else:
                                    no_response_streak += 1
                                    if no_response_streak >= MAX_NO_RESPONSE:
                                        session.log(f"\U0001f534 {no_response_streak} consecutive no-responses \u2014 reloading page...")
                                        no_response_streak = 0
                                        try:
                                            page.reload(wait_until="domcontentloaded")
                                            time.sleep(5)
                                            try:
                                                page.locator(f".{btn_cls}").wait_for(timeout=10000)
                                                page.locator(f".{btn_cls}").click()
                                                time.sleep(2)
                                                session.log(f"\u2705 {svc_name} panel re-opened after reload")
                                            except:
                                                session.log(f"\u26a0\ufe0f {svc_name} button not found after reload, restarting...")
                                                crashed_in_check = True
                                                break
                                        except Exception as reload_err:
                                            err_str = str(reload_err).lower()
                                            if "crash" in err_str or "target closed" in err_str:
                                                crashed_in_check = True
                                                break
                                            session.log(f"\u26a0\ufe0f Reload error: {reload_err}")
                                    else:
                                        session.log(f"\u26a0\ufe0f No response, retrying... ({no_response_streak}/{MAX_NO_RESPONSE})")
                                    break

                        if crashed_in_check:
                            session.log("\U0001f4a5 Crashed in main loop, restarting tab...")
                            break

                        time.sleep(3)
                        if cycle % 10 == 0:
                            gc.collect()

            except Exception as inner_err:
                err_str = str(inner_err).lower()
                if "crash" in err_str or "target closed" in err_str or "disposed" in err_str:
                    session.log(f"\U0001f4a5 Browser crashed, restarting tab...")
                else:
                    session.log(f"\u26a0\ufe0f Error: {inner_err} \u2014 restarting tab...")
                import traceback
                traceback.print_exc()
            finally:
                try:
                    if browser:
                        browser.close()
                except:
                    pass
                # Release global browser slot
                if got_slot:
                    with _active_browsers_lock:
                        _active_browsers = max(0, _active_browsers - 1)
                    _browser_semaphore.release()
                    got_slot = False
                gc.collect()

        session.log("\U0001f6d1 Tab exhausted all restart attempts.")

    except Exception as e:
        session.log(f"\u274c Fatal error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        with session.count_lock:
            session.active_tabs = max(0, session.active_tabs - 1)
            if session.active_tabs <= 0 and session.status == "running":
                session.status = "error"


def run_qqtube_tab(session, tab_id):
    """Runs a single QQTube bot tab — submits free likes, rotates IP, repeats forever."""
    import gc
    multi = session.num_tabs > 1
    _tab_prefix.value = f"[T{tab_id+1}] " if multi else ""
    
    QQTUBE_URL = "https://www.qqtube.com/free-tiktok-likes"
    MAX_FULL_RESTARTS = 1000  # effectively infinite
    
    with session.count_lock:
        session.active_tabs += 1
    
    try:
        submission_count = 0
        consecutive_cooldowns = 0
        tor_port_offset = 0  # cycle through different SOCKS ports too
        
        for attempt in range(MAX_FULL_RESTARTS):
            if session.stop_event.is_set():
                return
            
            browser = None
            got_slot = False
            
            try:
                # Acquire global browser slot
                if not _browser_semaphore.acquire(timeout=1):
                    session.log("⏳ Waiting for browser slot (max 3 globally)...")
                    _browser_semaphore.acquire()
                got_slot = True
                with _active_browsers_lock:
                    global _active_browsers
                    _active_browsers += 1
                
                # Rotate Tor circuit before each submission (except first)
                if USING_TOR and submission_count > 0:
                    session.log("🔄 Rotating Tor circuit for new IP...")
                    _rotate_tor_circuit()
                    time.sleep(3)  # Wait for new circuit to establish
                
                # Use different SOCKS port each time for extra IP diversity
                tor_port = 9050 + ((tab_id + tor_port_offset) % 10)
                tor_port_offset += 1
                
                with sync_playwright() as p:
                    launch_opts = {
                        "headless": True,
                        "args": [
                            "--no-sandbox",
                            "--disable-dev-shm-usage",
                            "--disable-gpu",
                            "--disable-extensions",
                            "--disable-background-networking",
                            "--disable-default-apps",
                            "--disable-sync",
                            "--disable-translate",
                            "--no-first-run",
                            "--renderer-process-limit=1",
                            "--js-flags=--max-old-space-size=128",
                            "--disable-software-rasterizer",
                            "--single-process",
                            "--memory-pressure-off",
                        ],
                    }
                    if USING_TOR:
                        launch_opts["proxy"] = {"server": f"socks5://127.0.0.1:{tor_port}"}
                        if submission_count == 0:
                            session.log(f"🧅 Routing through Tor (port {tor_port})...")
                    elif PROXY_URL:
                        launch_opts["proxy"] = {"server": PROXY_URL}
                    
                    browser = p.chromium.launch(**launch_opts)
                    page = browser.new_page(viewport={"width": 1280, "height": 720})
                    
                    # Navigate to QQTube
                    session.log(f"🌐 Loading QQTube (submission #{submission_count + 1})...")
                    page.goto(QQTUBE_URL, wait_until="domcontentloaded", timeout=60000)
                    time.sleep(4)
                    
                    # Check for cooldown
                    page_text = page.inner_text("body")
                    if "Come back in" in page_text or "already used" in page_text:
                        # Extract cooldown time
                        import re as _re
                        cd_match = _re.search(r'Come back in (\d+h \d+m|\d+m)', page_text)
                        cd_text = cd_match.group(1) if cd_match else "unknown"
                        consecutive_cooldowns += 1
                        session.log(f"⏰ IP on cooldown ({cd_text}), rotating... ({consecutive_cooldowns} IPs tried)")
                        
                        if consecutive_cooldowns >= 20:
                            session.log("⚠️ 20 consecutive IPs on cooldown, waiting 5 min before retrying...")
                            for wait_i in range(300):
                                if session.stop_event.is_set():
                                    return
                                if wait_i % 60 == 0:
                                    session.set_countdown(f"⏳ Cooldown break: {(300 - wait_i) // 60}m {(300 - wait_i) % 60}s")
                                time.sleep(1)
                            session.set_countdown("")
                            consecutive_cooldowns = 0
                        continue
                    
                    consecutive_cooldowns = 0  # Reset on non-cooldown
                    
                    # Find and fill the URL input
                    url_input = None
                    for selector in [
                        'input[placeholder*="tiktok"]',
                        'input[placeholder*="TikTok"]',
                        'input[placeholder*="Enter"]',
                        'input[placeholder*="enter"]',
                        'input[placeholder*="link"]',
                        'input[placeholder*="URL"]',
                        'input[placeholder*="url"]',
                        'input[type="url"]',
                        'input[type="text"]',
                    ]:
                        try:
                            el = page.query_selector(selector)
                            if el and el.is_visible():
                                url_input = el
                                break
                        except:
                            pass
                    
                    if not url_input:
                        # Try finding it near the "Free Boost" section
                        try:
                            url_input = page.locator('input').filter(has_text='').first
                        except:
                            pass
                    
                    if not url_input:
                        session.log("⚠️ Can't find URL input field, retrying...")
                        continue
                    
                    url_input.fill(session.video_url)
                    time.sleep(1)
                    session.log(f"📝 URL filled: {session.video_url[:50]}...")
                    
                    # Find and click submit button
                    submit_btn = None
                    for selector in [
                        'button:has-text("Get Free")',
                        'button:has-text("Free Likes")',
                        'button:has-text("Get Your")',
                        'button:has-text("Submit")',
                        'input[type="submit"]',
                    ]:
                        try:
                            el = page.locator(selector).first
                            if el.is_visible():
                                submit_btn = el
                                break
                        except:
                            pass
                    
                    if not submit_btn:
                        session.log("⚠️ Can't find submit button, retrying...")
                        continue
                    
                    submit_btn.click()
                    session.log("🚀 Submitted! Waiting for processing...")
                    time.sleep(3)
                    
                    # Wait for completion (progress bar, placing order, success)
                    completed = False
                    for wait_step in range(120):  # Max 2 minutes wait
                        if session.stop_event.is_set():
                            return
                        
                        try:
                            body_text = page.inner_text("body")
                        except:
                            session.log("💥 Page crashed during wait, restarting...")
                            break
                        
                        # Success detection
                        if any(kw in body_text.lower() for kw in [
                            "order has been placed",
                            "successfully",
                            "order placed",
                            "completed",
                            "thank you",
                            "check progress",
                        ]):
                            completed = True
                            break
                        
                        # Error detection
                        if any(kw in body_text.lower() for kw in [
                            "invalid url",
                            "invalid link",
                            "error",
                            "not found",
                            "please enter a valid",
                        ]):
                            # Check if it's a real error vs page chrome
                            if "invalid" in body_text.lower() or "not found" in body_text.lower():
                                session.log(f"❌ QQTube rejected the URL — may be invalid or expired")
                                time.sleep(10)
                                break
                        
                        # Cooldown appeared after submit
                        if "come back in" in body_text.lower():
                            session.log("⏰ Cooldown detected after submit, rotating...")
                            break
                        
                        # Progress update
                        pct_match = __import__('re').search(r'(\d+)%', body_text)
                        if pct_match:
                            session.set_countdown(f"⏳ Processing: {pct_match.group(1)}%")
                        elif "placing order" in body_text.lower():
                            session.set_countdown("📦 Placing order...")
                        
                        time.sleep(1)
                    
                    session.set_countdown("")
                    
                    if completed:
                        # Extract actual count if possible
                        count = 100  # Default (QQTube gives 100 free likes)
                        total = session.add_count(count)
                        cycle = session.add_cycle()
                        session.log(f"✅ +{count} likes sent! (Total: {total} | Cycle: {cycle})")
                        submission_count += 1
                        session.log(f"🔄 Rotating IP for next submission...")
                    else:
                        session.log("⚠️ Submission didn't complete, rotating IP and retrying...")
                
            except Exception as e:
                err_str = str(e).lower()
                if "crash" in err_str or "target closed" in err_str:
                    session.log("💥 Browser crashed, restarting...")
                else:
                    session.log(f"⚠️ Error: {e}")
                import traceback
                traceback.print_exc()
            finally:
                try:
                    if browser:
                        browser.close()
                except:
                    pass
                if got_slot:
                    with _active_browsers_lock:
                        _active_browsers = max(0, _active_browsers - 1)
                    _browser_semaphore.release()
                    got_slot = False
                gc.collect()
            
            # Small pause between submissions
            if not session.stop_event.is_set():
                time.sleep(2)
        
        session.log("🛑 Tab exhausted all restart attempts.")
    
    except Exception as e:
        session.log(f"❌ Fatal error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        with session.count_lock:
            session.active_tabs = max(0, session.active_tabs - 1)
            if session.active_tabs <= 0 and session.status == "running":
                session.status = "error"


# ═══════════════════════════════════════════════════════════════
#  ROUTES
# ═══════════════════════════════════════════════════════════════

@app.route("/")
def index():
    response = make_response(render_template("index.html"))
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


@app.route("/tor-status")
def tor_status():
    import socket, subprocess as sp
    ready = os.path.exists("/tmp/tor_ready")
    log = ""
    try:
        with open("/tmp/tor.log") as f:
            log = f.read()[-3000:]  # last 3KB
    except:
        log = "No log file yet"
    ports = {}
    for port in range(9050, 9060):
        try:
            s = socket.socket()
            s.settimeout(0.5)
            s.connect(("127.0.0.1", port))
            s.close()
            ports[port] = "OPEN"
        except:
            ports[port] = "CLOSED"
    # Check if tor process exists
    try:
        result = sp.run(["pgrep", "-a", "tor"], capture_output=True, text=True, timeout=3)
        tor_procs = result.stdout.strip()
    except:
        tor_procs = "unknown"
    return jsonify({"ready": ready, "ports": ports, "processes": tor_procs, "log": log})


@app.route("/sessions")
def list_sessions():
    with sessions_lock:
        data = [s.to_dict() for s in sessions.values()]
    return jsonify({"sessions": data, "browsers": _active_browsers, "maxBrowsers": MAX_GLOBAL_BROWSERS})


@app.route("/start", methods=["POST"])
def start():
    data = request.get_json()
    url = data.get("url", "").strip()
    service = data.get("service", "views").strip().lower()
    tabs = int(data.get("tabs", 1))
    if not url:
        return jsonify({"error": "No URL provided"}), 400
    if service not in SERVICES:
        return jsonify({"error": f"Unknown service: {service}. Valid: {', '.join(SERVICES.keys())}"}), 400

    session = Session(url, service=service, num_tabs=tabs)
    with sessions_lock:
        sessions[session.id] = session

    t = threading.Thread(target=run_session, args=(session,), daemon=True)
    session.thread = t
    t.start()

    return jsonify(session.to_dict())


@app.route("/stop/<int:sid>", methods=["POST"])
def stop(sid):
    with sessions_lock:
        session = sessions.get(sid)
    if not session:
        return jsonify({"error": "Not found"}), 404
    session.stop_event.set()
    session.status = "stopping"
    return jsonify({"ok": True})


@app.route("/stream/all")
def stream_all():
    """Single multiplexed SSE stream for ALL sessions — avoids browser connection limits."""
    def generate():
        tracking = {}  # sid → {last_log_idx, last_countdown, ended_sent}

        while True:
            with sessions_lock:
                current_sessions = dict(sessions)

            for sid, session in current_sessions.items():
                if sid not in tracking:
                    tracking[sid] = {"last_log_idx": 0, "last_countdown": "", "ended_sent": False}

                t = tracking[sid]

                # New log lines
                current_len = len(session.logs)
                while t["last_log_idx"] < current_len:
                    data = json.dumps({"type": "log", "sid": sid, "text": session.logs[t["last_log_idx"]]})
                    yield f"data: {data}\n\n"
                    t["last_log_idx"] += 1

                # Countdown update
                cd = session.countdown
                if cd != t["last_countdown"]:
                    t["last_countdown"] = cd
                    data = json.dumps({"type": "countdown", "sid": sid, "text": cd})
                    yield f"data: {data}\n\n"

                # Stats update
                data = json.dumps({
                    "type": "stats",
                    "sid": sid,
                    "count": session.total_count,
                    "unit": session.svc["unit"],
                    "cycles": session.cycles,
                    "status": session.status,
                })
                yield f"data: {data}\n\n"

                # Ended signal (once)
                if session.status in ("stopped", "error") and not t["ended_sent"]:
                    data = json.dumps({"type": "ended", "sid": sid, "status": session.status})
                    yield f"data: {data}\n\n"
                    t["ended_sent"] = True

            # Clean up tracking for removed sessions
            tracking = {sid: v for sid, v in tracking.items() if sid in current_sessions}

            time.sleep(0.5)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@app.route("/remove/<int:sid>", methods=["POST"])
def remove_session(sid):
    """Remove a stopped/error session from the list."""
    with sessions_lock:
        session = sessions.get(sid)
        if not session:
            return jsonify({"error": "Not found"}), 404
        if session.status not in ("stopped", "error"):
            return jsonify({"error": "Can only remove stopped sessions"}), 400
        del sessions[sid]
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=8080)
