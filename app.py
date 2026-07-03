from flask import Flask, render_template, request, jsonify, Response, stream_with_context
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
import os, time, json, queue, threading

app = Flask(__name__)
ZEFOY = "https://zefoy.com"

live_queues = {}

def emit(q, step, message, done=False, inspect=None):
    payload = {"step": step, "message": message, "done": done}
    if inspect:
        payload["inspect"] = inspect
    q.put(json.dumps(payload) + "\n")

def get_page_insight(page):
    try:
        texts = page.locator("body >> visible=true").all_inner_texts()
        visible_text = " | ".join([t.strip() for t in texts if t.strip()][:10])
        buttons = page.locator("button, [role='button'], a").all()
        btn_info = []
        for b in buttons[:20]:
            try:
                txt = b.inner_text(timeout=500).strip()
                if txt and len(txt) < 100:
                    tag = b.evaluate("el => el.tagName.toLowerCase()")
                    cls = b.evaluate("el => el.className")[:50]
                    btn_info.append(f"{tag}:{txt[:30]}(class={cls})")
            except:
                pass
        inputs = page.locator("input").all()
        input_info = []
        for inp in inputs[:10]:
            try:
                ph = inp.get_attribute("placeholder") or ""
                typ = inp.get_attribute("type") or "text"
                if ph:
                    input_info.append(f"{typ}:{ph[:40]}")
            except:
                pass
        return {
            "title": page.title(),
            "url": page.url,
            "visible_text_preview": visible_text[:300],
            "buttons_found": btn_info,
            "inputs_found": input_info
        }
    except Exception as e:
        return {"error": str(e)}

