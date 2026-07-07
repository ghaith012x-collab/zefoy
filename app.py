from flask import Flask, render_template, request, jsonify, Response
from playwright.sync_api import sync_playwright
import threading, time, re, sys, difflib, json, base64, os
from PIL import Image, ImageOps
from io import BytesIO
from collections import Counter, deque
import numpy as np

def _is_dead(e):
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

# Thread-local tab prefix for log messages
_tab_prefix = threading.local()

# Limit concurrent OCR calls
_ocr_semaphore = threading.Semaphore(3)

# Global limit: max Chromium browsers across ALL sessions at once.
MAX_GLOBAL_BROWSERS = int(os.environ.get("MAX_BROWSERS", "12"))
_browser_semaphore = threading.Semaphore(MAX_GLOBAL_BROWSERS)
_active_browsers = 0
_active_browsers_lock = threading.Lock()

app = Flask(__name__)
ZEFOY = "https://zefoy.com"

# Proxy support
PROXY_URL = ""
USING_TOR = False

def renew_tor_circuit():
    """Signal Tor to build new circuits (get a fresh IP)."""
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
    except Exception:
        pass
    return False

# ═══════════════════════════════════════════════════════════════
#  SERVICES CONFIGURATION - FIXED WITH CORRECT DOM SELECTORS
# ═══════════════════════════════════════════════════════════════

# Zefoy.com service buttons are found by their VISIBLE TEXT.
# The page has buttons like: Hearts, Views, Shares, Favorites, Followers, Comments Hearts
# Each opens a panel with an input field and Search button.

SERVICES = {
    "hearts": {
        "name": "Hearts",
        "emoji": "❤️",
        "button_text": "Hearts",
        "panel_title": "Hearts",
        "unit": "hearts",
        "has_dropdown": False,
        "retry_on_too_many": True,
    },
    "views": {
        "name": "Views",
        "emoji": "👁️",
        "button_text": "Views",
        "panel_title": "Views",
        "unit": "views",
        "has_dropdown": False,
        "retry_on_too_many": True,
    },
    "comment_hearts": {
        "name": "Comment Hearts",
        "emoji": "💬",
        "button_text": "Comments Hearts",
        "panel_title": "Comments Hearts",
        "unit": "hearts",
        "has_dropdown": False,
        "retry_on_too_many": True,
        "special_flow": "comment_hearts",
    },
    "shares": {
        "name": "Shares",
        "emoji": "🔄",
        "button_text": "Shares",
        "panel_title": "Shares",
        "unit": "shares",
        "has_dropdown": False,
        "retry_on_too_many": True,
    },
    "favorites": {
        "name": "Favorites",
        "emoji": "⭐",
        "button_text": "Favorites",
        "panel_title": "Favorites",
        "unit": "favorites",
        "has_dropdown": True,
        "retry_on_too_many": True,
        "select_value": "100",
    },
    "followers": {
        "name": "Followers",
        "emoji": "👥",
        "button_text": "Followers",
        "panel_title": "Followers",
        "unit": "followers",
        "has_dropdown": False,
        "retry_on_too_many": True,
    },
}


# ═══════════════════════════════════════════════════════════════
#  HELPER FUNCTIONS FOR DOM INTERACTION  (ALL FIXED)
# ═══════════════════════════════════════════════════════════════

def click_service_button_by_coords(page, service_text):
    """Click a service button by finding it via JavaScript and clicking at coordinates.
    
    Zefoy.com buttons have text like 'Hearts', 'Views', 'Shares', 'Favorites'.
    We scan all buttons and click the one matching the service name.
    """
    try:
        clicked = page.evaluate(f"""() => {{
            const buttons = document.querySelectorAll('button, a[role="button"]');
            for (const btn of buttons) {{
                const text = btn.innerText.trim();
                if (text === '{service_text}' || text.includes('{service_text}')) {{
                    if (btn.offsetParent !== null) {{
                        btn.click();
                        return {{clicked: true, text: text}};
                    }}
                }}
            }}
            // Partial match fallback
            for (const btn of buttons) {{
                const text = btn.innerText.trim().toLowerCase();
                if (text.includes('{service_text.lower()}')) {{
                    if (btn.offsetParent !== null) {{
                        btn.click();
                        return {{clicked: true, text: text}};
                    }}
                }}
            }}
            return {{clicked: false}};
        }}""")
        return clicked.get("clicked", False)
    except Exception as e:
        print(f"[BOT] Error clicking service button: {e}", flush=True)
        return False


