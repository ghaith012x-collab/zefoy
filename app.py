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

            # CLICK VIEWS ARROW — FORCE IT
            emit(q, 7, "Clicking Views arrow button...")
            views_clicked = False
            
            # Method 1: Find the Views card/container, then click the button inside it
            try:
                # Get all divs/containers and find one containing "Views" text
                all_divs = page.locator("div, section, article").all()
                for div in all_divs:
                    try:
                        txt = div.inner_text(timeout=500).strip()
                        if txt.startswith("Views") or txt == "Views":
                            # Found Views container, now find button inside it
                            btn = div.locator("button, a").first
                            if btn.is_visible(timeout=3000):
                                btn.click()
                                views_clicked = True
                                emit(q, 8, "Clicked Views arrow button")
                                break
                    except:
                        continue
            except Exception as e:
                emit(q, 7, f"Method 1 failed: {str(e)[:60]}")

            # Method 2: Click by coordinates relative to Views text
            if not views_clicked:
                try:
                    views_el = page.locator("text=Views").first
                    box = views_el.bounding_box()
                    if box:
                        # Click below the Views text (where the arrow button is)
                        x = box["x"] + box["width"] / 2
                        y = box["y"] + box["height"] + 30
                        page.mouse.click(x, y)
                        views_clicked = True
                        emit(q, 8, "Clicked Views arrow by coordinates")
                except Exception as e:
                    emit(q, 7, f"Method 2 failed: {str(e)[:60]}")

            # Method 3: JavaScript click on element after Views
            if not views_clicked:
                try:
                    page.evaluate("""() => {
                        const all = document.querySelectorAll('*');
                        for (let i = 0; i < all.length; i++) {
                            if (all[i].innerText && all[i].innerText.trim() === 'Views') {
                                // Next sibling or parent's next button
                                let el = all[i].parentElement.querySelector('button, a');
                                if (el) { el.click(); return true; }
                            }
                        }
                        return false;
                    }""")
                    views_clicked = True
                    emit(q, 8, "Clicked Views arrow via JS")
                except Exception as e:
                    emit(q, 7, f"Method 3 failed: {str(e)[:60]}")

            if not views_clicked:
                emit(q, 99, "Failed to click Views button", done=True)
                browser.close()
                return

            time.sleep(3)

            # FILL URL INPUT
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

            # MAIN LOOP: Search → Click whatever appears under input → Retry on rate limit
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

                # CLICK WHATEVER APPEARS DIRECTLY UNDER THE INPUT FIELD
                emit(q, 13, "Clicking element under URL input...")
                
                clicked_under = False
                try:
                    # Get the URL input position
                    inp = page.locator("input[placeholder*='Enter Video URL'], input[placeholder*='Video URL'], input[placeholder*='URL'], input[type='text']").first
                    inp_box = inp.bounding_box()
                    
                    if inp_box:
                        # Find all visible elements and click the one directly below input
                        x = inp_box["x"] + inp_box["width"] / 2
                        y = inp_box["y"] + inp_box["height"] + 40
                        
                        # Use elementFromPoint to find what's there
                        el_info = page.evaluate("""(coords) => {
                            const el = document.elementFromPoint(coords.x, coords.y);
                            if (!el) return null;
                            return {
                                tag: el.tagName,
                                text: el.innerText ? el.innerText.trim().slice(0,50) : '',
                                class: el.className,
                                clickable: el.tagName === 'BUTTON' || el.tagName === 'A' || el.onclick !== null
                            };
                        }""", {"x": int(x), "y": int(y)})
                        
                        emit(q, 13, f"Element under input: {el_info}")
                        
                        # Click it
                        page.mouse.click(x, y)
                        clicked_under = True
                        emit(q, 14, f"Clicked element under input: '{el_info.get('text', '')[:20]}'")
                except Exception as e:
                    emit(q, 13, f"Coordinate click failed: {str(e)[:60]}")

                # Fallback: click any visible button/div that appeared below input
                if not clicked_under:
                    try:
                        all_elements = page.locator("button, div, a").all()
                        for el in all_elements:
                            try:
                                box = el.bounding_box()
                                if not box:
                                    continue
                                # Must be below the input area
                                if box["y"] > 200 and box["height"] > 20 and el.is_visible(timeout=1000):
                                    txt = el.inner_text(timeout=500).strip()
                                    # Skip notice banners
                                    if "important" in txt.lower() or "notice" in txt.lower():
                                        continue
                                    el.click()
                                    clicked_under = True
                                    emit(q, 14, f"Clicked fallback element: '{txt[:20]}'")
                                    break
                            except:
                                continue
                    except:
                        pass

                if not clicked_under:
                    emit(q, 99, "Nothing to click under input", done=True)
                    browser.close()
                    return

                # Wait for result
                emit(q, 15, "Waiting for result...")
                time.sleep(3)

                # Check success
                try:
                    success = page.locator("text=Successfully").first
                    if success.is_visible(timeout=15000):
                        msg = success.inner_text(timeout=5000)
                        emit(q, 16, msg, done=True)
                        browser.close()
                        return
                except:
                    pass

                # Check "Checking Timer"
                try:
                    timer = page.locator("text=Checking Timer").first
                    if timer.is_visible(timeout=5000):
                        emit(q, 15, "Checking Timer...")
                        time.sleep(20)
                        try:
                            ready = page.locator("text=READY").first
                            if ready.is_visible(timeout=5000):
                                emit(q, 16, "Timer ready — done")
                                emit(q, 16, "Successfully sent views", done=True)
                                browser.close()
                                return
                        except:
                            pass
                except:
                    pass

                # Check rate limit
                try:
                    tm = page.locator("text=Too many requests").first
                    if tm.is_visible(timeout=3000):
                        emit(q, 13, "Rate limited — retrying in 5s...")
                        time.sleep(5)
                        continue
                except:
                    pass

                # Check spinner — wait more
                try:
                    spinner = page.locator(".spinner, [class*='spinner'], [class*='loading']").first
                    if spinner.is_visible(timeout=2000):
                        emit(q, 15, "Still spinning, waiting 10s more...")
                        time.sleep(10)
                except:
                    pass

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
