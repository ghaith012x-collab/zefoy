from flask import Flask, render_template, request, jsonify, Response, stream_with_context
from playwright.sync_api import sync_playwright
import queue, threading, time, re, sys, difflib
from PIL import Image, ImageFilter, ImageOps
import pytesseract
from io import BytesIO
from collections import Counter, deque
import numpy as np

app = Flask(__name__)
ZEFOY = "https://zefoy.com"

# ─── Load word dictionary once at startup ───
WORD_LIST = []
def load_dictionary():
    global WORD_LIST
    try:
        import urllib.request
        url = "https://raw.githubusercontent.com/dwyl/english-words/master/words_alpha.txt"
        data = urllib.request.urlopen(url, timeout=10).read().decode()
        WORD_LIST = [w.strip().lower() for w in data.splitlines() if 2 <= len(w.strip()) <= 10]
        print(f"[BOT] Dictionary loaded: {len(WORD_LIST)} words", flush=True)
    except Exception as e:
        print(f"[BOT] Dictionary load failed: {e}", flush=True)
        # Fallback: use /usr/share/dict/words if available
        try:
            with open('/usr/share/dict/words') as f:
                WORD_LIST = [w.strip().lower() for w in f if 2 <= len(w.strip()) <= 10]
            print(f"[BOT] Fallback dictionary: {len(WORD_LIST)} words", flush=True)
        except:
            WORD_LIST = []

# Load on import in background
threading.Thread(target=load_dictionary, daemon=True).start()


def log(msg):
    print(f"[BOT] {msg}", flush=True)

def emit(q, step, msg):
    q.put(f"{step}|{msg}")

def parse_wait_time(text):
    """Parse 'Please wait X minute(s) Y second(s)' into seconds."""
    mins = re.search(r'(\d+)\s*minute', text)
    secs = re.search(r'(\d+)\s*second', text)
    total = 0
    if mins:
        total += int(mins.group(1)) * 60
    if secs:
        total += int(secs.group(1))
    return total


def remove_small_components(binary_arr, min_size=12):
    """Remove connected components smaller than min_size using BFS."""
    h, w = binary_arr.shape
    visited = np.zeros((h, w), dtype=bool)
    result = np.zeros((h, w), dtype=np.uint8)

    for y in range(h):
        for x in range(w):
            if binary_arr[y, x] == 1 and not visited[y, x]:
                # BFS flood fill
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
    """Solve captcha: upscale first, then OCR with and without dot removal."""
    img = Image.open(BytesIO(img_bytes))
    if img.mode != 'RGB':
        img = img.convert('RGB')
    gray = ImageOps.grayscale(img)

    # Upscale 3x FIRST for better OCR accuracy
    w, h = gray.size
    big = gray.resize((w * 3, h * 3), Image.LANCZOS)
    arr = np.array(big)

    results = []

    # ── Strategy 1: Direct threshold (no dot removal) ──
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

    # ── Strategy 2: With dot removal ──
    for thresh_val in [130, 150]:
        binary = (arr < thresh_val).astype(np.uint8)
        cleaned = remove_small_components(binary, min_size=30)  # bigger min_size for 3x image
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

    log(f"OCR candidates: {results}")

    if not results:
        return ""

    # Most common OCR result
    most_common = Counter(results).most_common(1)[0][0]

    # Dictionary correction
    if WORD_LIST:
        matches = difflib.get_close_matches(most_common, WORD_LIST, n=3, cutoff=0.5)
        log(f"OCR most common: '{most_common}' → dictionary matches: {matches}")
        if matches:
            return matches[0]

    return most_common


