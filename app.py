from flask import Flask, render_template, request, jsonify, Response, stream_with_context
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
import os, time, json, queue, threading, re, sys

app = Flask(__name__)
ZEFOY = "https://zefoy.com"

live_queues = {}

def emit(q, step, message, done=False, inspect=None):
    payload = {"step": step, "message": message, "done": done}
    if inspect:
        payload["inspect"] = inspect
    q.put(json.dumps(payload) + "\n")

def log(msg):
    print(f"[BOT] {msg}", flush=True)
    sys.stdout.flush()

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
    return total if total > 0 else 60

def run_bot(tiktok_url, q):
    log(f"run_bot called with url={tiktok_url}")
    with sync_playwright() as p:
        # ── EXACT original browser setup ──
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            viewport={"width": 390, "height": 844},
            user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
        )

        try:
            # ── STEP 1: Load zefoy.com ──
            emit(q, 1, "Loading zefoy.com...")
            log("Loading zefoy.com...")
            page.goto(ZEFOY, wait_until="domcontentloaded", timeout=30000)
            log("DOM loaded, waiting for page JS to render...")
            # Wait for the actual Views element to appear (means JS has rendered)
            try:
                page.wait_for_selector("text=Views", timeout=20000)
                log("Views element found — page fully loaded")
            except PlaywrightTimeout:
                log("Views not found after 20s, waiting 5s fallback...")
                time.sleep(5)
            log(f"Page ready: title='{page.title()}' url={page.url}")
            emit(q, 1, "Page loaded!")

            # ── STEP 2: Captcha check (original logic) ──
            emit(q, 2, "Checking for captcha...")
            log("Checking for captcha...")
            captcha_present = False
            try:
                captcha_img = page.locator("img[src*='captcha'], .captcha img, [alt*='captcha']")
                captcha_img.wait_for(timeout=5000)
                captcha_present = True
                emit(q, 3, "Captcha found — solving...")
                log("Captcha found")
            except PlaywrightTimeout:
                emit(q, 3, "No captcha detected — proceeding...")
                log("No captcha")

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
                log(f"Captcha text: '{captcha_text}'")

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

            # ── STEP 7: Click Views arrow (original logic) ──
            emit(q, 7, "Clicking Views arrow button...")
            log("Clicking Views arrow...")
            views_clicked = False

            try:
                views_el = page.locator("text=Views").first
                box = views_el.bounding_box()
                if box:
                    x = box["x"] + box["width"] / 2
                    y = box["y"] + box["height"] + 30
                    page.mouse.click(x, y)
                    views_clicked = True
                    emit(q, 8, "Clicked Views arrow")
                    log("Views arrow clicked")
            except Exception as e:
                emit(q, 7, f"Failed: {str(e)[:60]}")
                log(f"Views click failed: {e}")

            if not views_clicked:
                emit(q, 99, "Failed to click Views", done=True)
                log("FAILED: Could not click Views")
                browser.close()
                return

            time.sleep(3)

            # ── STEP 9: Fill URL first time (original logic) ──
            emit(q, 9, "Looking for URL input...")
            log("Looking for URL input...")
            url_filled = False
            for sel in ["input[placeholder*='Enter Video URL']", "input[placeholder*='Video URL']", "input[placeholder*='URL']", "input[type='text']", "input"]:
                try:
                    inp = page.locator(sel).first
                    if inp.is_visible(timeout=5000):
                        inp.fill(tiktok_url)
                        url_filled = True
                        emit(q, 10, f"Link pasted: {tiktok_url[:40]}...")
                        log("URL filled")
                        break
                except:
                    continue

            if not url_filled:
                emit(q, 99, "No URL input found", done=True)
                log("FAILED: No URL input")
                browser.close()
                return

            # Get input position for bar detection (original logic)
            inp = page.locator("input[placeholder*='Enter Video URL'], input[placeholder*='Video URL'], input[placeholder*='URL'], input[type='text']").first
            inp_box = inp.bounding_box()
            input_bottom_y = inp_box["y"] + inp_box["height"] if inp_box else 200

            # ══════════════════════════════════════════════
            # ── MAIN LOOP (FIXED: continuous, proper waits) ──
            # ══════════════════════════════════════════════
            total_views_sent = 0
            max_cycles = 50

            for cycle in range(1, max_cycles + 1):
                log(f"=== Cycle {cycle} ===")
                emit(q, 11, f"Cycle {cycle}: Clicking Search...")

                # ── Re-fill URL on cycles 2+ ──
                if cycle > 1:
                    try:
                        inp = page.locator("input[placeholder*='Enter Video URL'], input[placeholder*='Video URL'], input[placeholder*='URL'], input[type='text']").first
                        if inp.is_visible(timeout=3000):
                            inp.fill("")
                            time.sleep(0.3)
                            inp.fill(tiktok_url)
                            log("URL re-filled")
                    except Exception as e:
                        log(f"URL re-fill failed: {e}")

                # ── Click Search (original logic) ──
                search_clicked = False
                for sel in ["button:has-text('Search')", "input[type='submit']"]:
                    try:
                        btn = page.locator(sel).first
                        if btn.is_visible(timeout=3000):
                            btn.click()
                            search_clicked = True
                            emit(q, 12, "Search clicked")
                            log("Search clicked")
                            break
                    except:
                        continue

                if not search_clicked:
                    try:
                        btn = page.locator("button").all()
                        for b in btn:
                            if b.is_visible(timeout=1000):
                                txt = b.inner_text(timeout=500).strip()
                                if "search" in txt.lower() or "🔍" in txt:
                                    b.click()
                                    search_clicked = True
                                    emit(q, 12, "Search clicked (fallback)")
                                    log("Search clicked (fallback)")
                                    break
                    except:
                        pass

                if not search_clicked:
                    emit(q, 99, "No Search button", done=True)
                    log("FAILED: No Search button")
                    browser.close()
                    return

                time.sleep(6)

                # ── Check for rate limit (FIXED: parse actual timer) ──
                try:
                    body_text = page.locator("body").inner_text(timeout=3000)
                    if "Too many requests" in body_text or "Please wait" in body_text or "too many" in body_text.lower():
                        wait_secs = parse_timer_seconds(body_text)
                        wait_secs = min(wait_secs, 600)  # cap at 10 min
                        emit(q, 13, f"⏳ Rate limited — waiting {wait_secs}s...")
                        log(f"Rate limited, waiting {wait_secs}s")
                        time.sleep(wait_secs + 5)
                        continue
                except:
                    pass

                # ── Find and click bar under input (original logic) ──
                emit(q, 14, "Looking for bar under input...")
                log("Looking for bar...")

                bar_clicked = False
                try:
                    check_x = int(inp_box["x"] + inp_box["width"] / 2) if inp_box else 195
                    check_y = int(input_bottom_y + 50)

                    el_info = page.evaluate("""(coords) => {
                        const el = document.elementFromPoint(coords.x, coords.y);
                        if (!el) return null;
                        let curr = el;
                        while (curr && curr.tagName !== 'BODY') {
                            if (curr.tagName === 'BUTTON' || curr.tagName === 'A' || curr.tagName === 'DIV') {
                                return {
                                    tag: curr.tagName,
                                    text: curr.innerText ? curr.innerText.trim().slice(0,50) : '',
                                    class: curr.className,
                                    y: curr.getBoundingClientRect().top
                                };
                            }
                            curr = curr.parentElement;
                        }
                        return {tag: el.tagName, text: el.innerText ? el.innerText.trim().slice(0,50) : '', class: el.className};
                    }""", {"x": check_x, "y": check_y})

                    log(f"Found under input: {el_info}")
                    emit(q, 14, f"Found under input: {el_info}")

                    if el_info and el_info.get("text") and "important" not in el_info["text"].lower() and "notice" not in el_info["text"].lower():
                        page.mouse.click(check_x, check_y)
                        bar_clicked = True
                        emit(q, 15, f"Clicked bar: '{el_info.get('text', '')[:20]}'")
                        log(f"Bar clicked: '{el_info.get('text', '')[:20]}'")
                except Exception as e:
                    emit(q, 14, f"Point check failed: {str(e)[:60]}")
                    log(f"Point check failed: {e}")

                # Fallback scan (original logic)
                if not bar_clicked:
                    try:
                        all_els = page.locator("button, div, a").all()
                        for el in all_els:
                            try:
                                box = el.bounding_box()
                                if not box:
                                    continue
                                dist_below = box["y"] - input_bottom_y
                                if 10 < dist_below < 120 and box["height"] > 30 and el.is_visible(timeout=1000):
                                    txt = el.inner_text(timeout=500).strip()
                                    if not txt:
                                        continue
                                    if "important" in txt.lower() or "notice" in txt.lower() or "official" in txt.lower():
                                        continue
                                    has_digits = any(c.isdigit() for c in txt)
                                    bg = el.evaluate("e => getComputedStyle(e).backgroundColor")
                                    is_dark = "rgb(0" in bg or "rgb(3" in bg or "rgb(4" in bg or "rgb(5" in bg or "rgb(6" in bg

                                    if has_digits or is_dark:
                                        el.click()
                                        bar_clicked = True
                                        emit(q, 15, f"Clicked bar (scan): '{txt[:20]}'")
                                        log(f"Bar clicked (scan): '{txt[:20]}'")
                                        break
                            except:
                                continue
                    except:
                        pass

                if not bar_clicked:
                    log("No bar found")
                    emit(q, 16, "No bar found — retrying...")
                    time.sleep(5)
                    continue

                # ── Wait for result (FIXED: longer wait, proper success detection) ──
                emit(q, 16, "Waiting for result...")
                log("Waiting for result...")
                time.sleep(3)

                result_found = False
                for poll in range(30):  # 30 x 2s = 60s max
                    time.sleep(2)
                    try:
                        body_text = page.locator("body").inner_text(timeout=3000)

                        # Success!
                        if "Successfully" in body_text:
                            match = re.search(r'Successfully\s+(\d+)\s+views?\s+sent', body_text, re.IGNORECASE)
                            count = match.group(1) if match else "?"
                            total_views_sent += int(count) if count != "?" else 0
                            emit(q, 17, f"✅ {count} views sent! (Total: {total_views_sent})")
                            log(f"SUCCESS: {count} views sent, total={total_views_sent}")
                            result_found = True
                            break

                        # Still checking
                        if "Checking Timer" in body_text:
                            emit(q, 16, "Checking Timer... please wait")
                            continue

                        # Rate limit after bar click
                        if "Too many requests" in body_text or "Please wait" in body_text:
                            wait_secs = parse_timer_seconds(body_text)
                            wait_secs = min(wait_secs, 600)
                            emit(q, 13, f"⏳ Rate limited — waiting {wait_secs}s...")
                            log(f"Rate limited after bar click, waiting {wait_secs}s")
                            time.sleep(wait_secs + 5)
                            result_found = True  # break inner, continue outer
                            break
                    except:
                        continue

                    # Check spinner
                    try:
                        spinner = page.locator(".spinner, [class*='spinner']").first
                        if spinner.is_visible(timeout=500):
                            continue
                    except:
                        pass

                if not result_found:
                    emit(q, 16, "No result after 60s — continuing...")
                    log("No result after 60s")

                time.sleep(3)

            emit(q, 18, f"🏁 Finished {max_cycles} cycles. Total views sent: {total_views_sent}", done=True)
            log(f"Done. Total views: {total_views_sent}")

        except Exception as e:
            emit(q, 99, f"Error: {str(e)}", done=True)
            log(f"EXCEPTION: {e}")
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
                msg = q.get(timeout=600)
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
