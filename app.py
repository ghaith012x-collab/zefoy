from flask import Flask, render_template, request, jsonify, Response
from playwright.sync_api import sync_playwright
import threading, time, re, sys, difflib, json, base64, os
from PIL import Image, ImageOps
from io import BytesIO
from collections import Counter, deque
import numpy as np

# Thread-local tab prefix for log messages
_tab_prefix = threading.local()

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
        self.num_tabs = max(1, min(num_tabs, 10))  # clamp 1-10
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

    if nt <= 1:
        session.log(f"🚀 Launching browser ({svc_name} mode)...")
        run_tab(session, 0)
    else:
        session.log(f"🚀 Launching {nt} tabs ({svc_name} mode)...")
        threads = []
        for tab_id in range(nt):
            t = threading.Thread(target=run_tab, args=(session, tab_id), daemon=True)
            t.start()
            threads.append(t)
            time.sleep(3)  # stagger launches to avoid overwhelming
        for t in threads:
            t.join()

    if session.status == "running":
        session.log("🛑 Session stopped.")
        session.status = "stopped"


def run_tab(session, tab_id):
    """Runs a single bot tab — each gets its own browser + Tor circuit."""
    svc = session.svc
    svc_name = svc["name"]
    btn_cls = svc["button_class"]
    menu_cls = svc["menu_class"]
    unit = svc["unit"]
    emoji = svc["emoji"]
    multi = session.num_tabs > 1

    # Set thread-local prefix for log messages
    _tab_prefix.value = f"[T{tab_id+1}] " if multi else ""

    try:
        with sync_playwright() as p:
            if multi:
                session.log(f"🚀 Starting tab...")

            launch_opts = {
                "headless": True,
                "args": ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
            }
            # Each tab gets its own Tor SOCKS port (9050-9059) → different IP
            if USING_TOR:
                tor_port = 9050 + (tab_id % 10)
                session.log(f"🧅 Routing through Tor (port {tor_port})...")
                # Wait for Tor to be ready
                import os
                for _tw in range(60):
                    if os.path.exists("/tmp/tor_ready"):
                        break
                    if _tw == 0:
                        session.log("⏳ Waiting for Tor to bootstrap...")
                    time.sleep(1)
                launch_opts["proxy"] = {
                    "server": f"socks5://127.0.0.1:{tor_port}",
                }
            elif PROXY_URL:
                session.log(f"🌐 Using proxy: {PROXY_URL.split('@')[-1] if '@' in PROXY_URL else PROXY_URL}")
                launch_opts["proxy"] = {"server": PROXY_URL}

            browser = p.chromium.launch(**launch_opts)

            with session.count_lock:
                session.active_tabs += 1

            try:
                page = browser.new_page()
                page.on("dialog", lambda d: d.accept())

                # ── Load zefoy ──
                session.log("🌐 Loading zefoy.com...")
                page.goto(ZEFOY, wait_until="domcontentloaded", timeout=60000)
                time.sleep(5)

                # ── Solve captcha ──
                session.log("🔐 Checking for captcha...")

                captcha_detected = False
                page_ready = False

                for page_attempt in range(10):
                    if session.stop_event.is_set():
                        break

                    # Check if site is returning an error page
                    try:
                        page_title = page.title().lower()
                        page_text = page.inner_text("body")[:200].lower()
                        if "502" in page_title or "502 bad gateway" in page_text:
                            session.log(f"🔴 Zefoy is down (502 error), retrying ({page_attempt + 1}/10)...")
                            time.sleep(10 + page_attempt * 3)
                            page.reload(wait_until="domcontentloaded")
                            time.sleep(5)
                            continue
                        if "503" in page_title or "cloudflare" in page_text or "just a moment" in page_text:
                            session.log(f"🔴 Zefoy loading/Cloudflare check ({page_attempt + 1}/10)...")
                            time.sleep(10 + page_attempt * 3)
                            page.reload(wait_until="domcontentloaded")
                            time.sleep(5)
                            continue
                    except:
                        pass

                    # Check for captcha elements
                    try:
                        page.locator("#captcha-img, .wrapper-capth, #captchatoken").first.wait_for(state="visible", timeout=10000)
                        captcha_detected = True
                        break
                    except:
                        pass

                    # No captcha — maybe already past it?
                    try:
                        page.locator(ANY_SERVICE_BUTTON).first.wait_for(timeout=5000)
                        session.log("✅ No captcha needed — service buttons already visible")
                        page_ready = True
                        break
                    except:
                        pass

                    # Neither found — reload and try again
                    session.log(f"⚠️ Page not ready, reloading (attempt {page_attempt + 1}/10)...")
                    page.reload(wait_until="domcontentloaded")
                    time.sleep(5 + page_attempt * 2)  # increasing wait

                if captcha_detected:
                    session.log("🔐 Captcha detected, solving...")
                    for captcha_attempt in range(15):
                        if session.stop_event.is_set():
                            break
                        try:
                            # Wait for captcha image to actually appear
                            captcha_img = page.locator("#captcha-img, img[src*='CAPTCHA'], img[src*='captcha']")
                            try:
                                captcha_img.first.wait_for(state="visible", timeout=10000)
                            except:
                                # Maybe page hasn't loaded — reload and retry
                                session.log("⚠️ Captcha image not loading, reloading page...")
                                page.reload(wait_until="domcontentloaded")
                                time.sleep(5)
                                continue

                            session.log(f"🔐 Solving captcha (attempt {captcha_attempt + 1})...")
                            time.sleep(2)
                            captcha_bytes = captcha_img.first.screenshot()
                            answer = solve_captcha(captcha_bytes)

                            if not answer:
                                session.log("⚠️ OCR failed, refreshing captcha...")
                                try: page.locator(".refresh-capthca-btn-new, [onclick*='refresh'], .captcha-refresh").first.click()
                                except: page.reload(wait_until="domcontentloaded")
                                time.sleep(3)
                                continue

                            session.log(f"🔤 Answer: '{answer}'")
                            # Fill and submit
                            captcha_input = page.locator("#captchatoken, input[name='captcha_secure'], input[placeholder*='aptcha']")
                            captcha_input.first.fill(answer)
                            time.sleep(0.5)
                            page.locator("button.submit-captcha, form .btn-primary[type='submit']").first.click()
                            time.sleep(5)

                            # Check if solved — any service button should appear
                            try:
                                page.locator(ANY_SERVICE_BUTTON).first.wait_for(timeout=8000)
                                session.log("✅ Captcha solved!")
                                break
                            except:
                                session.log(f"❌ Wrong answer '{answer}', retrying...")
                                # Dismiss any error modal
                                try: page.locator(".modal .btn-secondary, .modal .close, .swal2-confirm, [class*='close']").first.click()
                                except: pass
                                time.sleep(1)
                                # Refresh captcha
                                try: page.locator(".refresh-capthca-btn-new, [onclick*='refresh'], .captcha-refresh").first.click()
                                except: pass
                                time.sleep(3)
                        except Exception as e:
                            session.log(f"⚠️ Captcha error: {e}")
                            time.sleep(2)

                # ── Click service button ──
                session.log(f"{emoji} Looking for {svc_name} button...")
                try:
                    page.locator(f".{btn_cls}").wait_for(timeout=30000)
                except:
                    # Check if the button exists but is disabled
                    try:
                        btn_el = page.locator(f".{btn_cls}")
                        if btn_el.count() > 0 and btn_el.get_attribute("disabled"):
                            session.log(f"❌ {svc_name} is currently unavailable on Zefoy. Try a different service.")
                        else:
                            session.log(f"❌ {svc_name} button not found. Stopping.")
                    except:
                        session.log(f"❌ {svc_name} button not found. Stopping.")
                    return  # just this tab stops

                page.locator(f".{btn_cls}").click()
                time.sleep(2)
                session.log(f"✅ {svc_name} panel opened!")

                # ── Main loop ──
                zero_streak = 0  # consecutive cycles returning 0
                no_response_streak = 0  # consecutive "no response" cycles
                MAX_NO_RESPONSE = 5  # reload page after this many
                MAX_ZERO_STREAK = 10  # stop tab after this many 0-count results
                while not session.stop_event.is_set():
                    cycle = session.add_cycle()
                    session.log(f"🔄 Cycle {cycle}")

                    # Fill URL
                    url_input = page.locator(f".{menu_cls} input[type='text'], .{menu_cls} input[placeholder]").first
                    url_input.fill("")
                    time.sleep(0.3)
                    url_input.fill(session.video_url)
                    time.sleep(1)

                    # Click Search
                    page.locator(f".{menu_cls} button[type='submit']").first.click()
                    time.sleep(3)

                    # ── Analyze page state in a loop ──
                    for check_round in range(120):
                        if session.stop_event.is_set():
                            break

                        page_state = page.evaluate("""(menuClass) => {
                            const body = document.body.innerText || '';
                            const lower = body.toLowerCase();

                            // Rate limit countdown
                            const countdown = document.getElementById('login-countdown');
                            if (countdown && countdown.offsetParent !== null) {
                                const text = countdown.innerText || '';
                                if (text && (text.toLowerCase().includes('wait') ||
                                    text.toLowerCase().includes('minute') ||
                                    text.toLowerCase().includes('second'))) {
                                    return {type: 'ratelimit', text: text};
                                }
                            }

                            // Success
                            if (lower.includes('successfully')) {
                                // Try multiple patterns: "Successfully 100", "100 sent successfully", etc.
                                const nums = body.match(/\\d+/g);
                                let count = 0;
                                if (nums) {
                                    // Find the largest number near "successfully" (likely the count)
                                    const lines = body.split('\\n');
                                    for (const line of lines) {
                                        if (line.toLowerCase().includes('successfully')) {
                                            const lineNums = line.match(/\\d+/g);
                                            if (lineNums) {
                                                count = Math.max(...lineNums.map(Number));
                                            }
                                            break;
                                        }
                                    }
                                    // Fallback: largest number on page
                                    if (count === 0) {
                                        count = Math.max(...nums.map(Number).filter(n => n < 100000));
                                    }
                                }
                                return {type: 'success', count: count || 0};
                            }

                            // Spinner
                            const spinners = document.querySelectorAll('.fa-spinner, .fa-spin, .spinner, [class*="loading"], [class*="spin"]');
                            for (const s of spinners) {
                                if (s.offsetParent !== null) return {type: 'loading'};
                            }

                            // Action bar (video/profile result)
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

                            // Fallback rate limit text
                            if (lower.includes('please wait') && (lower.includes('minute') || lower.includes('second'))) {
                                return {type: 'ratelimit', text: body.substring(0, 500)};
                            }

                            return {type: 'waiting'};
                        }""", menu_cls)

                        state_type = page_state.get('type', 'waiting') if page_state else 'waiting'

                        if state_type == 'ratelimit':
                            no_response_streak = 0  # page is alive
                            timer_text = page_state.get('text', '')
                            wait_secs = parse_wait_time(timer_text)
                            if wait_secs <= 0:
                                wait_secs = 60
                            wait_secs += 5  # buffer
                            session.log(f"⏳ Rate limited ({wait_secs}s)")

                            # Live countdown — updates in-place via session.countdown
                            for remaining in range(wait_secs, 0, -1):
                                if session.stop_event.is_set():
                                    break
                                mins = remaining // 60
                                secs = remaining % 60
                                time_str = f"{mins}m {secs:02d}s" if mins > 0 else f"{secs}s"
                                session.set_countdown(f"⏳ {time_str} remaining")
                                time.sleep(1)

                            session.set_countdown("")
                            session.log("✅ Rate limit done, retrying...")

                            # Re-fill URL and search
                            time.sleep(1)
                            url_input.fill("")
                            time.sleep(0.3)
                            url_input.fill(session.video_url)
                            time.sleep(1)
                            page.locator(f".{menu_cls} button[type='submit']").first.click()
                            time.sleep(3)
                            continue

                        elif state_type == 'success':
                            count = page_state.get('count', 0)
                            new_total = session.add_count(count)
                            if count > 0:
                                zero_streak = 0
                                no_response_streak = 0
                                session.log(f"🎉 +{count} {unit}! Total: {new_total:,}")
                            else:
                                zero_streak += 1
                                no_response_streak = 0
                                if zero_streak >= MAX_ZERO_STREAK:
                                    session.log(f"🔴 {zero_streak} consecutive 0 {unit} — Zefoy may be blocking. Stopping tab.")
                                    return
                                session.log(f"⚠️ Zefoy returned 0 {unit} (streak: {zero_streak}/{MAX_ZERO_STREAK}) — retrying...")
                            break

                        elif state_type == 'bar':
                            # If there's a "Select Limit" dropdown, pick the highest value
                            if page_state.get('hasSelect') and page_state.get('selectOptions'):
                                try:
                                    best = page_state['selectOptions'][-1]  # highest
                                    page.locator("select#selectlimit, select[name='select_lmt'], select.form-select").first.select_option(best)
                                    session.log(f"📊 Selected limit: {best}")
                                    time.sleep(0.5)
                                except Exception as sel_err:
                                    session.log(f"⚠️ Could not set limit dropdown: {sel_err}")

                            x, y = page_state['x'], page_state['y']
                            session.log(f"{emoji} Sending {unit}...")
                            page.mouse.click(x, y)
                            time.sleep(2)

                            # Wait for success
                            count = 0
                            for _ in range(30):
                                try:
                                    body = page.inner_text("body")
                                    if "successfully" in body.lower():
                                        # Find count: try multiple patterns
                                        for line in body.split('\n'):
                                            if 'successfully' in line.lower():
                                                line_nums = re.findall(r'\d+', line)
                                                if line_nums:
                                                    count = max(int(n) for n in line_nums)
                                                break
                                        # Fallback: any number near "successfully"
                                        if count == 0:
                                            all_nums = re.findall(r'\d+', body)
                                            if all_nums:
                                                count = max(int(n) for n in all_nums if int(n) < 100000)
                                        new_total = session.add_count(count)
                                        break
                                except:
                                    pass
                                time.sleep(1)

                            if count > 0:
                                zero_streak = 0
                                no_response_streak = 0
                                session.log(f"🎉 +{count} {unit}! Total: {new_total:,}")
                            else:
                                zero_streak += 1
                                no_response_streak = 0
                                if zero_streak >= MAX_ZERO_STREAK:
                                    session.log(f"🔴 {zero_streak} consecutive 0 {unit} — Zefoy may be blocking. Stopping tab.")
                                    return
                                session.log(f"⚠️ Zefoy returned 0 {unit} (streak: {zero_streak}/{MAX_ZERO_STREAK}) — retrying...")
                            break

                        elif state_type == 'loading':
                            time.sleep(1)
                            continue

                        else:  # waiting
                            if check_round < 30:
                                time.sleep(1)
                                continue
                            else:
                                no_response_streak += 1
                                if no_response_streak >= MAX_NO_RESPONSE:
                                    session.log(f"🔴 {no_response_streak} consecutive no-responses — reloading page...")
                                    no_response_streak = 0
                                    try:
                                        page.reload(wait_until="domcontentloaded")
                                        time.sleep(5)
                                        # Re-check for service button
                                        try:
                                            page.locator(f".{btn_cls}").wait_for(timeout=10000)
                                            page.locator(f".{btn_cls}").click()
                                            time.sleep(2)
                                            session.log(f"✅ {svc_name} panel re-opened after reload")
                                        except:
                                            session.log(f"❌ {svc_name} button not found after reload. Stopping tab.")
                                            return
                                    except Exception as reload_err:
                                        session.log(f"❌ Page reload failed: {reload_err}. Stopping tab.")
                                        return
                                else:
                                    session.log(f"⚠️ No response, retrying... ({no_response_streak}/{MAX_NO_RESPONSE})")
                                break

                    time.sleep(3)

                if multi:
                    session.log("🛑 Tab finished.")

            finally:
                browser.close()
                with session.count_lock:
                    session.active_tabs -= 1
                    remaining = session.active_tabs
                if remaining <= 0 and session.status == "running":
                    session.log("🛑 All tabs finished.")
                    session.status = "stopped"

    except Exception as e:
        session.log(f"❌ Fatal error: {e}")
        import traceback
        traceback.print_exc()
        with session.count_lock:
            session.active_tabs = max(0, session.active_tabs - 1)
            if session.active_tabs <= 0:
                session.status = "error"


# ═══════════════════════════════════════════════════════════════
#  ROUTES
# ═══════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return render_template("index.html")


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
        return jsonify([s.to_dict() for s in sessions.values()])


@app.route("/start", methods=["POST"])
def start():
    data = request.get_json()
    url = data.get("url", "").strip()
    service = data.get("service", "views").strip().lower()
    tabs = int(data.get("tabs", 1))
    if not url:
        return jsonify({"error": "No URL provided"}), 400
    if service not in SERVICES:
        return jsonify({"error": f"Unknown service: {service}"}), 400

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