def run_bot(tiktok_url, q):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            viewport={"width": 390, "height": 844},
            user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
        )

        try:
            emit(q, 1, "Loading zefoy.com...")
            page.goto(ZEFOY, wait_until="networkidle", timeout=30000)
            time.sleep(3)

            # Captcha check
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

            # Click Views arrow button
            emit(q, 7, "Looking for Views arrow button...")
            views_clicked = False

            # Strategy: Find all buttons, look for the one with arrow icon (empty text or just arrow symbol)
            all_btns = page.locator("button, a").all()
            emit(q, 7, f"Found {len(all_btns)} clickable elements")
            
            for btn in all_btns:
                try:
                    txt = btn.inner_text(timeout=500).strip()
                    html = btn.evaluate("el => el.outerHTML")[:200]
                    # The arrow button has no text, just an icon inside
                    if not txt or txt in ["→", ">", ""]:
                        # Check if it contains an icon (i, svg, img)
                        has_icon = btn.locator("i, svg, img").count() > 0
                        if has_icon or not txt:
                            btn.click()
                            views_clicked = True
                            emit(q, 8, "Clicked arrow button")
                            break
                except:
                    continue

            # Fallback: click by index — the Views arrow is typically around index 4-6
            if not views_clicked:
                try:
                    # Get parent containers of "Views" text
                    views_text = page.locator("text=Views").first
                    parent = views_text.locator("xpath=..")
                    arrow = parent.locator("button, a").first
                    if arrow.is_visible(timeout=3000):
                        arrow.click()
                        views_clicked = True
                        emit(q, 8, "Clicked arrow button via parent")
                except:
                    pass

            if not views_clicked:
                emit(q, 99, "Could not find Views button", done=True)
                browser.close()
                return

            time.sleep(3)

            # Fill TikTok URL
            emit(q, 9, "Looking for video URL input...")
            url_input_selectors = [
                "input[placeholder*='Enter Video URL']",
                "input[placeholder*='Video URL']",
                "input[placeholder*='URL']",
                "input[type='text']",
                "input"
            ]
            url_filled = False
            for sel in url_input_selectors:
                try:
                    inp = page.locator(sel).first
                    if inp.is_visible(timeout=5000):
                        inp.fill(tiktok_url)
                        url_filled = True
                        emit(q, 10, f"Link pasted: {tiktok_url[:40]}...")
                        break
                except:
                    continue

            if not url_filled:
                emit(q, 99, "Could not find URL input", done=True)
                browser.close()
                return

            # MAIN LOOP: Search → Click Camera Bar → Handle "Too many requests" → Retry
            max_cycles = 15
            for cycle in range(1, max_cycles + 1):
                emit(q, 11, f"Cycle {cycle}: Clicking Search...")
                
                # Click Search
                search_clicked = False
                for sel in ["button:has-text('Search')", "input[type='submit']", "button"]:
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
                    emit(q, 99, "Could not find Search button", done=True)
                    browser.close()
                    return

                time.sleep(6)

                # Check for "Too many requests" immediately
                too_many = False
                try:
                    tm = page.locator("text=Too many requests")
                    if tm.is_visible(timeout=3000):
                        too_many = True
                        emit(q, 13, "Got 'Too many requests' — will retry after clicking camera bar")
                except:
                    pass

                # Click the dark camera/view count bar
                emit(q, 14, "Looking for view count bar (red camera icon)...")
                camera_clicked = False
                
                # The camera bar appears as a dark element with a number and red camera icon
                # It's typically a button or div with dark background
                camera_selectors = [
                    "button[class*='dark']",
                    "div[class*='dark']",
                    "[style*='background-color: rgb(54']",  # Dark gray background
                    "[style*='background:#3']",
                    "button:has(> i[class*='video'])",
                    "button:has(> i[class*='camera'])",
                    "div:has(> i[class*='video'])",
                    "div:has(> i[class*='camera'])",
                ]
                
                for sel in camera_selectors:
                    try:
                        elements = page.locator(sel).all()
                        for el in elements:
                            try:
                                txt = el.inner_text(timeout=500).strip()
                                # Must contain digits (view count) and NOT contain "Important" or "Notice"
                                has_digits = any(c.isdigit() for c in txt)
                                is_notice = "important" in txt.lower() or "notice" in txt.lower() or "zefoy" in txt.lower()
                                if has_digits and not is_notice and el.is_visible(timeout=2000):
                                    el.click()
                                    camera_clicked = True
                                    emit(q, 15, f"Clicked view bar: '{txt[:20]}'")
                                    break
                            except:
                                continue
                        if camera_clicked:
                            break
                    except:
                        continue

                # Fallback: find by text containing digits only (like "354", "16,800")
                if not camera_clicked:
                    try:
                        all_elements = page.locator("button, div").all()
                        for el in all_elements:
                            try:
                                txt = el.inner_text(timeout=500).strip()
                                # Pure number or number with comma, no letters
                                clean = txt.replace(",", "").replace(" ", "")
                                if clean.isdigit() and el.is_visible(timeout=2000):
                                    # Verify it's dark/black background
                                    bg = el.evaluate("e => getComputedStyle(e).backgroundColor")
                                    if "rgb(0" in bg or "rgb(3" in bg or "rgb(4" in bg or "rgb(5" in bg:
                                        el.click()
                                        camera_clicked = True
                                        emit(q, 15, f"Clicked view bar by number: '{txt}'")
                                        break
                            except:
                                continue
                    except:
                        pass

                if not camera_clicked:
                    emit(q, 16, "No camera bar found this cycle — checking if already complete...")
                    # Maybe it already succeeded?
                    try:
                        success = page.locator("text=Successfully").first
                        if success.is_visible(timeout=2000):
                            msg = success.inner_text(timeout=3000)
                            emit(q, 17, msg, done=True)
                            browser.close()
                            return
                    except:
                        pass

                    # If "Too many requests" was shown and no bar, just retry search
                    if too_many:
                        emit(q, 13, "Rate limited, retrying search...")
                        time.sleep(5)
                        continue
                    else:
                        emit(q, 99, "Could not find camera bar", done=True)
                        browser.close()
                        return

                # Wait for spinner / result after clicking camera bar
                emit(q, 16, "Waiting for result after camera bar click...")
                time.sleep(3)

                # Check various result states
                result_found = False
                
                # State 1: Success message
                try:
                    success = page.locator("text=Successfully").first
                    if success.is_visible(timeout=10000):
                        msg = success.inner_text(timeout=5000)
                        emit(q, 17, msg, done=True)
                        result_found = True
                        browser.close()
                        return
                except:
                    pass

                # State 2: "Checking Timer..." / "Next Submit: READY"
                try:
                    timer = page.locator("text=Checking Timer").first
                    if timer.is_visible(timeout=5000):
                        emit(q, 17, "Timer check in progress...")
                        # Wait for it to complete
                        time.sleep(15)
                        # Check again
                        try:
                            ready = page.locator("text=READY").first
                            if ready.is_visible(timeout=5000):
                                emit(q, 18, "Timer ready — can submit again")
                                # The camera bar should reappear, loop continues
                                result_found = True
                                time.sleep(3)
                                continue
                        except:
                            pass
                except:
                    pass

                # State 3: "Too many requests" after clicking bar
                try:
                    tm = page.locator("text=Too many requests").first
                    if tm.is_visible(timeout=3000):
                        emit(q, 13, "Rate limited after bar click — retrying...")
                        time.sleep(5)
                        result_found = True
                        continue
                except:
                    pass

                # State 4: Spinner still going, wait more
                try:
                    spinner = page.locator(".spinner, [class*='spinner'], [class*='loading']").first
                    if spinner.is_visible(timeout=2000):
                        emit(q, 16, "Still loading, waiting more...")
                        time.sleep(10)
                        # Re-check success
                        try:
                            success = page.locator("text=Successfully").first
                            if success.is_visible(timeout=5000):
                                msg = success.inner_text(timeout=3000)
                                emit(q, 17, msg, done=True)
                                browser.close()
                                return
                        except:
                            pass
                except:
                    pass

                if not result_found:
                    emit(q, 16, "No clear result yet, continuing to next cycle...")

            emit(q, 99, "Max cycles reached", done=True)

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
                msg = q.get(timeout=300)
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
