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

            # CLICK VIEWS ARROW
            emit(q, 7, "Clicking Views arrow button...")
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
            except Exception as e:
                emit(q, 7, f"Failed: {str(e)[:60]}")

            if not views_clicked:
                emit(q, 99, "Failed to click Views", done=True)
                browser.close()
                return

            time.sleep(3)

            # FILL URL
            emit(q, 9, "Looking for URL input...")
            for sel in ["input[placeholder*='Enter Video URL']", "input[placeholder*='Video URL']", "input[placeholder*='URL']", "input[type='text']", "input"]:
                try:
                    inp = page.locator(sel).first
                    if inp.is_visible(timeout=5000):
                        inp.fill(tiktok_url)
                        emit(q, 10, f"Link pasted: {tiktok_url[:40]}...")
                        break
                except:
                    continue
            else:
                emit(q, 99, "No URL input found", done=True)
                browser.close()
                return

            # GET INPUT POSITION FOR "UNDER" DETECTION
            inp = page.locator("input[placeholder*='Enter Video URL'], input[placeholder*='Video URL'], input[placeholder*='URL'], input[type='text']").first
            inp_box = inp.bounding_box()
            input_bottom_y = inp_box["y"] + inp_box["height"] if inp_box else 200

            # MAIN LOOP: Click Search → Check for bar → Retry on rate limit
            max_cycles = 20
            for cycle in range(1, max_cycles + 1):
                emit(q, 11, f"Cycle {cycle}: Clicking Search...")

                # CLICK SEARCH
                search_clicked = False
                for sel in ["button:has-text('Search')", "input[type='submit']"]:
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
                    # Fallback: find blue button next to input
                    try:
                        btn = page.locator("button").all()
                        for b in btn:
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

                time.sleep(6)

                # CHECK FOR "TOO MANY REQUESTS"
                too_many = False
                try:
                    tm = page.locator("text=Too many requests").first
                    if tm.is_visible(timeout=3000):
                        too_many = True
                        emit(q, 13, "Rate limited — clicking Search again...")
                        time.sleep(3)
                        continue
                except:
                    pass

                # FIND AND CLICK ELEMENT DIRECTLY UNDER INPUT (within ~120px / 3-4cm)
                emit(q, 14, "Looking for bar under input...")
                
                bar_clicked = False
                try:
                    # Use elementFromPoint at position directly under input
                    check_x = int(inp_box["x"] + inp_box["width"] / 2) if inp_box else 195
                    check_y = int(input_bottom_y + 50)  # ~50px below input = ~1.5cm
                    
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
                    
                    emit(q, 14, f"Found under input: {el_info}")

                    # Click the element at that position
                    if el_info and el_info.get("text") and "important" not in el_info["text"].lower() and "notice" not in el_info["text"].lower():
                        page.mouse.click(check_x, check_y)
                        bar_clicked = True
                        emit(q, 15, f"Clicked bar: '{el_info.get('text', '')[:20]}'")
                except Exception as e:
                    emit(q, 14, f"Point check failed: {str(e)[:60]}")

                # Fallback: scan all visible elements, find one within 120px below input
                if not bar_clicked:
                    try:
                        all_els = page.locator("button, div, a").all()
                        for el in all_els:
                            try:
                                box = el.bounding_box()
                                if not box:
                                    continue
                                # Must be within 120px below input bottom
                                dist_below = box["y"] - input_bottom_y
                                if 10 < dist_below < 120 and box["height"] > 30 and el.is_visible(timeout=1000):
                                    txt = el.inner_text(timeout=500).strip()
                                    if not txt:
                                        continue
                                    # Skip notices
                                    if "important" in txt.lower() or "notice" in txt.lower() or "official" in txt.lower():
                                        continue
                                    # Must have digits (view count) or be dark colored
                                    has_digits = any(c.isdigit() for c in txt)
                                    bg = el.evaluate("e => getComputedStyle(e).backgroundColor")
                                    is_dark = "rgb(0" in bg or "rgb(3" in bg or "rgb(4" in bg or "rgb(5" in bg or "rgb(6" in bg
                                    
                                    if has_digits or is_dark:
                                        el.click()
                                        bar_clicked = True
                                        emit(q, 15, f"Clicked bar (scan): '{txt[:20]}'")
                                        break
                            except:
                                continue
                    except:
                        pass

                if not bar_clicked:
                    emit(q, 16, "No bar found — checking if done...")
                    # Maybe success already?
                    try:
                        success = page.locator("text=Successfully").first
                        if success.is_visible(timeout=2000):
                            msg = success.inner_text(timeout=3000)
                            emit(q, 17, msg, done=True)
                            browser.close()
                            return
                    except:
                        pass
                    
                    # If rate limit, loop continues
                    if too_many:
                        continue
                    
                    emit(q, 99, "No bar appeared", done=True)
                    browser.close()
                    return

                # WAIT FOR RESULT AFTER CLICKING BAR
                emit(q, 16, "Waiting for result...")
                time.sleep(3)

                # Check success
                try:
                    success = page.locator("text=Successfully").first
                    if success.is_visible(timeout=20000):
                        msg = success.inner_text(timeout=5000)
                        emit(q, 17, msg, done=True)
                        browser.close()
                        return
                except:
                    pass

                # Check "Checking Timer"
                try:
                    timer = page.locator("text=Checking Timer").first
                    if timer.is_visible(timeout=5000):
                        emit(q, 16, "Checking Timer...")
                        time.sleep(20)
                        try:
                            ready = page.locator("text=READY").first
                            if ready.is_visible(timeout=5000):
                                msg = ready.inner_text(timeout=3000)
                                emit(q, 17, f"Timer ready: {msg}", done=True)
                                browser.close()
                                return
                        except:
                            pass
                except:
                    pass

                # Check rate limit after bar click
                try:
                    tm = page.locator("text=Too many requests").first
                    if tm.is_visible(timeout=2000):
                        emit(q, 13, "Rate limited after bar click — retrying...")
                        time.sleep(3)
                        continue
                except:
                    pass

                # Spinner wait
                try:
                    spinner = page.locator(".spinner, [class*='spinner']").first
                    if spinner.is_visible(timeout=2000):
                        emit(q, 16, "Spinner active, waiting 10s...")
                        time.sleep(10)
                except:
                    pass

            emit(q, 99, "Max cycles", done=True)

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
