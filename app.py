from flask import Flask, render_template, request, jsonify, Response, stream_with_context
from playwright.sync_api import sync_playwright
import queue, threading, time, re, sys
from PIL import Image, ImageFilter, ImageOps
import pytesseract
from io import BytesIO

app = Flask(__name__)
ZEFOY = "https://zefoy.com"

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

            def solve_captcha(img_bytes):
                """Try multiple OCR strategies on the captcha image."""
                img = Image.open(BytesIO(img_bytes))

                # Strategy 1: Grayscale + high contrast + threshold
                gray = ImageOps.grayscale(img)
                # Upscale 3x for better OCR
                big = gray.resize((gray.width * 3, gray.height * 3), Image.LANCZOS)
                # Sharpen
                sharp = big.filter(ImageFilter.SHARPEN)
                # Threshold: make it pure black text on white
                thresh = sharp.point(lambda px: 255 if px > 140 else 0)

                results = []
                # Try different PSM modes
                for psm in [7, 8, 13, 6]:
                    config = f'--psm {psm} --oem 3 -c tessedit_char_whitelist=abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'
                    try:
                        text = pytesseract.image_to_string(thresh, config=config).strip()
                        text = re.sub(r'[^A-Za-z0-9]', '', text).lower()
                        if len(text) >= 2:
                            results.append(text)
                    except:
                        pass

                # Strategy 2: Invert and retry
                inverted = ImageOps.invert(thresh)
                for psm in [7, 8]:
                    config = f'--psm {psm} --oem 3 -c tessedit_char_whitelist=abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'
                    try:
                        text = pytesseract.image_to_string(inverted, config=config).strip()
                        text = re.sub(r'[^A-Za-z0-9]', '', text).lower()
                        if len(text) >= 2:
                            results.append(text)
                    except:
                        pass

                # Strategy 3: Raw image
                try:
                    text = pytesseract.image_to_string(img, config='--psm 7').strip()
                    text = re.sub(r'[^A-Za-z0-9]', '', text).lower()
                    if len(text) >= 2:
                        results.append(text)
                except:
                    pass

                log(f"OCR candidates: {results}")
                # Pick the most common result, or the first one
                if not results:
                    return ""
                from collections import Counter
                most_common = Counter(results).most_common(1)[0][0]
                return most_common

            for captcha_attempt in range(5):
                try:
                    captcha_img = page.locator("img[src*='_CAPTCHA']")
                    if captcha_img.count() == 0:
                        log("No captcha found, proceeding...")
                        break

                    emit(q, 2, f"Solving captcha (attempt {captcha_attempt + 1})...")
                    log(f"Captcha attempt {captcha_attempt + 1}...")
                    time.sleep(1)

                    # Screenshot the captcha image
                    captcha_bytes = captcha_img.first.screenshot()
                    captcha_text = solve_captcha(captcha_bytes)
                    log(f"Captcha OCR result: '{captcha_text}'")

                    if not captcha_text:
                        log("OCR returned empty, refreshing captcha...")
                        # Click the refresh button to get a new captcha
                        try:
                            page.locator(".wrapper-capth button, .wrapper-capth .fa-refresh, .wrapper-capth .fa-sync, [onclick*='captcha']").first.click()
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
                        log("Captcha was wrong, page still shows captcha. Retrying...")
                        emit(q, 2, f"Wrong answer, retrying...")
                        time.sleep(2)
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

            # ── 5. MAIN LOOP ──
            for cycle in range(1, 51):
                log(f"\n{'='*30} CYCLE {cycle} {'='*30}")

                # ── Fill URL (scoped to Views panel only) ──
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

                # ── Handle rate limit ──
                for timer_try in range(3):
                    try:
                        countdown = page.locator("#login-countdown")
                        if countdown.count() > 0 and countdown.is_visible():
                            timer_text = countdown.inner_text()
                            if "wait" in timer_text.lower() or "minute" in timer_text.lower():
                                wait_secs = parse_wait_time(timer_text) + 5
                                emit(q, 6, f"Cycle {cycle}: Rate limited — waiting {wait_secs}s...")
                                log(f"Rate limit: '{timer_text}' → waiting {wait_secs}s")
                                time.sleep(wait_secs)

                                # Re-fill and search again
                                url_input.fill("")
                                time.sleep(0.3)
                                url_input.fill(tiktok_url)
                                time.sleep(1)
                                page.locator(".t-views-menu button[type='submit']").first.click()
                                time.sleep(3)
                                continue
                    except Exception as e:
                        log(f"Timer check error: {e}")
                    break

                # ── Find and click the video bar ──
                emit(q, 7, f"Cycle {cycle}: Looking for video bar...")
                log("Scanning for video bar...")

                bar_clicked = False
                for attempt in range(30):
                    try:
                        coords = page.evaluate("""(formAction) => {
                            // Strategy 1: clickable element inside the result container
                            if (formAction) {
                                const container = document.getElementById(formAction);
                                if (container) {
                                    const el = container.querySelector('a, button');
                                    if (el) {
                                        const r = el.getBoundingClientRect();
                                        if (r.width > 0 && r.height > 0) {
                                            return {x: r.x + r.width/2, y: r.y + r.height/2, m: 'container'};
                                        }
                                    }
                                    // Also check for any div with digits (the bar itself)
                                    const divs = container.querySelectorAll('div, span');
                                    for (const d of divs) {
                                        const text = d.innerText?.trim();
                                        if (text && /\\d/.test(text) && text.length < 60 &&
                                            !text.includes('wait') && !text.includes('minute') &&
                                            !text.includes('second') && !text.includes('Please')) {
                                            const r = d.getBoundingClientRect();
                                            if (r.width > 50 && r.height > 10) {
                                                return {x: r.x + r.width/2, y: r.y + r.height/2, m: 'container-div'};
                                            }
                                        }
                                    }
                                }
                            }

                            // Strategy 2: scan below input for elements with digits
                            const input = document.querySelector('input[placeholder="Enter Video URL"]');
                            if (!input) return null;
                            const inputRect = input.getBoundingClientRect();
                            const startY = inputRect.bottom + 10;

                            for (let y = startY; y < startY + 300; y += 5) {
                                for (let x = 50; x < window.innerWidth - 50; x += 10) {
                                    const el = document.elementFromPoint(x, y);
                                    if (el && el.innerText) {
                                        const text = el.innerText.trim();
                                        if (/\\d/.test(text) && text.length < 60 &&
                                            !text.includes('Enter') && !text.includes('Search') &&
                                            !text.includes('wait') && !text.includes('minute') &&
                                            !text.includes('second') && !text.includes('Please') &&
                                            !text.includes('Join') && !text.includes('YouTube')) {
                                            const r = el.getBoundingClientRect();
                                            if (r.width > 50 && r.height > 10) {
                                                return {x: r.x + r.width/2, y: r.y + r.height/2, m: 'scan'};
                                            }
                                        }
                                    }
                                }
                            }
                            return null;
                        }""", form_action)

                        if coords:
                            log(f"Bar found via '{coords['m']}' at ({coords['x']:.0f}, {coords['y']:.0f})")
                            page.mouse.click(coords['x'], coords['y'])
                            bar_clicked = True
                            break
                    except Exception as e:
                        if attempt == 0:
                            log(f"Bar scan error: {e}")
                    time.sleep(1)

                if not bar_clicked:
                    emit(q, 7, f"Cycle {cycle}: No bar found, skipping...")
                    log("Bar not found after 30 attempts, skipping cycle")
                    time.sleep(3)
                    continue

                # ── Wait for success ──
                emit(q, 8, f"Cycle {cycle}: Processing...")
                log("Waiting for success message...")

                got_success = False
                for _ in range(60):
                    try:
                        body = page.inner_text("body")
                        lower = body.lower()
                        if "successfully" in lower:
                            match = re.search(r'[Ss]uccessfully\s+(\d+)', body)
                            views = int(match.group(1)) if match else 0
                            total_views += views
                            emit(q, 8, f"✅ Cycle {cycle}: +{views} views (Total: {total_views})")
                            log(f"SUCCESS! +{views} views | Total: {total_views}")
                            got_success = True
                            break
                        elif "error" in lower and "captcha" not in lower:
                            log(f"Error detected in body text")
                    except:
                        pass
                    time.sleep(1)

                if not got_success:
                    log("No success message after 60s")
                    emit(q, 8, f"Cycle {cycle}: Timeout waiting for result")

                time.sleep(3)

            # ── DONE ──
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