def find_url_input(page):
    """Find the URL input field in the active panel.
    
    The input has placeholder text like 'Enter Video URL'.
    """
    # Strategy 1: Find input with placeholder containing URL/video
    try:
        input_el = page.locator('input[placeholder*="URL" i], input[placeholder*="Video" i], input[placeholder*="Link" i]').first
        if input_el.is_visible(timeout=3000):
            return input_el
    except:
        pass

    # Strategy 2: Find the first visible text input in a panel with a Search button
    try:
        input_el = page.locator('.card input[type="text"], .panel input[type="text"], form input[type="text"]').first
        if input_el.is_visible(timeout=3000):
            return input_el
    except:
        pass

    # Strategy 3: JavaScript fallback - find visible text input
    try:
        result = page.evaluate("""() => {
            const inputs = document.querySelectorAll('input[type="text"], input:not([type])');
            for (const inp of inputs) {
                if (inp.offsetParent !== null && !inp.disabled) {
                    const rect = inp.getBoundingClientRect();
                    if (rect.width > 100 && rect.height > 20) {
                        return {found: true, className: inp.className, id: inp.id, name: inp.name};
                    }
                }
            }
            return {found: false};
        }""")
        if result.get("found"):
            if result.get("id"):
                return page.locator(f"#{result['id']}").first
            elif result.get("className"):
                classes = result["className"].split()
                if classes:
                    return page.locator(f".{classes[0]}").first
            elif result.get("name"):
                return page.locator(f'input[name="{result["name"]}"]').first
    except:
        pass

    return None


def find_search_button(page):
    """Find the Search button in the active panel.
    
    The button has text 'Search' and is next to the URL input.
    """
    # Strategy 1: Button with "Search" text
    try:
        btn = page.locator('button:has-text("Search")').first
        if btn.is_visible(timeout=3000):
            return btn
    except:
        pass

    # Strategy 2: Button with submit type
    try:
        btn = page.locator('button[type="submit"], input[type="submit"]').first
        if btn.is_visible(timeout=3000):
            return btn
    except:
        pass

    # Strategy 3: JavaScript fallback
    try:
        result = page.evaluate("""() => {
            const buttons = document.querySelectorAll('button');
            for (const btn of buttons) {
                const text = btn.innerText.toLowerCase();
                if ((text.includes('search') || btn.type === 'submit') && btn.offsetParent !== null) {
                    return {found: true, className: btn.className, id: btn.id};
                }
            }
            return {found: false};
        }""")
        if result.get("found"):
            if result.get("id"):
                return page.locator(f"#{result['id']}").first
            elif result.get("className"):
                classes = result["className"].split()
                if classes:
                    return page.locator(f".{classes[0]}").first
    except:
        pass

    return None


def click_send_button_js(page):
    """Use JavaScript to find and click the send button.
    
    After clicking Search, zefoy shows:
    - A dark button (btn-dark) with the count number for sending
    - Or a btn-success button
    - Or a button with just a number as text
    
    This function removes overlays and clicks the appropriate button.
    """
    try:
        clicked = page.evaluate("""() => {
            // Remove any overlays that might block clicks
            document.querySelectorAll('iframe').forEach(el => el.remove());
            document.querySelectorAll('.fc-dialog-overlay, .fc-monetization-dialog-container').forEach(el => el.remove());
            
            // Strategy 1: btn-dark (primary send button on zefoy)
            const darkBtns = document.querySelectorAll('button.btn-dark, .btn-dark');
            for (const btn of darkBtns) {
                if (btn.offsetParent !== null) {
                    btn.click();
                    return {clicked: true, type: 'btn-dark', text: btn.innerText.trim()};
                }
            }
            
            // Strategy 2: btn-success
            const successBtns = document.querySelectorAll('button.btn-success, .btn-success');
            for (const btn of successBtns) {
                if (btn.offsetParent !== null) {
                    btn.click();
                    return {clicked: true, type: 'btn-success', text: btn.innerText.trim()};
                }
            }
            
            // Strategy 3: Any button with a number as text (count button)
            const allBtns = document.querySelectorAll('button');
            for (const btn of allBtns) {
                const text = btn.innerText.trim();
                if (/^\\d+$/.test(text) && btn.offsetParent !== null) {
                    btn.click();
                    return {clicked: true, type: 'count-button', text: text};
                }
            }
            
            // Strategy 4: Any non-search button in a panel with input
            const panels = document.querySelectorAll('div, form');
            for (const panel of panels) {
                const hasInput = panel.querySelector('input[type="text"]');
                if (hasInput) {
                    const panelBtns = panel.querySelectorAll('button');
                    for (const btn of panelBtns) {
                        const btnText = btn.innerText.trim().toLowerCase();
                        if (!btnText.includes('search') && btn.offsetParent !== null 
                            && btn.offsetWidth > 50) {
                            btn.click();
                            return {clicked: true, type: 'panel-button', text: btn.innerText.trim()};
                        }
                    }
                }
            }
            
            return {clicked: false};
        }""")
        return clicked
    except Exception as e:
        print(f"[BOT] Error in click_send_button_js: {e}", flush=True)
        return {"clicked": False}


