from flask import Flask, render_template, request, jsonify, Response
from playwright.sync_api import sync_playwright
import threading, time, re, sys, difflib, json, base64
from PIL import Image, ImageOps
from io import BytesIO
from collections import Counter, deque
import numpy as np

app = Flask(__name__)
ZEFOY = "https://zefoy.com"

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
    img = Image.open(BytesIO(img_bytes))
    if img.mode != 'RGB':
        img = img.convert('RGB')
    gray = ImageOps.grayscale(img)
    w, h = gray.size
    big = gray.resize((w * 3, h * 3), Image.LANCZOS)
    arr = np.array(big)

    results = []

    # Strategy 1: Direct threshold
    for thresh_val in [120, 140, 160, 180]:
        binary_img = Image.fromarray(((arr >= thresh_val) * 255).astype('uint8'))
        for psm in [7, 6]:
            config = f'--psm {psm} --oem 3 -c tessedit_char_whitelist=abcdefghijklmnopqrstuvwxyz'
            try:
                text = pytesseract.image_to_string(binary_img, config=config).strip()
                text = re.sub(r'[^a-z]', '', text.lower())
                if len(text) >= 2:
                    results.append(text)
            except:
                pass

    # Strategy 2: Dot removal
    for thresh_val in [130, 150]:
        binary = (arr < thresh_val).astype(np.uint8)
        cleaned = remove_small_components(binary, min_size=30)
        clean_img = Image.fromarray(((1 - cleaned) * 255).astype('uint8'))
        for psm in [7, 6]:
            config = f'--psm {psm} --oem 3 -c tessedit_char_whitelist=abcdefghijklmnopqrstuvwxyz'
            try:
                text = pytesseract.image_to_string(clean_img, config=config).strip()
                text = re.sub(r'[^a-z]', '', text.lower())
                if len(text) >= 2:
                    results.append(text)
            except:
                pass

    print(f"[BOT] OCR candidates: {results}", flush=True)
    if not results:
        return ""

    most_common = Counter(results).most_common(1)[0][0]

    if WORD_LIST:
        matches = difflib.get_close_matches(most_common, WORD_LIST, n=3, cutoff=0.5)
        print(f"[BOT] OCR: '{most_common}' → dictionary: {matches}", flush=True)
        if matches:
            return matches[0]

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

    def __init__(self, video_url, service="views"):
        with Session._lock:
            Session._counter += 1
            self.id = Session._counter
        self.video_url = video_url
        self.service = service  # key into SERVICES dict
        self.status = "starting"
        self.total_count = 0
        self.cycles = 0
        self.logs = []       # List of log message strings
        self.countdown = ""  # Current countdown text (updates in-place on frontend)
        self.stop_event = threading.Event()
        self.thread = None

    @property
    def svc(self):
        return SERVICES.get(self.service, SERVICES["views"])

    def log(self, msg):
        self.logs.append(msg)
        self.countdown = ""
        print(f"[S{self.id}] {msg}", flush=True)

    def set_countdown(self, text):
        self.countdown = text

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
        }


sessions = {}
sessions_lock = threading.Lock()


# ═══════════════════════════════════════════════════════════════
#  BOT LOOP (runs in background thread per session)
# ═══════════════════════════════════════════════════════════════

def run_session(session):
    svc = session.svc
    svc_name = svc["name"]
    btn_cls = svc["button_class"]
    menu_cls = svc["menu_class"]
    unit = svc["unit"]
    emoji = svc["emoji"]

    try:
        with sync_playwright() as p:
            session.log(f"🚀 Launching browser ({svc_name} mode)...")
            session.status = "running"

            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
            )
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

            for page_attempt in range(5):
                if session.stop_event.is_set():
                    break

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
                session.log(f"⚠️ Page not ready, reloading (attempt {page_attempt + 1}/5)...")
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
                session.status = "error"
                browser.close()
                return

            page.locator(f".{btn_cls}").click()
            time.sleep(2)
            session.log(f"✅ {svc_name} panel opened!")

            # ── Main loop ──
            zero_streak = 0  # consecutive cycles returning 0
            while not session.stop_event.is_set():
                session.cycles += 1
                cycle = session.cycles
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
                            const match = body.match(/[Ss]uccessfully\\s+(\\d+)/);
                            return {type: 'success', count: match ? parseInt(match[1]) : 0};
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
                        session.total_count += count
                        if count > 0:
                            zero_streak = 0
                            session.log(f"🎉 +{count} {unit}! Total: {session.total_count:,}")
                        else:
                            zero_streak += 1
                            session.log(f"⚠️ Zefoy returned 0 {unit} (streak: {zero_streak}) — retrying...")
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
                                    match = re.search(r'[Ss]uccessfully\s+(\d+)', body)
                                    count = int(match.group(1)) if match else 0
                                    session.total_count += count
                                    break
                            except:
                                pass
                            time.sleep(1)

                        if count > 0:
                            zero_streak = 0
                            session.log(f"🎉 +{count} {unit}! Total: {session.total_count:,}")
                        else:
                            zero_streak += 1
                            session.log(f"⚠️ Zefoy returned 0 {unit} (streak: {zero_streak}) — retrying...")
                        break

                    elif state_type == 'loading':
                        time.sleep(1)
                        continue

                    else:  # waiting
                        if check_round < 30:
                            time.sleep(1)
                            continue
                        else:
                            session.log("⚠️ No response, retrying...")
                            break

                time.sleep(3)

            session.log("🛑 Session stopped.")
            session.status = "stopped"
            browser.close()

    except Exception as e:
        session.log(f"❌ Fatal error: {e}")
        import traceback
        traceback.print_exc()
        session.status = "error"


# ═══════════════════════════════════════════════════════════════
#  ROUTES
# ═══════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/sessions")
def list_sessions():
    with sessions_lock:
        return jsonify([s.to_dict() for s in sessions.values()])


@app.route("/start", methods=["POST"])
def start():
    data = request.get_json()
    url = data.get("url", "").strip()
    service = data.get("service", "views").strip().lower()
    if not url:
        return jsonify({"error": "No URL provided"}), 400
    if service not in SERVICES:
        return jsonify({"error": f"Unknown service: {service}"}), 400

    session = Session(url, service=service)
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


@app.route("/stream/<int:sid>")
def stream(sid):
    with sessions_lock:
        session = sessions.get(sid)
    if not session:
        return jsonify({"error": "Not found"}), 404

    def generate():
        last_idx = 0
        last_countdown = ""
        while True:
            # New log lines
            current_len = len(session.logs)
            while last_idx < current_len:
                data = json.dumps({"type": "log", "text": session.logs[last_idx]})
                yield f"data: {data}\n\n"
                last_idx += 1

            # Countdown update (replaces in-place on frontend)
            cd = session.countdown
            if cd != last_countdown:
                last_countdown = cd
                data = json.dumps({"type": "countdown", "text": cd})
                yield f"data: {data}\n\n"

            # Stats update
            data = json.dumps({
                "type": "stats",
                "count": session.total_count,
                "unit": session.svc["unit"],
                "cycles": session.cycles,
                "status": session.status,
            })
            yield f"data: {data}\n\n"

            if session.status in ("stopped", "error"):
                data = json.dumps({"type": "ended", "status": session.status})
                yield f"data: {data}\n\n"
                break

            time.sleep(0.5)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=8080)
