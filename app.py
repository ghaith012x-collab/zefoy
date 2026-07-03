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

def get_page_insight(page):
    """Extract key visible elements from the page for debugging."""
    try:
        # Get all visible text
        texts = page.locator("body >> visible=true").all_inner_texts()
        visible_text = " | ".join([t.strip() for t in texts if t.strip()][:10])
        
        # Get all button texts
        buttons = page.locator("button, [role='button'], a").all()
        btn_info = []
        for b in buttons[:15]:
            try:
                txt = b.inner_text(timeout=500).strip()
                if txt and len(txt) < 100:
                    tag = b.evaluate("el => el.tagName.toLowerCase()")
                    cls = b.evaluate("el => el.className")[:50]
                    btn_info.append(f"{tag}:{txt[:30]}(class={cls})")
            except:
                pass
        
        # Get all input placeholders
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
            viewport={"width": 390, "height": 844},  # iPhone viewport like your video
            user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
        )

        try:
            emit(q, 1, "Loading zefoy.com...")
            page.goto(ZEFOY, wait_until="networkidle", timeout=30000)
            time.sleep(3)

            insight = get_page_insight(page)
            emit(q, 1, f"Page loaded: {insight['title']}", inspect=insight)

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

            # Re-inspect after captcha
            insight = get_page_insight(page)
            emit(q, 7, f"Post-captcha page state", inspect=insight)

            # CLICK THE BLUE ARROW BUTTON UNDER "Views"
            emit(q, 8, "Looking for Views arrow button...")
            views_clicked = False
            
            # Strategy 1: Find the "Views" text, then click the next button/arrow after it
            try:
                # Get all elements and find Views heading index
                all_elements = page.locator("h1, h2, h3, h4, h5, h6, p, div, button, a, span").all()
                views_idx = -1
                for i, el in enumerate(all_elements):
                    try:
                        txt = el.inner_text(timeout=500).strip()
                        if txt == "Views":
                            views_idx = i
                            emit(q, 8, f"Found 'Views' text at element index {i}")
                            break
                    except:
                        continue
                
                if views_idx >= 0:
                    # Look for clickable elements after Views
                    for j in range(views_idx + 1, min(views_idx + 5, len(all_elements))):
                        el = all_elements[j]
                        try:
                            tag = el.evaluate("e => e.tagName.toLowerCase()")
                            txt = el.inner_text(timeout=500).strip()
                            emit(q, 8, f"Checking element {j}: <{tag}> '{txt[:20]}'")
                            
                            if tag in ["button", "a"] or el.is_visible(timeout=1000):
                                # Check if it looks like an arrow button (blue, has arrow icon, etc)
                                is_clickable = el.is_enabled(timeout=500) if hasattr(el, 'is_enabled') else True
                                if is_clickable:
                                    el.click()
                                    views_clicked = True
                                    emit(q, 9, f"Clicked element after Views: <{tag}> '{txt[:30]}'")
                                    break
                        except Exception as e:
                            emit(q, 8, f"Element {j} error: {str(e)[:50]}")
                            continue
            except Exception as e:
                emit(q, 8, f"Views search error: {str(e)[:100]}")

            # Strategy 2: Direct selectors for arrow buttons
            if not views_clicked:
                arrow_selectors = [
                    "button:has(> svg)",  # Button containing SVG arrow
                    "button:has(> i)",    # Button containing icon
                    "a:has(> svg)",
                    "a:has(> i)",
                    "[class*='arrow']",
                    "[class*='views'] button",
                    "[class*='views'] a",
                    "button[class*='btn']",
                    "a[class*='btn']",
                    "button",
                    "a"
                ]
                for sel in arrow_selectors:
                    try:
                        elements = page.locator(sel).all()
                        emit(q, 10, f"Trying selector '{sel}' — found {len(elements)} elements")
                        for el in elements:
                            try:
                                txt = el.inner_text(timeout=500).strip()
                                # Look for arrow-like content or empty text (icon buttons often have no text)
                                if not txt or "→" in txt or ">" in txt or "arrow" in txt.lower():
                                    if el.is_visible(timeout=2000):
                                        el.click()
                                        views_clicked = True
                                        emit(q, 11, f"Clicked arrow button with selector '{sel}', text='{txt}'")
                                        break
                            except:
                                continue
                        if views_clicked:
                            break
                    except Exception as e:
                        emit(q, 10, f"Selector '{sel}' failed: {str(e)[:60]}")
                        continue

            # Strategy 3: XPath — find Views text then following sibling button
            if not views_clicked:
                try:
                    emit(q, 12, "Trying XPath strategy...")
                    # Find element containing "Views" then get next button
                    views_el = page.locator("text=Views").first
                    if views_el.is_visible(timeout=3000):
                        # Try to find parent container then button within it
                        parent = views_el.locator("xpath=../..")
                        btn = parent.locator("button, a").first
                        if btn.is_visible(timeout=3000):
                            btn.click()
                            views_clicked = True
                            emit(q, 13, "Clicked button in Views container")
                except Exception as e:
                    emit(q, 12, f"XPath failed: {str(e)[:80]}")

            if not views_clicked:
                # Final debug dump
                insight = get_page_insight(page)
                emit(q, 99, "Could not find Views button. Full page inspection:", inspect=insight, done=True)
                browser.close()
                return

            time.sleep(3)

            # Re-inspect on Views page
            insight = get_page_insight(page)
            emit(q, 14, f"Views page loaded", inspect=insight)

            # Fill TikTok URL
            emit(q, 15, "Looking for video URL input...")
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
                        emit(q, 16, f"URL entered")
                        break
                except:
                    continue

            if not url_filled:
                insight = get_page_insight(page)
                emit(q, 99, "Could not find URL input. Page inspection:", inspect=insight, done=True)
                browser.close()
                return

            # Click Search
            emit(q, 17, "Clicking Search...")
            search_clicked = False
            search_selectors = ["button:has-text('Search')", "input[type='submit']", "button"]
            for sel in search_selectors:
                try:
                    btn = page.locator(sel).first
                    if btn.is_visible(timeout=3000):
                        btn.click()
                        search_clicked = True
                        emit(q, 18, "Search clicked")
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
                    emit(q, 19, "Rate limited — waiting 12s...")
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
            emit(q, 20, "Looking for send button...")
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
                        emit(q, 21, "Send button clicked — sending views...")
                        break
                except:
                    continue

            if not send_clicked:
                emit(q, 99, "Could not find send button", done=True)
                browser.close()
                return

            time.sleep(3)

            # Wait for success
            emit(q, 22, "Waiting for confirmation...")
            success_found = False
            try:
                page.wait_for_selector("text=Successfully", timeout=30000)
                msg = page.locator("text=Successfully").first.inner_text(timeout=5000)
                emit(q, 23, msg, done=True)
                success_found = True
            except:
                pass

            if not success_found:
                fallback = ["sent", "complete", "done", "views", "1000"]
                for txt in fallback:
                    try:
                        el = page.locator(f"text={txt}").first
                        if el.is_visible(timeout=2000):
                            emit(q, 23, "Views sent successfully", done=True)
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