def select_dropdown_value(page, value="100"):
    """Select a value from the limit dropdown (for Favorites service).
    
    Zefoy.com shows a <select> element with options: 25, 50, 75, 100
    """
    try:
        select_el = page.locator("select").first
        if select_el.is_visible(timeout=3000):
            select_el.select_option(value)
            print(f"[BOT] Selected limit: {value}", flush=True)
            time.sleep(0.5)
            return True
    except:
        pass

    # JavaScript fallback
    try:
        result = page.evaluate(f"""() => {{
            const selects = document.querySelectorAll('select');
            for (const sel of selects) {{
                if (sel.offsetParent !== null) {{
                    sel.value = '{value}';
                    sel.dispatchEvent(new Event('change', {{ bubbles: true }}));
                    return {{selected: true}};
                }}
            }}
            return {{selected: false}};
        }}""")
        return result.get("selected", False)
    except:
        pass

    return False


# ═══════════════════════════════════════════════════════════════
#  OCR / CAPTCHA SOLVER (unchanged)
# ═══════════════════════════════════════════════════════════════

WORD_LIST = []

def load_dictionary():
    global WORD_LIST
    try:
        with open("/usr/share/dict/words") as f:
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
        return _solve_captcha_inner(img_bytes)


def _solve_captcha_inner(img_bytes):
    import pytesseract
    from PIL import ImageFilter, ImageEnhance
    img = Image.open(BytesIO(img_bytes))
    if img.mode != "RGB":
        img = img.convert("RGB")
    gray = ImageOps.grayscale(img)
    w, h = gray.size
    big = gray.resize((w * 4, h * 4), Image.LANCZOS)
    arr = np.array(big)

    results = []

    def run_ocr(pil_img, tag=""):
        found = []
        for psm in [7, 8, 13, 6]:
            config = f'--psm {psm} --oem 3 -c tessedit_char_whitelist=abcdefghijklmnopqrstuvwxyz'
            try:
                text = pytesseract.image_to_string(pil_img, config=config).strip()
                text = re.sub(r'[^a-z]', '', text.lower())
                if 3 <= len(text) <= 12:
                    found.append(text)
            except Exception:
                pass
        return found

    for thresh_val in [100, 120, 140, 160, 180, 200]:
        binary_img = Image.fromarray(((arr >= thresh_val) * 255).astype("uint8"))
        results.extend(run_ocr(binary_img, f"thresh-{thresh_val}"))

    for thresh_val in [100, 130, 160]:
        binary_img = Image.fromarray(((arr < thresh_val) * 255).astype("uint8"))
        results.extend(run_ocr(binary_img, f"inv-{thresh_val}"))

    for thresh_val in [110, 130, 150, 170]:
        binary = (arr < thresh_val).astype(np.uint8)
        cleaned = remove_small_components(binary, min_size=25)
        clean_img = Image.fromarray(((1 - cleaned) * 255).astype("uint8"))
        results.extend(run_ocr(clean_img, f"clean-{thresh_val}"))

    try:
        enhanced = ImageEnhance.Contrast(big).enhance(3.0)
        enhanced_arr = np.array(enhanced)
        for thresh_val in [120, 150, 180]:
            binary_img = Image.fromarray(((enhanced_arr >= thresh_val) * 255).astype("uint8"))
            results.extend(run_ocr(binary_img, f"contrast-{thresh_val}"))
    except:
        pass

    try:
        median = big.filter(ImageFilter.MedianFilter(size=3))
        median_arr = np.array(median)
        for thresh_val in [120, 150]:
            binary_img = Image.fromarray(((median_arr >= thresh_val) * 255).astype("uint8"))
            results.extend(run_ocr(binary_img, f"median-{thresh_val}"))
    except:
        pass

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

    most_common = Counter(results).most_common(1)[0][0]
    return most_common


def parse_wait_time(text):
    mins = re.search(r'(\d+)\s*minute', text)
    secs = re.search(r'(\d+)\s*second', text)
    total = 0
    if mins:
        total += int(mins.group(1)) * 60
    if secs:
        total += int(secs.group(1))
    return total


# ═══════════════════════════════════════════════════════════════
#  SESSION MANAGEMENT (unchanged)
# ═══════════════════════════════════════════════════════════════

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
        self.thread = None
        self.count_lock = threading.Lock()
        self.active_tabs = 0

    @property
    def svc(self):
        return SERVICES.get(self.service, SERVICES["views"])

    def log(self, msg):
        pre = getattr(_tab_prefix, "value", "")
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
#  MAIN BOT LOOP - COMPLETELY REWRITTEN WITH CORRECT SELECTORS
# ═══════════════════════════════════════════════════════════════

