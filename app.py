from flask import Flask, render_template, request, jsonify, Response, stream_with_context
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
import os, time, json, queue, threading

app = Flask(__name__)
ZEFOY = "https://zefoy.com"

# Store live logs per request
live_queues = {}

def emit(q, step, message, done=False):
    q.put(json.dumps({"step": step, "message": message, "done": done}) + "\n")

def run_bot(tiktok_url, q):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )

        try:
            emit(q, 1, "Loading zefoy.com...")

            page.goto(ZEFOY, wait_until="networkidle", timeout=30000)
            time.sleep(3)

            # Check for captcha
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

                # Fill captcha input
                input_selectors = [
                    "input[placeholder*='Enter the word']",
                    "input[placeholder*='word']",
                    "input[type='text']",
                    "input"
                ]
                for sel in input_selectors:
                    try:
                        inp = page.locator(sel).first
                        if inp.is_visible(timeout=2000):
                            inp.fill(captcha_text)
                            emit(q, 5, "Captcha entered")
                            break
                    except:
                        continue

                # Click submit
                btn_selectors = ["button[type='submit']", "button:has-text('✓')", "button"]
                for sel in btn_selectors:
                    try:
                        btn = page.locator(sel).first
                        if btn.is_visible(timeout=2000):
                            btn.click()
                            emit(q, 6, "Captcha submitted")
                            break
                    except:
                        continue
                time.sleep(4)

            # Click Views
            emit(q, 7, "Looking for Views button...")
            views_clicked = False
            views_selectors = ["text=Views", "a:has-text('Views')", "button:has-text('Views')", "div:has-text('Views')"]
            for sel in views_selectors:
                try:
                    btn = page.locator(sel).first
                    if btn.is_visible(timeout=5000):
                        btn.click()
                        views_clicked = True
                        emit(q, 8, "Views button clicked")
                        break
                except:
                    continue

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
                        emit(q, 10, f"URL entered: {tiktok_url[:30]}...")
                        break
                except Exception as e:
                    continue

            if not url_filled:
                # Debug: dump page HTML to see what inputs exist
                html_snippet = page.content()[:2000]
                emit(q, 99, f"Could not find URL input field. Page HTML snippet: {html_snippet[:500]}", done=True)
                browser.close()
                return

            # Click Search
            emit(q, 11, "Clicking Search...")
            search_clicked = False
            search_selectors = ["button:has-text('Search')", "input[type='submit']", "button"]
            for sel in search_selectors:
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

            # Check for rate limit
            try:
                too_many = page.locator("text=Too many requests")
                if too_many.is_visible(timeout=3000):
                    emit(q, 13, "Rate limited — waiting 12s...")
                    time.sleep(12)
                    page.reload(wait_until="networkidle", timeout=30000)
                    time.sleep(3)
                    for sel in url_input_selectors:
                        try:
                            inp = page.locator(sel).first
                            if inp.is_visible(timeout=3000):
                                inp.fill(tiktok_url)
                                break
                        except:
                            continue
                    for sel in search_selectors:
                        try:
                            btn = page.locator(sel).first
                            if btn.is_visible(timeout=3000):
                                btn.click()
                                break
                        except:
                            continue
                    time.sleep(5)
            except:
                pass

            # Click send button
            emit(q, 14, "Looking for send button...")
            send_clicked = False
            send_selectors = [
                "button:has-text('16,800')",
                "button:has-text('1,000')",
                "button:has-text('Send')",
                "div:has-text('16,800')",
                ".btn-send",
                "[class*='send']",
                "button"
            ]
            for sel in send_selectors:
                try:
                    btn = page.locator(sel).first
                    if btn.is_visible(timeout=5000):
                        btn.click()
                        send_clicked = True
                        emit(q, 15, "Send button clicked — sending views...")
                        break
                except:
                    continue

            if not send_clicked:
                emit(q, 99, "Could not find send button. Video may not exist or service is down.", done=True)
                browser.close()
                return

            time.sleep(3)

            # Wait for success
            emit(q, 16, "Waiting for confirmation...")
            success_found = False
            try:
                page.wait_for_selector("text=Successfully", timeout=30000)
                msg = page.locator("text=Successfully").first.inner_text(timeout=5000)
                emit(q, 17, msg, done=True)
                success_found = True
            except:
                pass

            if not success_found:
                fallback = ["sent", "complete", "done", "views", "1000"]
                for txt in fallback:
                    try:
                        el = page.locator(f"text={txt}").first
                        if el.is_visible(timeout=2000):
                            emit(q, 17, "Views sent successfully", done=True)
                            success_found = True
                            break
                    except:
                        continue

            if not success_found:
                emit(q, 99, "Unknown result — check Zefoy manually", done=True)

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
