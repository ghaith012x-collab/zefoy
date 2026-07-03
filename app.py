from flask import Flask, render_template, request, jsonify
from playwright.sync_api import sync_playwright
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
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        )

        try:
            # Step 1: Load Zefoy
            page.goto(ZEFOY, wait_until="networkidle")
            time.sleep(2)

            # Step 2: Solve captcha
            captcha_img = page.locator("img[src*='captcha']")
            captcha_img.wait_for(timeout=10000)
            img_bytes = captcha_img.screenshot()
            captcha_text = solve_captcha(img_bytes)

            page.locator("input[placeholder*='Enter the word'], input[type='text']").fill(captcha_text)
            page.locator("button[type='submit'], button:has-text('✓')").click()
            time.sleep(3)

            # Step 3: Click Views
            page.locator("text=Views").click()
            time.sleep(2)

            # Step 4: Paste TikTok link
            page.locator("input[placeholder*='Enter Video URL']").fill(tiktok_url)
            page.locator("button:has-text('Search')").click()
            time.sleep(5)

            # Step 5: Handle rate limit
            too_many = page.locator("text=Too many requests")
            if too_many.is_visible():
                time.sleep(10)
                page.reload()
                time.sleep(3)
                page.locator("input[placeholder*='Enter Video URL']").fill(tiktok_url)
                page.locator("button:has-text('Search')").click()
                time.sleep(5)

            # Step 6: Click the send button (the dark bar with view count)
            send_bar = page.locator("div:has-text('16,800'), button:has-text('16,800'), .btn-send")
            if send_bar.is_visible():
                send_bar.click()
                time.sleep(2)

            # Step 7: Wait for success
            page.wait_for_selector("text=Successfully", timeout=30000)
            success = page.locator("text=Successfully").inner_text()

            result_holder["status"] = "success"
            result_holder["message"] = success

        except Exception as e:
            result_holder["status"] = "error"
            result_holder["message"] = str(e)
        finally:
            browser.close()

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/send', methods=['POST'])
def send():
    url = request.json.get('url')
    if not url:
        return jsonify({"error": "No URL"}), 400

    result = {"status": "running"}
    thread = threading.Thread(target=run_bot, args=(url, result))
    thread.start()
    thread.join(timeout=120)

    return jsonify(result)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
