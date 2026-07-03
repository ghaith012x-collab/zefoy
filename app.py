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

            insight = get_page_insight(page)
            emit(q, 1, f"Page loaded: {insight['title']}", inspect=insight)

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

            insight = get_page_insight(page)
            emit(q, 7, "Post-captcha page state", inspect=insight)

            # Click Views arrow button
            emit(q, 8, "Looking for Views arrow button...")
            views_clicked = False

            try:
                all_elements = page.locator("h1, h2, h3, h4, h5, h6, p, div, button, a, span").all()
                views_idx = -1
                for i, el in enumerate(all_elements):
                    try:
                        if el.inner_text(timeout=500).strip() == "Views":
                            views_idx = i
                            emit(q, 8, f"Found 'Views' at element index {i}")
                            break
                    except:
                        continue

                if views_idx >= 0:
                    for j in range(views_idx + 1, min(views_idx + 5, len(all_elements))):
                        el = all_elements[j]
                        try:
                            tag = el.evaluate("e => e.tagName.toLowerCase()")
                            txt = el.inner_text(timeout=500).strip()
                            emit(q, 8, f"Checking element {j}: <{tag}> '{txt[:20]}'")
                            if tag in ["button", "a"]:
                                el.click()
                                views_clicked = True
                                emit(q, 9, f"Clicked arrow button after Views")
                                break
                        except:
                            continue
            except Exception as e:
                emit(q, 8, f"Views search error: {str(e)[:80]}")

            if not views_clicked:
                for sel in ["button:has(> svg)", "button:has(> i)", "a:has(> svg)", "a:has(> i)", "[class*='arrow']", "[class*='views'] button", "[class*='views'] a", "button", "a"]:
                    try:
                        elements = page.locator(sel).all()
                        for el in elements:
                            try:
                                txt = el.inner_text(timeout=500).strip()
                                if not txt or "→" in txt or ">" in txt:
                                    if el.is_visible(timeout=2000):
                                        el.click()
                                        views_clicked = True
                                        emit(q, 9, f"Clicked arrow button")
                                        break
                            except:
                                continue
                        if views_clicked:
                            break
                    except:
                        continue

            if not views_clicked:
                insight = get_page_insight(page)
                emit(q, 99, "Could not find Views button", inspect=insight, done=True)
                browser.close()
                return

            time.sleep(3)
            insight = get_page_insight(page)
            emit(q, 10, "Views page loaded", inspect=insight)

            # Fill TikTok URL
            emit(q, 11, "Looking for video URL input...")
            url_input_selectors = [
                "input[placeholder*='Enter Video URL']",
                "input[placeholder*='Video URL']",
                "input[placeholder*='URL']",
                "input[placeholder*='video']",
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
                        emit(q, 12, f"Link pasted: {tiktok_url[:40]}...")
                        break
                except:
                    continue

            if not url_filled:
                insight = get_page_insight(page)
                emit(q, 99, "Could not find URL input", inspect=insight, done=True)
                browser.close()
                return

            # Click Search + handle "Too many requests" loop
            max_retries = 10
            for attempt in range(1, max_retries + 1):
                emit(q, 13, f"Clicking Search (attempt {attempt})...")
                search_clicked = False
                for sel in ["button:has-text('Search')", "input[type='submit']", "button"]:
                    try:
                        btn = page.locator(sel).first
                        if btn.is_visible(timeout=3000):
                            btn.click()
                            search_clicked = True
                            emit(q, 14, "Search clicked")
                            break
                    except:
                        continue

                if not search_clicked:
                    emit(q, 99, "Could not find Search button", done=True)
                    browser.close()
                    return

                time.sleep(6)

                # Check for "Too many requests"
                too_many = False
                try:
                    tm = page.locator("text=Too many requests")
                    if tm.is_visible(timeout=3000):
                        too_many = True
                        emit(q, 15, "Got 'Too many requests' — retrying...")
                        time.sleep(3)
                        continue
                except:
                    pass

                if not too_many:
                    break

                if attempt == max_retries:
                    emit(q, 99, "Max retries reached. Zefoy is rate limiting.", done=True)
                    browser.close()
                    return

            # Click the dark bar with red camera / view count
            emit(q, 16, "Looking for view count bar (red camera icon)...")
            camera_clicked = False
            camera_selectors = [
                "button:has-text('354')",
                "button:has-text('16,800')",
                "button:has-text('1,000')",
                "div:has-text('354')",
                "div:has-text('16,800')",
                "[class*='camera']",
                "[class*='video']",
                "button[class*='dark']",
                "div[class*='dark']",
                "button",
                "div"
            ]
            for sel in camera_selectors:
                try:
                    elements = page.locator(sel).all()
                    for el in elements:
                        try:
                            txt = el.inner_text(timeout=500).strip()
                            # Look for numbers (view counts) or camera icon
                            has_number = any(c.isdigit() for c in txt)
                            has_camera = "camera" in txt.lower() or "video" in txt.lower()
                            if (has_number or has_camera) and el.is_visible(timeout=2000):
                                el.click()
                                camera_clicked = True
                                emit(q, 17, f"Clicked view bar: '{txt[:30]}'")
                                break
                        except:
                            continue
                    if camera_clicked:
                        break
                except:
                    continue

            if not camera_clicked:
                insight = get_page_insight(page)
                emit(q, 99, "Could not find view count bar", inspect=insight, done=True)
                browser.close()
                return

            time.sleep(3)

            # Wait for success
            emit(q, 18, "Waiting for confirmation...")
            success_found = False
            try:
                page.wait_for_selector("text=Successfully", timeout=30000)
                msg = page.locator("text=Successfully").first.inner_text(timeout=5000)
                emit(q, 19, msg, done=True)
                success_found = True
            except:
                pass

            if not success_found:
                for txt in ["sent", "complete", "done", "views", "1000"]:
                    try:
                        el = page.locator(f"text={txt}").first
                        if el.is_visible(timeout=2000):
                            emit(q, 19, "Views sent successfully", done=True)
                            success_found = True
                            break
                    except:
                        continue

            if not success_found:
                emit(q, 99, "Unknown result", done=True)

        except Exception as e:
            insight = get_page_insight(page) if 'page' in locals() else {"error": "page not created"}
            emit(q, 99, f"Error: {str(e)}", inspect=insight, done=True)
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
                msg = q.get(timeout=180)
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