def run_session(session):
    """Orchestrates one or more tabs for this session."""
    session.status = "running"
    svc_name = session.svc["name"]
    nt = session.num_tabs

    if nt <= 1:
        session.log(f"Launching browser ({svc_name} mode)...")
        run_tab(session, 0)
    else:
        session.log(f"Launching {nt} tabs ({svc_name} mode)...")
        threads = []
        for tab_id in range(nt):
            t = threading.Thread(target=run_tab, args=(session, tab_id), daemon=True)
            t.start()
            threads.append(t)
            time.sleep(5)
        for t in threads:
            t.join()

    if session.status == "running":
        session.log("Session stopped.")
        session.status = "stopped"


def run_tab(session, tab_id):
    """Runs a single bot tab with CORRECTED DOM selectors."""
    import gc
    svc = session.svc
    svc_name = svc["name"]
    svc_button_text = svc["button_text"]
    unit = svc["unit"]
    emoji = svc["emoji"]
    has_dropdown = svc.get("has_dropdown", False)
    select_value = svc.get("select_value", "100")
    retry_on_too_many = svc.get("retry_on_too_many", True)
    special_flow = svc.get("special_flow", None)
    multi = session.num_tabs > 1

    _tab_prefix.value = f"[T{tab_id+1}] " if multi else ""

    MAX_FULL_RESTARTS = 100
    backoff = 5

    with session.count_lock:
        session.active_tabs += 1

    try:
        for full_restart in range(MAX_FULL_RESTARTS):
            if session.stop_event.is_set():
                return

            if full_restart > 0:
                wait_time = min(int(backoff), 30)
                session.log(f"Full restart #{full_restart} (waiting {wait_time}s)...")
                time.sleep(wait_time)
                backoff = min(backoff * 1.5, 30)
                gc.collect()
                if USING_TOR:
                    session.log("Requesting fresh Tor IP...")
                    renew_tor_circuit()
            else:
                if multi:
                    session.log("Starting tab...")

            browser = None
            page = None

            got_slot = False
            try:
                if not _browser_semaphore.acquire(timeout=1):
                    session.log(f"Waiting for browser slot (max {MAX_GLOBAL_BROWSERS} globally)...")
                    _browser_semaphore.acquire()
                got_slot = True
                with _active_browsers_lock:
                    global _active_browsers
                    _active_browsers += 1
                    session.log(f"Browser slot acquired ({_active_browsers}/{MAX_GLOBAL_BROWSERS} in use)")
            except Exception:
                pass

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
                            "--disable-ipc-flooding-protection",
                            "--memory-pressure-off",
                        ],
                    }
                    if USING_TOR:
                        tor_port = 9050 + (tab_id % 10)
                        if full_restart == 0:
                            session.log(f"Routing through Tor (port {tor_port})...")
                        for _tw in range(60):
                            if os.path.exists("/tmp/tor_ready"):
                                break
                            if _tw == 0:
                                session.log("Waiting for Tor to bootstrap...")
                            time.sleep(1)
                        launch_opts["proxy"] = {"server": f"socks5://127.0.0.1:{tor_port}"}
                    elif PROXY_URL:
                        if full_restart == 0:
                            session.log(f"Using proxy: {PROXY_URL.split('@')[-1] if '@' in PROXY_URL else PROXY_URL}")
                        launch_opts["proxy"] = {"server": PROXY_URL}

                    browser = p.chromium.launch(**launch_opts)
                    page = browser.new_page(viewport={"width": 800, "height": 600})
                    page.on("dialog", lambda d: d.accept())

                    def _safe_check(pg):
                        try:
                            pg.title()
                            return True
                        except:
                            return False

                    # -- Load zefoy --
                    session.log("Loading zefoy.com...")
                    try:
                        page.goto(ZEFOY, wait_until="domcontentloaded", timeout=60000)
                    except Exception:
                        session.log("Page crashed on load, restarting...")
                        continue
                    time.sleep(5)

                    if not _safe_check(page):
                        session.log("Page crashed on load, restarting...")
                        continue

                    # -- Check page / Solve captcha --
                    session.log("Checking for captcha...")

                    captcha_detected = False
                    page_ready = False

                    for page_attempt in range(10):
                        if session.stop_event.is_set():
                            return

                        if not _safe_check(page):
                            session.log("Crashed during page check, restarting...")
                            break

                        try:
                            page_title = page.title().lower()
                            page_text = page.inner_text("body")[:200].lower()
                            if "502" in page_title or "502 bad gateway" in page_text:
                                session.log(f"Zefoy is down (502), retrying ({page_attempt + 1}/10)...")
                                time.sleep(10 + page_attempt * 3)
                                page.reload(wait_until="domcontentloaded")
                                time.sleep(5)
                                continue
                            if "503" in page_title or "cloudflare" in page_text or "just a moment" in page_text:
                                session.log(f"Zefoy loading/Cloudflare ({page_attempt + 1}/10)...")
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
                            page.locator('button:has-text("Hearts"), button:has-text("Views"), button:has-text("Favorites")').first.wait_for(timeout=20000)
                            session.log("No captcha needed - service buttons visible")
                            page_ready = True
                            break
                        except:
                            pass

                        session.log(f"Page not ready, reloading ({page_attempt + 1}/10)...")
                        try:
                            page.reload(wait_until="domcontentloaded")
                        except Exception:
                            session.log("Error reloading - restarting tab...")
                            break
                        time.sleep(10 + page_attempt * 3)
                    else:
                        session.log("Page never became ready, restarting...")
                        continue

                    if not captcha_detected and not page_ready:
                        continue

                    if captcha_detected:
                        session.log("Captcha detected, solving...")
                        captcha_solved = False
                        for captcha_attempt in range(20):
                            if session.stop_event.is_set():
                                return

                            if not _safe_check(page):
                                session.log("Crashed during captcha, restarting...")
                                break

                            try:
                                captcha_img = page.locator("#captcha-img, img[src*='CAPTCHA'], img[src*='captcha']")
                                try:
                                    captcha_img.first.wait_for(state="visible", timeout=10000)
                                except:
                                    session.log("Captcha image not loading, reloading...")
                                    page.reload(wait_until="domcontentloaded")
                                    time.sleep(5)
                                    continue

                                session.log(f"Solving captcha (attempt {captcha_attempt + 1})...")
                                time.sleep(2)
                                captcha_bytes = captcha_img.first.screenshot()
                                answer = solve_captcha(captcha_bytes)

                                if not answer:
                                    session.log("OCR failed, refreshing captcha...")
                                    try:
                                        page.locator(".refresh-capthca-btn-new, [onclick*='refresh'], .captcha-refresh").first.click()
                                    except:
                                        page.reload(wait_until="domcontentloaded")
                                    time.sleep(3)
                                    continue

                                session.log(f"Answer: '{answer}'")
                                captcha_input = page.locator("#captchatoken, input[name='captcha_secure'], input[placeholder*='aptcha']")
                                captcha_input.first.fill(answer)
                                time.sleep(0.5)
                                page.locator("button.submit-captcha, form .btn-primary[type='submit']").first.click()
                                time.sleep(5)

                                try:
                                    page.locator('button:has-text("Hearts"), button:has-text("Views"), button:has-text("Favorites")').first.wait_for(timeout=8000)
                                    session.log("Captcha solved!")
                                    captcha_solved = True
                                    break
                                except:
                                    session.log(f"Wrong answer '{answer}', retrying...")
                                    try:
                                        page.locator(".modal .btn-secondary, .modal .close, .swal2-confirm, [class*='close']").first.click()
                                    except:
                                        pass
                                    time.sleep(1)
                                    try:
                                        page.locator(".refresh-capthca-btn-new, [onclick*='refresh'], .captcha-refresh").first.click()
                                    except:
                                        pass
                                    time.sleep(3)
                            except Exception as e:
                                if _is_dead(e):
                                    session.log("Crashed during captcha, restarting...")
                                    break
                                else:
                                    session.log(f"Captcha error: {e}")
                                time.sleep(2)

                        if not captcha_solved:
                            continue

                    # ============================================================
                    #  CLICK SERVICE BUTTON - FIXED: Use text-based selection
                    # ============================================================
                    session.log(f"{emoji} Looking for {svc_name} button...")

                    clicked = click_service_button_by_coords(page, svc_button_text)
                    if clicked:
                        session.log(f"{emoji} {svc_name} button clicked!")
                    else:
                        try:
                            btn_loc = page.locator(f'button:has-text("{svc_button_text}")').first
                            btn_loc.wait_for(timeout=10000)
                            btn_loc.click()
                            session.log(f"{emoji} {svc_name} button clicked (Playwright)!")
                        except Exception:
                            session.log(f"Could not find {svc_name} button")
                            try:
                                body_text = page.inner_text("body").lower()
                                if "soon will be update" in body_text and svc_button_text.lower() in body_text:
                                    session.log(f"{svc_name} is unavailable on Zefoy (soon will be update).")
                            except:
                                pass
                            continue

                    time.sleep(2)
                    session.log(f"{emoji} {svc_name} panel opened!")

                    # ============================================================
                    #  MAIN LOOP - FIXED: Correct element detection & clicking
                    # ============================================================
                    backoff = 5
                    url_filled = False
                    input_fail_count = 0
                    MAX_INPUT_FAILS = 5

                    while not session.stop_event.is_set():
                        if not _safe_check(page):
                            session.log("Page crashed in main loop, restarting...")
                            break

                        cycle = session.add_cycle()
                        session.log(f"Cycle {cycle}")

                        try:
                            # -- Find URL input (FIXED) --
                            url_input = find_url_input(page)

                            if url_input is None:
                                input_fail_count += 1
                                try:
                                    body_snip = page.inner_text("body")[:300].lower()
                                except:
                                    session.log("Page unreadable, restarting...")
                                    break

                                if "502" in body_snip or "bad gateway" in body_snip or "503" in body_snip:
                                    session.log("Zefoy is down, restarting...")
                                    break
                                if page.locator("#captcha-img, img[src*='captcha'], img[src*='CAPTCHA']").count() > 0:
                                    session.log("Session expired (captcha), restarting...")
                                    break
                                if input_fail_count >= MAX_INPUT_FAILS:
                                    session.log(f"Input not found after {MAX_INPUT_FAILS} attempts, restarting...")
                                    break

                                session.log(f"Input not visible, retrying ({input_fail_count}/{MAX_INPUT_FAILS})...")
                                click_service_button_by_coords(page, svc_button_text)
                                time.sleep(3)
                                continue

                            input_fail_count = 0

                            # Fill URL
                            if not url_filled:
                                url_input.fill("")
                                time.sleep(0.3)
                                url_input.fill(session.video_url)
                                time.sleep(1)
                                url_filled = True
                                session.log(f"URL filled: {session.video_url[:50]}...")

                            # -- Find and click Search button (FIXED) --
                            search_btn = find_search_button(page)
                            if search_btn is None:
                                session.log("Search button not found, retrying...")
                                time.sleep(3)
                                continue

                            search_btn.click()
                            time.sleep(3)
                            session.log("Search clicked")

                        except Exception as fill_err:
                            if _is_dead(fill_err):
                                session.log("Crashed filling URL, restarting...")
                                break
                            session.log(f"Error: {fill_err}")
                            time.sleep(3)
                            continue

                        # ===================================================
                        #  COMMENT HEARTS SPECIAL FLOW
                        # ===================================================
                        if special_flow == "comment_hearts":
                            target_user = session.username.lstrip("@").lower()

                            try:
                                body_check = page.inner_text("body").lower()
                            except:
                                session.log("Page crash, restarting...")
                                break

                            # Too many requests? Click Search again
                            if "too many" in body_check or "slow down" in body_check:
                                session.log("Too many requests, clicking Search...")
                                try:
                                    search_btn = find_search_button(page)
                                    if search_btn:
                                        search_btn.click()
                                except:
                                    pass
                                time.sleep(3)
                                continue

                            # Countdown? Wait then click Search
                            if "please wait" in body_check and ("minute" in body_check or "second" in body_check):
                                wait_secs = parse_wait_time(body_check)
                                if wait_secs <= 0:
                                    wait_secs = 60
                                wait_secs += 3
                                session.log(f"Countdown: {wait_secs}s")
                                for remaining in range(wait_secs, 0, -1):
                                    if session.stop_event.is_set():
                                        break
                                    mins = remaining // 60
                                    secs = remaining % 60
                                    time_str = f"{mins}m {secs:02d}s" if mins > 0 else f"{secs}s"
                                    session.set_countdown(f"Wait {time_str}")
                                    time.sleep(1)
                                session.set_countdown("")
                                session.log("Countdown done - clicking Search...")
                                try:
                                    search_btn = find_search_button(page)
                                    if search_btn:
                                        search_btn.click()
                                except:
                                    pass
                                time.sleep(3)
                                continue

                            # Wait for comment count button and click it
                            try:
                                count_btn = page.locator("button.wbutton").first
                                count_btn.wait_for(state="visible", timeout=20000)
                                count_btn.click()
                                time.sleep(4)
                                session.log("Comments loaded")
                            except:
                                try:
                                    snippet = page.inner_text("body")[:150]
                                    session.log(f"Comment button not found. Page: {snippet}")
                                except:
                                    session.log("Comment button not found")
                                try:
                                    search_btn = find_search_button(page)
                                    if search_btn:
                                        search_btn.click()
                                except:
                                    pass
                                time.sleep(3)
                                continue

                            # Find target username - paginate through comment pages
                            found_user = False
                            crashed = False
                            max_pages = 250
                            for pg in range(max_pages):
                                if session.stop_event.is_set():
                                    break
                                try:
                                    result = page.evaluate(
                                        """(targetUser) => {
                                            const forms = document.querySelectorAll('form.w1a');
                                            for (let i = 0; i < forms.length; i++) {
                                                const userEl = forms[i].querySelector('.kadi-rengi');
                                                if (!userEl) continue;
                                                const uname = userEl.innerText.trim().replace('@','').toLowerCase();
                                                if (uname === targetUser) {
                                                    return {found: true, index: i, total: forms.length};
                                                }
                                            }
                                            const nextBtn = document.querySelector('li[title="Next"] button');
                                            const hasNext = nextBtn && !nextBtn.disabled;
                                            return {found: false, total: forms.length, hasNext: hasNext};
                                        }""",
                                        target_user,
                                    )

                                    if result.get("found"):
                                        idx = result["index"]
                                        form_loc = page.locator("form.w1a").nth(idx)
                                        form_loc.locator("select[name='select_lmt']").select_option("100")
                                        time.sleep(1)
                                        form_loc.locator("button[type='submit']").click()
                                        session.log(f"Sent 100 hearts to @{target_user} (page {pg + 1})")
                                        session.add_count(100)
                                        found_user = True
                                        time.sleep(3)
                                        break

                                    if result.get("hasNext"):
                                        if pg == 0:
                                            session.log(f"@{target_user} not on page 1, paginating...")
                                        page.locator('li[title="Next"] button').click()
                                        time.sleep(4)
                                    else:
                                        total_scanned = (pg * 40) + result.get("total", 0)
                                        session.log(f"@{target_user} not found in {total_scanned} comments")
                                        break
                                except Exception as ce:
                                    if _is_dead(ce):
                                        crashed = True
                                        session.log("Crashed during pagination, restarting...")
                                        break
                                    session.log(f"Pagination error: {ce}")
                                    break

                            if crashed:
                                break
                            if not found_user:
                                time.sleep(2)
                                try:
                                    search_btn = find_search_button(page)
                                    if search_btn:
                                        search_btn.click()
                                except:
                                    pass
                                time.sleep(3)
                            continue

                        # ===================================================
                        #  STANDARD FLOW: Hearts, Views, Shares, Favorites
                        # ===================================================
                        max_checks = 60
                        crashed = False
                        for check_i in range(max_checks):
                            if session.stop_event.is_set():
                                break

                            try:
                                body = page.inner_text("body")
                            except Exception as e:
                                if _is_dead(e):
                                    crashed = True
                                    break
                                time.sleep(1)
                                continue

                            lower_body = body.lower()

                            # -- "Too many requests" - click Search again --
                            if retry_on_too_many and ("too many" in lower_body or "slow down" in lower_body):
                                session.log("Too many requests - clicking Search again...")
                                time.sleep(2)
                                try:
                                    search_btn = find_search_button(page)
                                    if search_btn:
                                        search_btn.click()
                                except:
                                    pass
                                time.sleep(3)
                                break

                            # -- Countdown - wait then click Search --
                            if "please wait" in lower_body and ("minute" in lower_body or "second" in lower_body):
                                wait_secs = parse_wait_time(body)
                                if wait_secs <= 0:
                                    wait_secs = 60
                                wait_secs += 3
                                session.log(f"Countdown: {wait_secs}s")
                                for remaining in range(wait_secs, 0, -1):
                                    if session.stop_event.is_set():
                                        break
                                    mins = remaining // 60
                                    secs = remaining % 60
                                    time_str = f"{mins}m {secs:02d}s" if mins > 0 else f"{secs}s"
                                    session.set_countdown(f"Wait {time_str}")
                                    time.sleep(1)
                                session.set_countdown("")
                                session.log("Countdown done - clicking Search...")
                                try:
                                    search_btn = find_search_button(page)
                                    if search_btn:
                                        search_btn.click()
                                except:
                                    pass
                                time.sleep(3)
                                break

                            # -- "READY" text - click Search --
                            if "ready" in lower_body and "next submit" in lower_body:
                                session.log("Ready - clicking Search...")
                                try:
                                    search_btn = find_search_button(page)
                                    if search_btn:
                                        search_btn.click()
                                except:
                                    pass
                                time.sleep(3)
                                break

                            # -- Success message --
                            if "successfully" in lower_body and "sent" in lower_body:
                                count = 0
                                for line in body.split("\n"):
                                    if "successfully" in line.lower():
                                        session.log(f"Raw: {line.strip()[:120]}")
                                        try:
                                            nums = [int(m) for m in re.findall(r"\d+", line) if not (2020 <= int(m) <= 2035) and int(m) < 100000]
                                        except:
                                            nums = []
                                        if nums:
                                            count = max(nums)
                                        break
                                add_amt = count if count > 0 else (int(select_value) if has_dropdown else 1)
                                new_total = session.add_count(add_amt)
                                session.log(f"+{add_amt} {unit}! Total: {new_total:,}")
                                url_filled = False
                                time.sleep(3)
                                break

                            # -- "Checking Timer" - still loading --
                            if "checking timer" in lower_body:
                                if check_i < 30:
                                    time.sleep(1)
                                    continue
                                else:
                                    session.log("Still checking timer after 30s, retrying...")
                                    break

                            # -- Loading spinner - wait --
                            try:
                                if page.locator(".spinner, .loading").count() > 0 and check_i < 30:
                                    time.sleep(1)
                                    continue
                            except:
                                pass

                            # -- Send button visible - click it! --
                            try:
                                # Handle dropdown if present (Favorites)
                                if has_dropdown:
                                    select_dropdown_value(page, select_value)

                                # Try JavaScript click first
                                clicked = click_send_button_js(page)
                                if clicked.get("clicked"):
                                    session.log(f"{emoji} Clicked send button ({clicked.get('type')}: {clicked.get('text', 'N/A')})")
                                    time.sleep(3)
                                    continue
                            except:
                                pass

                            # -- Still loading / waiting --
                            if check_i < 30:
                                time.sleep(1)
                                continue
                            else:
                                session.log(f"No response after {check_i}s, breaking to retry...")
                                break

                        if crashed:
                            session.log("Crashed in main loop, restarting tab...")
                            break

                        time.sleep(2)
                        if cycle % 10 == 0:
                            gc.collect()

            except Exception as inner_err:
                if _is_dead(inner_err):
                    session.log("Browser crashed, restarting tab...")
                else:
                    import traceback

                    session.log(f"Error: {inner_err} - restarting tab...")
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

        session.log("Tab exhausted all restart attempts.")

    except Exception as e:
        session.log(f"Fatal error: {e}")
        import traceback

        traceback.print_exc()
    finally:
        with session.count_lock:
            session.active_tabs = max(0, session.active_tabs - 1)
            if session.active_tabs <= 0 and session.status == "running":
                session.status = "error"


