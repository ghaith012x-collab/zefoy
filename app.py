from flask import Flask, render_template, request, jsonify
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
import base64, io, os, time, threading
from PIL import Image
import pytesseract

app = Flask(__name__)

ZEFOY = "https://zefoy.com"

def solve_captcha(img_bytes):
    img = Image.open(io.BytesIO(img_bytes))
    img = img.convert('L').resize((img.width * 3, img.height * 3))
    text = pytesseract.image_to_string(
        img,
        config='--psm 8 -c tessedit_char_whitelist=abcdefghijklmnopqrstuvwxyz0123456789'
    )
    return text.strip().lower()

def run_bot(tiktok_url, result_holder):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )

        try:
            # Step 1: Load Zefoy with longer timeout
            page.goto(ZEFOY, wait_until="networkidle", timeout=30000)
            time.sleep(3)

            # Step 2: Check if captcha exists (sometimes it skips)
            captcha_present = False
            try:
                captcha_img = page.locator("img[src*='captcha'], .captcha img, [alt*='captcha']")
                captcha_img.wait_for(timeout=5000)
                captcha_present = True
            except PlaywrightTimeout:
                pass  # No captcha, proceed

            if captcha_present:
                img_bytes = captcha_img.screenshot()
                captcha_text = solve_captcha(img_bytes)
                print(f"Captcha solved: {captcha_text}")

                # Find the input field — try multiple selectors
                input_selectors = [
                    "input[placeholder*='Enter the word']",
                    "input[placeholder*='word']",
                    "input[type='text']",
                    "input[name*='captcha']",
                    "input"
                ]
                for sel in input_selectors:
                    try:
                        inp = page.locator(sel).first
                        if inp.is_visible(timeout=2000):
                            inp.fill(captcha_text)
                            break
                    except:
                        continue

                # Find submit button
                btn_selectors = [
                    "button[type='submit']",
                    "button:has-text('✓')",
                    "button:has-text('Submit')",
                    "button"
                ]
                for sel in btn_selectors:
                    try:
                        btn = page.locator(sel).first
                        if btn.is_visible(timeout=2000):
                            btn.click()
                            break
                    except:
                        continue

                time.sleep(4)

            # Step 3: Click Views — try multiple text matchers
            views_clicked = False
            views_selectors = ["text=Views", "text=views", "a:has-text('Views')", "button:has-text('Views')"]
            for sel in views_selectors:
                try:
                    views_btn = page.locator(sel).first
                    if views_btn.is_visible(timeout=5000):
                        views_btn.click()
                        views_clicked = True
                        break
                except:
                    continue

            if not views_clicked:
                result_holder["status"] = "error"
                result_holder["message"] = "Could not find Views button"
                browser.close()
                return

            time.sleep(3)

            # Step 4: Paste TikTok link
            url_input_selectors = [
                "input[placeholder*='Enter Video URL']",
                "input[placeholder*='URL']",
                "input[placeholder*='video']",
                "input[type='text']"
            ]
            url_filled = False
            for sel in url_input_selectors:
                try:
                    inp = page.locator(sel).first
                    if inp.is_visible(timeout=5000):
                        inp.fill(tiktok_url)
                        url_filled = True
                        break
                except:
                    continue

            if not url_filled:
                result_holder["status"] = "error"
                result_holder["message"] = "Could not find URL input field"
                browser.close()
                return

            # Step 5: Click Search
            search_clicked = False
            search_selectors = ["button:has-text('Search')", "button:has-text('search')", "input[type='submit']"]
            for sel in search_selectors:
                try:
                    btn = page.locator(sel).first
                    if btn.is_visible(timeout=3000):
                        btn.click()
                        search_clicked = True
                        break
                except:
                    continue

            if not search_clicked:
                result_holder["status"] = "error"
                result_holder["message"] = "Could not find Search button"
                browser.close()
                return

            time.sleep(6)

            # Step 6: Handle rate limit if shown
            try:
                too_many = page.locator("text=Too many requests")
                if too_many.is_visible(timeout=3000):
                    time.sleep(12)
                    page.reload(wait_until="networkidle", timeout=30000)
                    time.sleep(3)
                    # Re-fill URL
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

            # Step 7: Click the send/views button
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
                        break
                except:
                    continue

            if not send_clicked:
                result_holder["status"] = "error"
                result_holder["message"] = "Could not find send button. Video may not be found or service down."
                browser.close()
                return

            time.sleep(3)

            # Step 8: Wait for success message
            success_found = False
            try:
                page.wait_for_selector("text=Successfully", timeout=30000)
                success = page.locator("text=Successfully").first.inner_text(timeout=5000)
                result_holder["status"] = "success"
                result_holder["message"] = success
                success_found = True
            except:
                pass

            # Fallback: check for other confirmation texts
            if not success_found:
                fallback_texts = ["sent", "complete", "done", "views"]
                for txt in fallback_texts:
                    try:
                        el = page.locator(f"text={txt}").first
                        if el.is_visible(timeout=2000):
                            result_holder["status"] = "success"
                            result_holder["message"] = "Views sent successfully"
                            success_found = True
                            break
                    except:
                        continue

            if not success_found:
                result_holder["status"] = "error"
                result_holder["message"] = "Unknown result — check if views were sent manually"

        except Exception as e:
            result_holder["status"] = "error"
            result_holder["message"] = f"Exception: {str(e)}"
        finally:
            browser.close()

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/send', methods=['POST'])
def send():
    url = request.json.get('url')
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    result = {"status": "running"}
    thread = threading.Thread(target=run_bot, args=(url, result))
    thread.start()
    thread.join(timeout=180)

    if thread.is_alive():
        return jsonify({"status": "timeout", "message": "Operation timed out after 3 minutes"})

    return jsonify(result)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))
