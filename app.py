from flask import Flask, render_template, request, jsonify, Response, stream_with_context
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
import os, time, json, queue, threading, re

app = Flask(__name__)
ZEFOY = "https://zefoy.com"

live_queues = {}

def emit(q, step, message, done=False, inspect=None):
    payload = {"step": step, "message": message, "done": done}
    if inspect:
        payload["inspect"] = inspect
    q.put(json.dumps(payload) + "\n")

def parse_timer_seconds(text):
    """Extract wait seconds from timer text like 'Please wait 2 minutes 30 seconds'."""
    minutes = 0
    seconds = 0
    m = re.search(r'(\d+)\s*minute', text, re.IGNORECASE)
    if m:
        minutes = int(m.group(1))
    s = re.search(r'(\d+)\s*second', text, re.IGNORECASE)
    if s:
        seconds = int(s.group(1))
    total = minutes * 60 + seconds
    return total if total > 0 else 60  # default 60s if can't parse

def run_bot(tiktok_url, q):
    with sync_playwright() as p:
        emit(q, 1, "Launching browser...")
        try:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-setuid-sandbox",
                    "--single-process",
                ]
            )
        except Exception as e:
            emit(q, 99, f"Browser failed to launch: {str(e)}", done=True)
            return

        page = browser.new_page(
            viewport={"width": 390, "height": 844},
            user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
        )
        # Hide webdriver flag
        page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

        try:
            # ── STEP 1: Load zefoy.com ──
            emit(q, 1, "Loading zefoy.com...")
            loaded = False
            for attempt in range(3):
                try:
                    page.goto(ZEFOY, wait_until="commit", timeout=30000)
                    loaded = True
                    emit(q, 1, f"Navigation committed (attempt {attempt+1}), waiting for page...")
                    break
                except PlaywrightTimeout:
                    emit(q, 1, f"Attempt {attempt+1} timed out, retrying...")
                    time.sleep(2)
                except Exception as e:
                    emit(q, 1, f"Attempt {attempt+1} error: {str(e)[:80]}, retrying...")
                    time.sleep(2)

            if not loaded:
                emit(q, 99, "Failed to load zefoy.com after 3 attempts", done=True)
                browser.close()
                return

            # Give page time to render
            time.sleep(8)

            # Log what we got for debugging
            try:
                title = page.title()
                url = page.url
                emit(q, 1, f"Page ready: '{title}' @ {url}")
            except:
                emit(q, 1, "Page loaded (couldn't read title)")

            # ── STEP 2: Captcha check ──
            emit(q, 2, "Checking for captcha...")
            captcha_present = False
            try:
                captcha_img = page.locator("img[src*='captcha'], .captcha img, [alt*='captcha']")
                captcha_img.wait_for(timeout=5000)
                captcha_present = True
                emit(q, 3, "Captcha found — solving...")
            except PlaywrightTimeout:
                emit(q, 3, "No captcha detected — proceeding...")

            if captcha_present:
                from PIL import Image
                import pytesseract, io
                img_bytes = captcha_img.screenshot()
                img = Image.open(io.BytesIO(img_bytes))
                img = img.convert('L').resize((img.width * 3, img.height * 3))
                captcha_text = pytesseract.image_to_string(
                    img,
                    config='--psm 8 -c tessedit_char_whitelist=abcdefghijklmnopqrstuvwxyz0123456789'
                ).strip().lower()
                emit(q, 4, f"Captcha solved: '{captcha_text}'")

                for sel in ["input[placeholder*='Enter the word']", "input[placeholder*='word']", "input[type='text']", "input"]:
                    try:
                        inp = page.locator(sel).first
                        if inp.is_visible(timeout=2000):
                            inp.fill(captcha_text)
                            emit(q, 5, "Captcha entered")
                            break
                    except:
                        continue

                for sel in ["button[type='submit']", "button:has-text('✓')", "button"]:
                    try:
                        btn = page.locator(sel).first
                        if btn.is_visible(timeout=2000):
                            btn.click()
                            emit(q, 6, "Captcha submitted")
                            break
                    except:
                        continue
                time.sleep(4)

            # ── STEP 7: Click the Views arrow button ──
            emit(q, 7, "Clicking Views arrow button...")
            views_clicked = False

            # The arrow buttons are <a> tags inside each service row.
            # Strategy: find the card/row containing "Views" text, then click the arrow button inside it.
            try:
                # Look for all the service rows and find the one with "Views"
                rows = page.locator(".row, .col, .card, div").all()
                for row in rows:
                    try:
                        txt = row.inner_text(timeout=1000)
                        if "Views" in txt and "Comment" not in txt:
                            # Find the arrow button inside
                            arrow = row.locator("a, button").first
                            if arrow.is_visible(timeout=2000):
                                arrow.click()
                                views_clicked = True
                                emit(q, 8, "Clicked Views arrow")
                                break
                    except:
                        continue
            except Exception as e:
                emit(q, 7, f"Row scan failed: {str(e)[:60]}")

            # Fallback: use coordinate approach
            if not views_clicked:
                try:
                    views_el = page.locator("text=Views").first
                    box = views_el.bounding_box()
                    if box:
                        # Click the arrow button below the "Views" text
                        x = box["x"] + box["width"] / 2
                        y = box["y"] + box["height"] + 30
                        page.mouse.click(x, y)
                        views_clicked = True
                        emit(q, 8, "Clicked Views arrow (coordinate)")
                except Exception as e:
                    emit(q, 7, f"Coordinate click failed: {str(e)[:60]}")

            if not views_clicked:
                emit(q, 99, "Failed to click Views", done=True)
                browser.close()
                return

            time.sleep(3)

            # ── MAIN LOOP ──
            total_views_sent = 0
            max_cycles = 50
            for cycle in range(1, max_cycles + 1):

                # ── STEP 9: Ensure URL is filled ──
                emit(q, 9, f"Cycle {cycle}: Filling URL...")
                url_filled = False
                for sel in ["input[placeholder*='Enter Video URL']", "input[placeholder*='Video URL']", "input[placeholder*='URL']", "input[type='text']"]:
                    try:
                        inp = page.locator(sel).first
                        if inp.is_visible(timeout=5000):
                            inp.fill("")
                            time.sleep(0.3)
                            inp.fill(tiktok_url)
                            url_filled = True
                            emit(q, 10, f"Link pasted: {tiktok_url[:40]}...")
                            break
                    except:
                        continue

                if not url_filled:
                    emit(q, 99, "No URL input found", done=True)
                    browser.close()
                    return

                # ── STEP 11: Click Search ──
                emit(q, 11, f"Cycle {cycle}: Clicking Search...")
                search_clicked = False
                for sel in ["button:has-text('Search')", "input[type='submit']", "button.btn-primary"]:
                    try:
                        btn = page.locator(sel).first
                        if btn.is_visible(timeout=3000):
                            btn.click()
                            search_clicked = True
                            emit(q, 12, "Search clicked")
                            break
                    except:
                        continue

                if not search_clicked:
                    # Fallback: find any button with search-like text
                    try:
                        for b in page.locator("button").all():
                            if b.is_visible(timeout=1000):
                                txt = b.inner_text(timeout=500).strip()
                                if "search" in txt.lower() or "🔍" in txt:
                                    b.click()
                                    search_clicked = True
                                    emit(q, 12, "Search clicked (fallback)")
                                    break
                    except:
                        pass

                if not search_clicked:
                    emit(q, 99, "No Search button", done=True)
                    browser.close()
                    return

                time.sleep(5)

                # ── STEP 13: Check for rate limit / timer ──
                rate_limited = False
                try:
                    # Look for timer or "Too many" or "Please wait" text
                    body_text = page.locator("body").inner_text(timeout=3000)

                    if "Too many requests" in body_text or "Please wait" in body_text or "too many" in body_text.lower():
                        rate_limited = True
                        wait_secs = parse_timer_seconds(body_text)
                        # Cap at 10 minutes max
                        wait_secs = min(wait_secs, 600)
                        emit(q, 13, f"Rate limited — waiting {wait_secs}s...")
                        time.sleep(wait_secs + 5)  # add 5s buffer
                        continue
                except:
                    pass

                # ── STEP 14: Find and click the dark bar (video info bar) ──
                emit(q, 14, "Looking for video bar...")
                bar_clicked = False

                # The bar shows something like "🎬 354" - it's a dark element with digits
                # Strategy: find clickable elements below the input that contain digits
                try:
                    inp = page.locator("input[placeholder*='Enter Video URL'], input[placeholder*='Video URL'], input[placeholder*='URL'], input[type='text']").first
                    inp_box = inp.bounding_box()
                    input_bottom_y = inp_box["y"] + inp_box["height"] if inp_box else 200

                    # Look for elements below the input
                    candidates = page.locator("div, button, a, span").all()
                    for el in candidates:
                        try:
                            box = el.bounding_box()
                            if not box:
                                continue
                            # Must be below the input, within ~150px
                            dist_below = box["y"] - input_bottom_y
                            if dist_below < 5 or dist_below > 150:
                                continue
                            if box["height"] < 25 or box["width"] < 100:
                                continue
                            if not el.is_visible(timeout=500):
                                continue

                            txt = el.inner_text(timeout=500).strip()
                            if not txt:
                                continue
                            # Skip navigation/notice text
                            if any(skip in txt.lower() for skip in ["important", "notice", "official", "join", "youtube", "telegram", "home", "zefoy"]):
                                continue

                            # The bar typically has digits (view count)
                            has_digits = any(c.isdigit() for c in txt)
                            if has_digits:
                                el.click()
                                bar_clicked = True
                                emit(q, 15, f"Clicked bar: '{txt[:30]}'")
                                break
                        except:
                            continue
                except Exception as e:
                    emit(q, 14, f"Bar scan error: {str(e)[:60]}")

                # Fallback: click by coordinate below input
                if not bar_clicked:
                    try:
                        inp_box = inp.bounding_box() if inp else None
                        if inp_box:
                            check_x = int(inp_box["x"] + inp_box["width"] / 2)
                            check_y = int(inp_box["y"] + inp_box["height"] + 50)

                            el_info = page.evaluate("""(coords) => {
                                const el = document.elementFromPoint(coords.x, coords.y);
                                if (!el) return null;
                                const text = el.innerText ? el.innerText.trim().slice(0,50) : '';
                                if (text && /\d/.test(text)) return {text: text, tag: el.tagName};
                                // Check parent
                                const p = el.parentElement;
                                if (p) {
                                    const pt = p.innerText ? p.innerText.trim().slice(0,50) : '';
                                    if (pt && /\d/.test(pt)) return {text: pt, tag: p.tagName};
                                }
                                return null;
                            }""", {"x": check_x, "y": check_y})

                            if el_info:
                                page.mouse.click(check_x, check_y)
                                bar_clicked = True
                                emit(q, 15, f"Clicked bar (coord): '{el_info.get('text', '')[:20]}'")
                    except Exception as e:
                        emit(q, 14, f"Coord fallback failed: {str(e)[:60]}")

                if not bar_clicked:
                    # Maybe already processing or rate limited — check page state
                    try:
                        body_text = page.locator("body").inner_text(timeout=2000)
                        if "Successfully" in body_text:
                            match = re.search(r'Successfully\s+(\d+)\s+views?\s+sent', body_text, re.IGNORECASE)
                            count = match.group(1) if match else "?"
                            total_views_sent += int(count) if count.isdigit() else 0
                            emit(q, 17, f"✅ {count} views sent! (Total: {total_views_sent})")
                            time.sleep(3)
                            continue
                        if "Too many" in body_text or "Please wait" in body_text:
                            wait_secs = parse_timer_seconds(body_text)
                            wait_secs = min(wait_secs, 600)
                            emit(q, 13, f"Rate limited — waiting {wait_secs}s...")
                            time.sleep(wait_secs + 5)
                            continue
                    except:
                        pass

                    emit(q, 16, "No bar found — retrying cycle...")
                    time.sleep(5)
                    continue

                # ── STEP 16: Wait for result after clicking bar ──
                emit(q, 16, "Waiting for result...")

                # Poll for up to 60 seconds for a result
                result_found = False
                for _ in range(30):  # 30 x 2s = 60s max
                    time.sleep(2)
                    try:
                        body_text = page.locator("body").inner_text(timeout=3000)

                        # Check for success
                        if "Successfully" in body_text:
                            match = re.search(r'Successfully\s+(\d+)\s+views?\s+sent', body_text, re.IGNORECASE)
                            count = match.group(1) if match else "?"
                            total_views_sent += int(count) if count.isdigit() else 0
                            emit(q, 17, f"✅ {count} views sent! (Total: {total_views_sent})")
                            result_found = True
                            break

                        # Check for timer (still processing)
                        if "Checking Timer" in body_text:
                            emit(q, 16, "Checking Timer... please wait")
                            continue

                        # Check for rate limit
                        if "Too many requests" in body_text or "Please wait" in body_text:
                            wait_secs = parse_timer_seconds(body_text)
                            wait_secs = min(wait_secs, 600)
                            emit(q, 13, f"Rate limited — waiting {wait_secs}s...")
                            time.sleep(wait_secs + 5)
                            result_found = True  # break inner, continue outer
                            break
                    except:
                        continue

                    # Check for spinner
                    try:
                        spinner = page.locator(".spinner, [class*='spinner'], [class*='loading']").first
                        if spinner.is_visible(timeout=500):
                            continue  # still loading
                    except:
                        pass

                if not result_found:
                    emit(q, 16, "No result after 60s — continuing to next cycle...")

                # Brief pause before next cycle
                time.sleep(3)

            emit(q, 18, f"🏁 Finished {max_cycles} cycles. Total views sent: {total_views_sent}", done=True)

        except Exception as e:
            emit(q, 99, f"Error: {str(e)}", done=True)
        finally:
            browser.close()

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/stream', methods=['POST'])
def stream():
    url = request.json.get('url')
    if not url:
        return jsonify({"error": "No URL"}), 400

    req_id = str(time.time())
    q = queue.Queue()
    live_queues[req_id] = q

    thread = threading.Thread(target=run_bot, args=(url, q))
    thread.start()

    def generate():
        while True:
            try:
                msg = q.get(timeout=600)  # 10 min timeout (longer for rate limits)
                yield f"data: {msg}\n\n"
                data = json.loads(msg.strip())
                if data.get("done"):
                    break
            except queue.Empty:
                yield f"data: {json.dumps({'step': 99, 'message': 'Timed out', 'done': True})}\n\n"
                break
        live_queues.pop(req_id, None)

    return Response(stream_with_context(generate()), mimetype='text/event-stream')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))