# ═══════════════════════════════════════════════════════════════
#  FLASK ROUTES
# ═══════════════════════════════════════════════════════════════

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
    url = data.get("url", "").strip()
    service = data.get("service", "views").strip().lower()
    username = data.get("username", "").strip()
    tabs = int(data.get("tabs", 1))
    if not url:
        return jsonify({"error": "No URL provided"}), 400
    if service not in SERVICES:
        return jsonify({"error": f"Unknown service: {service}"}), 400
    if service == "comment_hearts" and not username:
        return jsonify({"error": "Username is required for Comment Hearts"}), 400

    session = Session(url, service=service, num_tabs=tabs, username=username)
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
    def generate():
        tracking = {}
        while True:
            with sessions_lock:
                current_sessions = dict(sessions)
            for sid, session in current_sessions.items():
                if sid not in tracking:
                    tracking[sid] = {"last_log_idx": 0, "last_countdown": "", "ended_sent": False}
                t = tracking[sid]
                current_len = len(session.logs)
                while t["last_log_idx"] < current_len:
                    data = json.dumps({"type": "log", "sid": sid, "text": session.logs[t["last_log_idx"]]})
                    yield f"data: {data}\n\n"
                    t["last_log_idx"] += 1
                cd = session.countdown
                if cd != t["last_countdown"]:
                    t["last_countdown"] = cd
                    data = json.dumps({"type": "countdown", "sid": sid, "text": cd})
                    yield f"data: {data}\n\n"
                data = json.dumps(
                    {
                        "type": "stats",
                        "sid": sid,
                        "count": session.total_count,
                        "unit": session.svc["unit"],
                        "cycles": session.cycles,
                        "status": session.status,
                    }
                )
                yield f"data: {data}\n\n"
                if session.status in ("stopped", "error") and not t["ended_sent"]:
                    data = json.dumps({"type": "ended", "sid": sid, "status": session.status})
                    yield f"data: {data}\n\n"
                    t["ended_sent"] = True
            tracking = {sid: v for sid, v in tracking.items() if sid in current_sessions}
            time.sleep(0.5)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@app.route("/remove/<int:sid>", methods=["POST"])
def remove_session(sid):
    with sessions_lock:
        session = sessions.get(sid)
        if not session:
            return jsonify({"error": "Not found"}), 404
        session.stop_event.set()
        session.status = "stopping"
        del sessions[sid]
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=8080)
