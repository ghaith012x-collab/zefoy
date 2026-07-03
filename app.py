from flask import Flask, request, jsonify
from playwright.sync_api import sync_playwright
import base64, io, os, time, re
from PIL import Image
import pytesseract

app = Flask(__name__)

ZEFOY_URL = "https://zefoy.com"

def solve_captcha_from_image(image_path):
    """Uses OCR to read the captcha text."""
    img = Image.open(image_path)
    # Convert to grayscale and upscale for better OCR accuracy
    img = img.convert('L').resize((img.width * 2, img.height * 2))
    text = pytesseract.image_to_string(img, config='--psm 8 -c tessedit_char_whitelist=abcdefghijklmnopqrstuvwxyz0123456789')
    return text.strip().lower()

@app.route('/api/send_views', methods=['POST'])
def send_views():
    data = request.get_json()
    tiktok_url = data.get('url')
    if not tiktok_url:
        return jsonify({"error": "No URL provided"}), 400

    with sync_playwright() as p:
        # Launch headless browser
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        try:
            # 1. Navigate to Zefoy
            page.goto(ZEFOY_URL, wait_until="networkidle")
            time.sleep(2)

            # 2. Handle the initial Captcha
            # The captcha image usually has a specific selector, inspect element to get exact ID
            # Based on your video, it's an image inside the captcha container
            captcha_img = page.locator("img[src*='captcha']") # Adjust selector if needed
            captcha_img.wait_for(timeout=10000)
            
            # Download and solve captcha
            img_bytes = captcha_img.screenshot()
            with open("captcha.png", "wb") as f:
                f.write(img_bytes)
            
            captcha_text = solve_captcha_from_image("captcha.png")
            print(f"Solved Captcha: {captcha_text}")

            # Input captcha text
            page.locator("input[placeholder*='Enter the word']").fill(captcha_text)
            page.locator("button[type='submit']").click()
            time.sleep(3)

            # 3. Navigate to Views
            page.locator("text=Views").click()
            time.sleep(2)

            # 4. Input TikTok URL
            input_field = page.locator("input[placeholder*='Enter Video URL']")
            input_field.fill(tiktok_url)
            
            # 5. Click Search
            page.locator("button:has-text('Search')").click()
            time.sleep(5)

            # 6. Auto-confirm / Click the send button if it appears
            # The site usually shows a button to send 1000 views after searching
            send_button = page.locator("button:has-text('Send')") # Adjust based on actual DOM
            if send_button.is_visible():
                send_button.click()
                time.sleep(2)
                
                # Wait for success message
                success_msg = page.locator("text=Successfully").inner_text(timeout=15000)
                browser.close()
                return jsonify({"status": "success", "message": success_msg})

            browser.close()
            return jsonify({"status": "completed", "message": "Process finished"})

        except Exception as e:
            browser.close()
            return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))