def run_bot(tiktok_url, q):
    with sync_playwright() as p:
        total_views = 0
        try:
            # ── 1. LAUNCH BROWSER ──
            emit(q, 1, "Launching browser...")
            log("Launching browser...")
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"]
            )
            page = browser.new_page()

            # *** KEY FIX: auto-dismiss the alert dialog that blocks everything ***
            page.on("dialog", lambda d: d.accept())

            # ── 2. LOAD ZEFOY.COM ──
            emit(q, 1, "Loading zefoy.com...")
            log("Navigating to zefoy.com...")
            page.goto(ZEFOY, wait_until="domcontentloaded", timeout=60000)
            time.sleep(5)
            log(f"Page loaded: title='{page.title()}' url={page.url}")

            # ── 3. HANDLE CAPTCHA (with retries) ──
            emit(q, 2, "Checking for captcha...")
            log("Checking for captcha...")

            for captcha_attempt in range(8):
                try:
                    captcha_img = page.locator("#captcha-img, img[src*='_CAPTCHA']")
                    if captcha_img.count() == 0:
                        log("No captcha found, proceeding...")
                        break

                    emit(q, 2, f"Solving captcha (attempt {captcha_attempt + 1})...")
                    log(f"Captcha attempt {captcha_attempt + 1}...")
                    time.sleep(2)

                    # Screenshot the captcha image element directly
                    captcha_bytes = captcha_img.first.screenshot()
                    captcha_text = solve_captcha(captcha_bytes)
                    log(f"Captcha answer: '{captcha_text}'")

                    if not captcha_text:
                        log("OCR returned empty, refreshing captcha...")
                        try:
                            page.locator(".refresh-capthca-btn-new").click()
                        except:
                            page.reload(wait_until="domcontentloaded")
                        time.sleep(3)
                        continue

                    # Fill and submit
                    page.locator("#captchatoken").fill(captcha_text)
                    time.sleep(0.5)
                    page.locator(".submit-captcha").click()
                    time.sleep(5)

                    # Check if captcha was accepted (Views button should appear)
                    try:
                        page.locator(".t-views-button").wait_for(timeout=5000)
                        log("Captcha solved! Views button appeared.")
                        emit(q, 2, "Captcha solved ✅")
                        break
                    except:
                        log("Captcha was wrong, refreshing for retry...")
                        emit(q, 2, f"Wrong answer '{captcha_text}', retrying...")
                        # Dismiss error modal if present
                        try:
                            page.locator(".modal .btn-secondary, .modal .close").first.click()
                            time.sleep(1)
                        except:
                            pass
                        # Refresh captcha for new image
                        try:
                            page.locator(".refresh-capthca-btn-new").click()
                        except:
                            pass
                        time.sleep(3)
                        continue

                except Exception as e:
                    log(f"Captcha attempt error: {e}")
                    time.sleep(2)

            # ── 4. FIND AND CLICK VIEWS BUTTON ──
            emit(q, 3, "Looking for Views button...")
            log("Waiting for .t-views-button...")
            try:
                page.locator(".t-views-button").wait_for(timeout=30000)
                log("Views button found!")
            except Exception:
                log("Views button NOT found after 30s")
                emit(q, 0, "Error: Views button not found. Captcha may need manual solving.")
                browser.close()
                q.put("DONE")
                return

            emit(q, 4, "Opening Views panel...")
            page.locator(".t-views-button").click()
            time.sleep(2)
            log("Views panel opened")

            # Discover the result container ID from the form action
            form_action = page.evaluate("""() => {
                const form = document.querySelector('.t-views-menu form');
                return form ? form.getAttribute('action') : '';
            }""")
            log(f"Result container ID: '{form_action}'")

            # ── 5. MAIN LOOP (infinite until stopped) ──
            cycle = 0
            while True:
                cycle += 1
                log(f"\n{'='*30} CYCLE {cycle} {'='*30}")

                # ── Fill URL ──
                emit(q, 5, f"Cycle {cycle}: Entering URL...")
                url_input = page.locator(".t-views-menu input[placeholder='Enter Video URL']")
                url_input.fill("")
                time.sleep(0.3)
                url_input.fill(tiktok_url)
                time.sleep(1)
                log("URL filled")

                # ── Click Search ──
                emit(q, 6, f"Cycle {cycle}: Searching...")
                log("Clicking Search...")
                page.locator(".t-views-menu button[type='submit']").first.click()
                time.sleep(3)

                # ── Analyze page: rate limit, spinner, video bar, or success ──
                action_taken = False
                for check_round in range(120):  # up to ~2 mins of checking
                    page_state = page.evaluate("""() => {
                        const body = document.body.innerText || '';
                        const lower = body.toLowerCase();

                        // Check for rate limit countdown
                        const countdown = document.getElementById('login-countdown');
                        if (countdown && countdown.offsetParent !== null) {
                            const text = countdown.innerText || '';
                            if (text && (text.toLowerCase().includes('wait') ||
                                text.toLowerCase().includes('minute') ||
                                text.toLowerCase().includes('second'))) {
                                return {type: 'ratelimit', text: text};
                            }
                        }

                        // Check for success message
                        if (lower.includes('successfully')) {
                            const match = body.match(/[Ss]uccessfully\\s+(\\d+)/);
                            return {type: 'success', views: match ? parseInt(match[1]) : 0};
                        }

                        // Check for loading spinner (fa-spinner, loading class, etc.)
                        const spinners = document.querySelectorAll('.fa-spinner, .fa-spin, .spinner, [class*="loading"], [class*="spin"]');
                        for (const s of spinners) {
                            if (s.offsetParent !== null) return {type: 'loading'};
                        }

                        // Check for video bar (clickable result)
                        const viewsMenu = document.querySelector('.t-views-menu');
                        if (viewsMenu) {
                            // Look for the result container with a send button
                            const forms = viewsMenu.querySelectorAll('form');
                            for (const form of forms) {
                                const action = form.getAttribute('action');
                                if (action) {
                                    const container = document.getElementById(action);
                                    if (container && container.offsetParent !== null) {
                                        const btn = container.querySelector('a, button, [onclick]');
                                        if (btn && btn.offsetParent !== null) {
                                            const r = btn.getBoundingClientRect();
                                            if (r.width > 0 && r.height > 0) {
                                                return {type: 'bar', x: r.x + r.width/2, y: r.y + r.height/2};
                                            }
                                        }
                                        // Any clickable div with digits
                                        const divs = container.querySelectorAll('div, span');
                                        for (const d of divs) {
                                            const t = d.innerText?.trim();
                                            if (t && /\\d/.test(t) && t.length < 60 &&
                                                !t.includes('wait') && !t.includes('minute') &&
                                                !t.includes('second') && !t.includes('Please')) {
                                                const r = d.getBoundingClientRect();
                                                if (r.width > 50 && r.height > 10) {
                                                    return {type: 'bar', x: r.x + r.width/2, y: r.y + r.height/2};
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        }

                        // Check for any visible timer text in the page
                        if (lower.includes('please wait') || lower.includes('minute') && lower.includes('second')) {
                            return {type: 'ratelimit', text: body.substring(0, 500)};
                        }

                        return {type: 'waiting'};
                    }""")

                    state_type = page_state.get('type', 'waiting') if page_state else 'waiting'
                    log(f"Page state: {state_type}")

                    if state_type == 'ratelimit':
                        timer_text = page_state.get('text', '')
                        wait_secs = parse_wait_time(timer_text)
                        if wait_secs <= 0:
                            wait_secs = 60  # fallback
                        wait_secs += 5  # buffer
                        log(f"Rate limit: '{timer_text}' → waiting {wait_secs}s")

                        # ── LIVE COUNTDOWN ──
                        for remaining in range(wait_secs, 0, -1):
                            mins = remaining // 60
                            secs = remaining % 60
                            time_str = f"{mins}m {secs:02d}s" if mins > 0 else f"{secs}s"
                            emit(q, 6, f"⏳ Cycle {cycle}: Rate limited — {time_str} remaining")
                            time.sleep(1)

                        emit(q, 6, f"Cycle {cycle}: Rate limit done, retrying...")
                        # Re-fill and search
                        url_input.fill("")
                        time.sleep(0.3)
                        url_input.fill(tiktok_url)
                        time.sleep(1)
                        page.locator(".t-views-menu button[type='submit']").first.click()
                        time.sleep(3)
                        continue  # re-check page state

                    elif state_type == 'success':
                        views = page_state.get('views', 0)
                        total_views += views
                        emit(q, 8, f"✅ Cycle {cycle}: +{views} views (Total: {total_views})")
                        log(f"SUCCESS! +{views} views | Total: {total_views}")
                        action_taken = True
                        break

                    elif state_type == 'bar':
                        x, y = page_state['x'], page_state['y']
                        log(f"Video bar found at ({x:.0f}, {y:.0f}), clicking...")
                        emit(q, 7, f"Cycle {cycle}: Sending views...")
                        page.mouse.click(x, y)
                        time.sleep(2)

                        # Wait for success after clicking bar
                        for _ in range(30):
                            try:
                                body = page.inner_text("body")
                                if "successfully" in body.lower():
                                    match = re.search(r'[Ss]uccessfully\s+(\d+)', body)
                                    views = int(match.group(1)) if match else 0
                                    total_views += views
                                    emit(q, 8, f"✅ Cycle {cycle}: +{views} views (Total: {total_views})")
                                    log(f"SUCCESS! +{views} views | Total: {total_views}")
                                    action_taken = True
                                    break
                            except:
                                pass
                            time.sleep(1)
                        break

                    elif state_type == 'loading':
                        emit(q, 7, f"Cycle {cycle}: Loading...")
                        time.sleep(1)
                        continue  # keep checking

                    else:  # waiting
                        if check_round < 10:
                            time.sleep(1)
                            continue
                        elif check_round < 30:
                            # Scan more aggressively for bar elements
                            emit(q, 7, f"Cycle {cycle}: Scanning page...")
                            time.sleep(1)
                            continue
                        else:
                            emit(q, 7, f"Cycle {cycle}: No response, retrying...")
                            log("No page response after extended wait")
                            break

                if not action_taken:
                    log(f"Cycle {cycle}: No success, moving to next cycle")

                time.sleep(3)

            # ── DONE (only if loop breaks, which it shouldn't) ──
            emit(q, 9, f"🎉 Finished! Total views sent: {total_views}")
            log(f"All done. Total views: {total_views}")
            browser.close()

        except Exception as e:
            log(f"FATAL ERROR: {e}")
            import traceback
            traceback.print_exc()
            emit(q, 0, f"Error: {str(e)}")

        q.put("DONE")


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/start", methods=["POST"])
def start():
    data = request.get_json()
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    q = queue.Queue()
    t = threading.Thread(target=run_bot, args=(url, q), daemon=True)
    t.start()

    def generate():
        while True:
            try:
                msg = q.get(timeout=600)
                if msg == "DONE":
                    yield "data: DONE\n\n"
                    break
                yield f"data: {msg}\n\n"
            except queue.Empty:
                yield "data: 0|Timeout — no response from bot\n\n"
                break

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=8080)
