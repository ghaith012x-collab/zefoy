from flask import Flask, render_template, request, jsonify, Response, make_response
from playwright.sync_api import sync_playwright
import threading, time, re, sys, difflib, json, base64, os
from PIL import Image, ImageOps
from io import BytesIO
from collections import Counter, deque
import numpy as np
import requests
import hashlib
import uuid
import random
import subprocess
import tempfile

# Thread-local tab prefix for log messages
_tab_prefix = threading.local()

# Global limit: max 5 Chromium browsers across ALL sessions at once
MAX_GLOBAL_BROWSERS = 5
_browser_semaphore = threading.Semaphore(MAX_GLOBAL_BROWSERS)
_active_browsers = 0
_active_browsers_lock = threading.Lock()


# Random User-Agent pool for QQTube HTTP requests
_QQTUBE_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.5; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
]

def _random_ua():
    return random.choice(_QQTUBE_USER_AGENTS)

app = Flask(__name__)
ZEFOY = "https://zefoy.com"

# Proxy support: set PROXY_URL env var, or leave empty to auto-use Tor (built into container)
# Supports formats: http://user:pass@host:port  OR  host:port:user:pass  OR  host:port
# Set USE_TOR=false to disable Tor fallback
def _parse_proxy(raw):
    raw = raw.strip()
    if not raw:
        return ""
    if raw.startswith("http://") or raw.startswith("https://") or raw.startswith("socks"):
        return raw
    parts = raw.split(":")
    if len(parts) == 4:  # host:port:user:pass
        return f"http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
    if len(parts) == 2:  # host:port
        return f"http://{parts[0]}:{parts[1]}"
    return raw  # try as-is

PROXY_URL = _parse_proxy(os.environ.get("PROXY_URL", ""))
USE_TOR = os.environ.get("USE_TOR", "true").strip().lower() in ("true", "1", "yes")

# ═══════════════════════════════════════════════════════════════
#  reCAPTCHA v2 SOLVER  (CapSolver primary, 2Captcha fallback)
#  Set ONE of these env vars to enable automatic solving:
#    CAPSOLVER_API_KEY   – https://capsolver.com  (~$0.8/1k solves)
#    TWOCAPTCHA_API_KEY  – https://2captcha.com   (~$3/1k solves)
# ═══════════════════════════════════════════════════════════════
CAPSOLVER_API_KEY = os.environ.get("CAPSOLVER_API_KEY", "").strip()
TWOCAPTCHA_API_KEY = os.environ.get("TWOCAPTCHA_API_KEY", "").strip()


def solve_recaptcha_v2(site_key, page_url, session=None, timeout=180, proxy=None):
    """Solve reCAPTCHA v2 and return the g-recaptcha-response token, or ''."""
    if CAPSOLVER_API_KEY:
        token = _solve_capsolver(site_key, page_url, session, timeout)
        if token:
            return token

    if TWOCAPTCHA_API_KEY:
        token = _solve_2captcha(site_key, page_url, session, timeout)
        if token:
            return token

    # Free solver: audio challenge + Google speech recognition (no API key)
    token = _solve_recaptcha_free(site_key, page_url, proxy=proxy, session=session)
    if token:
        return token

    return ""


def _solve_capsolver(site_key, page_url, session, timeout):
    """CapSolver: create task → poll result."""
    try:
        if session:
            session.log("\U0001f9e9 Solving reCAPTCHA via CapSolver...")
        r = requests.post("https://api.capsolver.com/createTask", json={
            "clientKey": CAPSOLVER_API_KEY,
            "task": {
                "type": "ReCaptchaV2TaskProxyLess",
                "websiteURL": page_url,
                "websiteKey": site_key,
            }
        }, timeout=30)
        data = r.json()
        if data.get("errorId", 1) != 0:
            if session:
                session.log(f"\u26a0\ufe0f CapSolver error: {data.get('errorDescription', '?')[:80]}")
            return None
        task_id = data.get("taskId")
        if not task_id:
            return None

        start = time.time()
        while time.time() - start < timeout:
            time.sleep(3)
            r = requests.post("https://api.capsolver.com/getTaskResult", json={
                "clientKey": CAPSOLVER_API_KEY,
                "taskId": task_id,
            }, timeout=30)
            res = r.json()
            if res.get("status") == "ready":
                return res.get("solution", {}).get("gRecaptchaResponse") or None
            if res.get("errorId", 0) != 0:
                if session:
                    session.log(f"\u26a0\ufe0f CapSolver poll error: {res.get('errorDescription', '?')[:80]}")
                return None
        if session:
            session.log("\u26a0\ufe0f CapSolver timed out")
        return None
    except Exception as e:
        if session:
            session.log(f"\u26a0\ufe0f CapSolver exception: {str(e)[:60]}")
        return None


def _solve_2captcha(site_key, page_url, session, timeout):
    """2Captcha: submit → poll result."""
    try:
        if session:
            session.log("\U0001f9e9 Solving reCAPTCHA via 2Captcha...")
        r = requests.post("https://2captcha.com/in.php", data={
            "key": TWOCAPTCHA_API_KEY,
            "method": "userrecaptcha",
            "googlekey": site_key,
            "pageurl": page_url,
            "json": 1,
        }, timeout=30)
        data = r.json()
        if data.get("status") != 1:
            if session:
                session.log(f"\u26a0\ufe0f 2Captcha error: {data.get('request', '?')[:80]}")
            return None
        captcha_id = data.get("request")

        time.sleep(15)  # 2Captcha needs an initial processing delay
        start = time.time()
        while time.time() - start < timeout:
            r = requests.get("https://2captcha.com/res.php", params={
                "key": TWOCAPTCHA_API_KEY,
                "action": "get",
                "id": captcha_id,
                "json": 1,
            }, timeout=30)
            res = r.json()
            if res.get("status") == 1:
                return res.get("request", "") or None
            if res.get("request") != "CAPCHA_NOT_READY":
                if session:
                    session.log(f"\u26a0\ufe0f 2Captcha error: {res.get('request', '?')[:80]}")
                return None
            time.sleep(5)
        if session:
            session.log("\u26a0\ufe0f 2Captcha timed out")
        return None
    except Exception as e:
        if session:
            session.log(f"\u26a0\ufe0f 2Captcha exception: {str(e)[:60]}")
        return None


# ═══════════════════════════════════════════════════════════════
#  FREE reCAPTCHA v2 SOLVER  (audio challenge + speech recognition)
#  Same technique as the Buster browser extension — zero cost.
#  Requires: ffmpeg (apt), SpeechRecognition (pip)
# ═══════════════════════════════════════════════════════════════
def _solve_recaptcha_free(site_key, page_url, proxy=None, session=None, max_attempts=3):
    """
    Free reCAPTCHA v2 solver — audio challenge + Google speech-to-text.
    No API keys needed.

    KEY INSIGHT: Solve on a CLEAN IP (no Tor) so Google serves the audio
    challenge. The token is domain-bound, not IP-bound, so it works when
    submitted through Tor in the QQTube API call.
    """
    global _active_browsers

    try:
        import speech_recognition as sr
    except ImportError:
        if session:
            session.log("⚠️ SpeechRecognition not installed")
        return ""

    for attempt in range(1, max_attempts + 1):
        browser = None
        got_slot = False
        try:
            if session:
                session.log(f"🧩 Free reCAPTCHA solver (attempt {attempt}/{max_attempts})...")

            # Acquire browser slot
            if not _browser_semaphore.acquire(timeout=1):
                if session:
                    session.log("⏳ Waiting for browser slot...")
                _browser_semaphore.acquire()
            got_slot = True
            with _active_browsers_lock:
                _active_browsers += 1

            with sync_playwright() as pw:
                launch_opts = {
                    "headless": True,
                    "args": [
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-gpu",
                        "--single-process",
                        "--js-flags=--max-old-space-size=128",
                        "--disable-blink-features=AutomationControlled",
                        "--disable-features=IsolateOrigins,site-per-process",
                    ],
                }
                # IMPORTANT: Do NOT use Tor proxy for captcha solving.
                # Google blocks audio challenges on Tor exit IPs.
                # We solve on clean IP; token still works when submitted via Tor.
                # (reCAPTCHA tokens are domain-validated, not IP-validated)

                browser = pw.chromium.launch(**launch_opts)
                ctx = browser.new_context(
                    user_agent=_random_ua(),
                    viewport={"width": 1920, "height": 1080},
                    locale="en-US",
                )
                page = ctx.new_page()
                page.set_default_timeout(30000)

                # Minimal HTML page with reCAPTCHA widget.
                # Route-intercepted on QQTube domain so site key
                # passes Google's domain validation.
                captcha_html = (
                    '<!DOCTYPE html><html><head><title>Loading</title>'
                    '<script src="https://www.google.com/recaptcha/api.js"'
                    ' async defer></script></head><body>'
                    f'<div class="g-recaptcha" data-sitekey="{site_key}"'
                    ' data-callback="onSolved"></div>'
                    '<script>function onSolved(t){document.title="SOLVED:"+t;}'
                    '</script></body></html>'
                )

                def _intercept(route):
                    """Serve our minimal captcha page for QQTube domain requests."""
                    if route.request.resource_type == "document":
                        route.fulfill(
                            status=200,
                            content_type="text/html",
                            body=captcha_html,
                        )
                    else:
                        # Allow all other resources (Google reCAPTCHA scripts, etc.)
                        route.continue_()

                page.route("https://www.qqtube.com/**", _intercept)

                try:
                    page.goto("https://www.qqtube.com/free-tiktok-likes",
                              wait_until="domcontentloaded", timeout=30000)
                except Exception:
                    pass  # partial load OK

                # Wait for reCAPTCHA anchor iframe
                try:
                    page.wait_for_selector(
                        "iframe[src*='recaptcha/api2/anchor']", timeout=20000
                    )
                except Exception:
                    if session:
                        session.log("⚠️ reCAPTCHA widget didn't load")
                    continue

                time.sleep(2)

                # ── Click the checkbox ──
                anchor = page.frame_locator("iframe[src*='recaptcha/api2/anchor']")
                try:
                    anchor.locator("#recaptcha-anchor").click(timeout=10000)
                except Exception:
                    if session:
                        session.log("⚠️ Couldn't click checkbox")
                    continue

                time.sleep(3)

                # Lucky pass? (no challenge shown)
                if page.title().startswith("SOLVED:"):
                    token = page.title().split("SOLVED:", 1)[1]
                    if session:
                        session.log("✅ reCAPTCHA passed instantly!")
                    return token

                # ── Switch to audio challenge ──
                try:
                    page.wait_for_selector(
                        "iframe[src*='recaptcha/api2/bframe']", timeout=10000
                    )
                except Exception:
                    if session:
                        session.log("⚠️ Challenge frame didn't appear")
                    continue

                bframe = page.frame_locator("iframe[src*='recaptcha/api2/bframe']")

                try:
                    bframe.locator("#recaptcha-audio-button").click(timeout=10000)
                except Exception:
                    if session:
                        session.log("⚠️ No audio option available")
                    continue

                time.sleep(3)

                # Check if Google blocked audio
                try:
                    err_msg = bframe.locator(".rc-audiochallenge-error-message").text_content(timeout=3000) or ""
                    if err_msg and ("automated" in err_msg.lower() or "try again" in err_msg.lower()):
                        if session:
                            session.log(f"⚠️ Audio blocked: {err_msg[:60]}")
                        continue
                except Exception:
                    pass  # no error = good

                # ── Get audio URL ──
                audio_url = None
                for sel in (".rc-audiochallenge-tdownload-link", "#audio-source"):
                    try:
                        attr = "href" if "link" in sel else "src"
                        val = bframe.locator(sel).get_attribute(attr, timeout=8000)
                        if val:
                            audio_url = val
                            break
                    except Exception:
                        continue

                if not audio_url:
                    # Try JS extraction from the audio element
                    try:
                        bframe_handle = page.frame("recaptcha/api2/bframe")
                        if bframe_handle:
                            audio_url = bframe_handle.evaluate(
                                """() => {
                                    const a = document.querySelector('#audio-source');
                                    if (a && a.src) return a.src;
                                    const dl = document.querySelector('.rc-audiochallenge-tdownload-link');
                                    if (dl && dl.href) return dl.href;
                                    const audio = document.querySelector('audio source');
                                    if (audio && audio.src) return audio.src;
                                    return null;
                                }"""
                            )
                    except Exception:
                        pass

                if not audio_url:
                    if session:
                        session.log("⚠️ No audio URL found")
                    continue

                if session:
                    session.log("🔊 Got audio challenge, transcribing...")

                # ── Download audio DIRECTLY (clean IP, no proxy) ──
                import tempfile
                import subprocess as sp

                try:
                    audio_bytes = requests.get(audio_url, timeout=30).content
                except Exception as e:
                    if session:
                        session.log(f"⚠️ Audio download failed: {str(e)[:50]}")
                    continue

                if len(audio_bytes) < 1000:
                    if session:
                        session.log("⚠️ Audio too small — likely error page")
                    continue

                # ── Convert MP3 → WAV → transcribe ──
                mp3_path = tempfile.mktemp(suffix=".mp3")
                wav_path = mp3_path.replace(".mp3", ".wav")
                text = ""
                try:
                    with open(mp3_path, "wb") as f:
                        f.write(audio_bytes)

                    conv = sp.run(
                        ["ffmpeg", "-y", "-loglevel", "error",
                         "-i", mp3_path, "-ar", "16000", "-ac", "1", wav_path],
                        capture_output=True, timeout=30,
                    )
                    if conv.returncode != 0:
                        if session:
                            session.log("⚠️ ffmpeg conversion failed")
                        continue

                    rec = sr.Recognizer()
                    with sr.AudioFile(wav_path) as src:
                        audio_data = rec.record(src)
                        text = rec.recognize_google(audio_data).strip().lower()

                    if session:
                        session.log(f"🔊 Heard: '{text}'")
                except sr.UnknownValueError:
                    if session:
                        session.log("⚠️ Couldn't understand audio")
                    continue
                except sr.RequestError as e:
                    if session:
                        session.log(f"⚠️ Speech API error: {str(e)[:60]}")
                    continue
                finally:
                    for fp in (mp3_path, wav_path):
                        try:
                            os.unlink(fp)
                        except Exception:
                            pass

                if not text:
                    if session:
                        session.log("⚠️ Empty transcription")
                    continue

                # ── Type the answer and submit ──
                try:
                    response_input = bframe.locator("#audio-response")
                    response_input.fill(text, timeout=5000)
                    time.sleep(1)
                    bframe.locator("#recaptcha-verify-button").click(timeout=5000)
                except Exception as e:
                    if session:
                        session.log(f"⚠️ Submit failed: {str(e)[:50]}")
                    continue

                time.sleep(4)

                # Check if solved
                if page.title().startswith("SOLVED:"):
                    token = page.title().split("SOLVED:", 1)[1]
                    if session:
                        session.log("✅ reCAPTCHA solved!")
                    return token

                # Check via checkbox state
                try:
                    checked = anchor.locator("#recaptcha-anchor[aria-checked='true']").count()
                    if checked > 0:
                        # Extract token from page
                        tkn = page.evaluate(
                            "document.querySelector('[name=g-recaptcha-response]')?.value || ''"
                        )
                        if tkn:
                            if session:
                                session.log("✅ reCAPTCHA solved!")
                            return tkn
                except Exception:
                    pass

                if session:
                    session.log("⚠️ Answer may be wrong, retrying...")

        except Exception as e:
            if session:
                session.log(f"⚠️ Solver error: {str(e)[:80]}")
        finally:
            if browser:
                try:
                    browser.close()
                except Exception:
                    pass
            if got_slot:
                with _active_browsers_lock:
                    _active_browsers -= 1
                _browser_semaphore.release()
            import gc
            gc.collect()

    if session:
        session.log("⚠️ Free solver exhausted — will rotate IP")
    return ""
def _rotate_tor_circuit():
    """Send NEWNYM signal to Tor control port to get a new IP."""
    try:
        import socket
        cookie = b""
        try:
            with open("/tmp/tor-data/control_auth_cookie", "rb") as f:
                cookie = f.read()
        except:
            pass
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(5)
        s.connect(("127.0.0.1", 9060))
        resp = s.recv(256)
        cookie_hex = cookie.hex()
        auth_cmd = "AUTHENTICATE " + cookie_hex + "\r\n"
        s.send(auth_cmd.encode())
        resp = s.recv(256)
        if b"250" not in resp:
            s.close()
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(5)
            s.connect(("127.0.0.1", 9060))
            s.recv(256)
            s.send(b'AUTHENTICATE ""\r\n')
            resp = s.recv(256)
        s.send(b"SIGNAL NEWNYM\r\n")
        resp = s.recv(256)
        success = b"250" in resp
        s.close()
        if success:
            print("[TOR] Circuit rotated - new IP!", flush=True)
        return success
    except Exception as e:
        print(f"[TOR] Circuit rotation failed: {e}", flush=True)
        return False


# ═══════════════════════════════════════════════════════════════
#  SERVICES
# ═══════════════════════════════════════════════════════════════

SERVICES = {
    "views": {
        "name": "Views",
        "emoji": "👁️",
        "button_class": "t-views-button",
        "menu_class": "t-views-menu",
        "unit": "views",
    },
    "hearts": {
        "name": "Hearts",
        "emoji": "❤️",
        "button_class": "t-hearts-button",
        "menu_class": "t-hearts-menu",
        "unit": "hearts",
    },
    "shares": {
        "name": "Shares",
        "emoji": "🔄",
        "button_class": "t-shares-button",
        "menu_class": "t-shares-menu",
        "unit": "shares",
    },
    "favorites": {
        "name": "Favorites",
        "emoji": "⭐",
        "button_class": "t-favorites-button",
        "menu_class": "t-favorites-menu",
        "unit": "favorites",
    },
    "followers": {
        "name": "Followers",
        "emoji": "👥",
        "button_class": "t-followers-button",
        "menu_class": "t-followers-menu",
        "unit": "followers",
    },
    "qqtube_likes": {
        "name": "QQTube Likes",
        "emoji": "💜",
        "button_class": "",
        "menu_class": "",
        "unit": "likes",
        "engine": "qqtube",
    },
}

# CSS selector that matches ANY service button (used for captcha-solved check)
ANY_SERVICE_BUTTON = ", ".join(f".{s['button_class']}" for s in SERVICES.values())


# ═══════════════════════════════════════════════════════════════
#  DICTIONARY
# ═══════════════════════════════════════════════════════════════

WORD_LIST = []

def load_dictionary():
    global WORD_LIST
    try:
        with open('/usr/share/dict/words') as f:
            WORD_LIST = [w.strip().lower() for w in f if 2 <= len(w.strip()) <= 10]
        print(f"[BOT] Dictionary loaded: {len(WORD_LIST)} words", flush=True)
    except:
        try:
            import urllib.request
            url = "https://raw.githubusercontent.com/dwyl/english-words/master/words_alpha.txt"
            data = urllib.request.urlopen(url, timeout=10).read().decode()
            WORD_LIST = [w.strip().lower() for w in data.splitlines() if 2 <= len(w.strip()) <= 10]
            print(f"[BOT] Online dictionary loaded: {len(WORD_LIST)} words", flush=True)
        except Exception as e:
            print(f"[BOT] Dictionary load failed: {e}", flush=True)

threading.Thread(target=load_dictionary, daemon=True).start()


# ═══════════════════════════════════════════════════════════════
#  CAPTCHA SOLVER
# ═══════════════════════════════════════════════════════════════

def remove_small_components(binary_arr, min_size=30):
    h, w = binary_arr.shape
    visited = np.zeros((h, w), dtype=bool)
    result = np.zeros((h, w), dtype=np.uint8)
    for y in range(h):
        for x in range(w):
            if binary_arr[y, x] == 1 and not visited[y, x]:
                component = []
                q = deque([(y, x)])
                visited[y, x] = True
                while q:
                    cy, cx = q.popleft()
                    component.append((cy, cx))
                    for dy, dx in [(-1,0),(1,0),(0,-1),(0,1),(-1,-1),(-1,1),(1,-1),(1,1)]:
                        ny, nx = cy + dy, cx + dx
                        if 0 <= ny < h and 0 <= nx < w and binary_arr[ny, nx] == 1 and not visited[ny, nx]:
                            visited[ny, nx] = True
                            q.append((ny, nx))
                if len(component) >= min_size:
                    for cy, cx in component:
                        result[cy, cx] = 1
    return result


def solve_captcha(img_bytes):
    import pytesseract
    from PIL import ImageFilter, ImageEnhance
    img = Image.open(BytesIO(img_bytes))
    if img.mode != 'RGB':
        img = img.convert('RGB')
    gray = ImageOps.grayscale(img)
    w, h = gray.size
    # Upscale 4x for better OCR accuracy
    big = gray.resize((w * 4, h * 4), Image.LANCZOS)
    arr = np.array(big)

    results = []

    def run_ocr(pil_img, tag=""):
        """Run tesseract with multiple PSM modes and collect results."""
        found = []
        for psm in [7, 8, 13, 6]:
            config = f'--psm {psm} --oem 3 -c tessedit_char_whitelist=abcdefghijklmnopqrstuvwxyz'
            try:
                text = pytesseract.image_to_string(pil_img, config=config).strip()
                text = re.sub(r'[^a-z]', '', text.lower())
                if 3 <= len(text) <= 12:
                    found.append(text)
            except Exception as ocr_err:
                print(f"[BOT] OCR {tag} psm={psm} error: {ocr_err}", flush=True)
        return found

    # Strategy 1: Direct thresholds (dark text on light bg)
    for thresh_val in [100, 120, 140, 160, 180, 200]:
        binary_img = Image.fromarray(((arr >= thresh_val) * 255).astype('uint8'))
        results.extend(run_ocr(binary_img, f"thresh-{thresh_val}"))

    # Strategy 2: Inverted thresholds (light text on dark bg)
    for thresh_val in [100, 130, 160]:
        binary_img = Image.fromarray(((arr < thresh_val) * 255).astype('uint8'))
        results.extend(run_ocr(binary_img, f"inv-{thresh_val}"))

    # Strategy 3: Dot/noise removal + threshold
    for thresh_val in [110, 130, 150, 170]:
        binary = (arr < thresh_val).astype(np.uint8)
        cleaned = remove_small_components(binary, min_size=25)
        clean_img = Image.fromarray(((1 - cleaned) * 255).astype('uint8'))
        results.extend(run_ocr(clean_img, f"clean-{thresh_val}"))

    # Strategy 4: Contrast enhancement + threshold
    try:
        enhanced = ImageEnhance.Contrast(big).enhance(3.0)
        enhanced_arr = np.array(enhanced)
        for thresh_val in [120, 150, 180]:
            binary_img = Image.fromarray(((enhanced_arr >= thresh_val) * 255).astype('uint8'))
            results.extend(run_ocr(binary_img, f"contrast-{thresh_val}"))
    except:
        pass

    # Strategy 5: Median filter (removes salt-and-pepper noise) + threshold
    try:
        median = big.filter(ImageFilter.MedianFilter(size=3))
        median_arr = np.array(median)
        for thresh_val in [120, 150]:
            binary_img = Image.fromarray(((median_arr >= thresh_val) * 255).astype('uint8'))
            results.extend(run_ocr(binary_img, f"median-{thresh_val}"))
    except:
        pass

    # Strategy 6: Morphological closing (fills gaps in characters)
    try:
        for thresh_val in [130, 160]:
            binary = (arr < thresh_val).astype(np.uint8)
            cleaned = remove_small_components(binary, min_size=20)
            # Dilate then erode (close operation) to connect broken strokes
            from PIL import ImageFilter
            tmp_img = Image.fromarray((cleaned * 255).astype('uint8'))
            tmp_img = tmp_img.filter(ImageFilter.MaxFilter(3))  # dilate
            tmp_img = tmp_img.filter(ImageFilter.MinFilter(3))  # erode
            inv_img = ImageOps.invert(tmp_img)
            results.extend(run_ocr(inv_img, f"morph-{thresh_val}"))
    except:
        pass

    print(f"[BOT] OCR candidates: {results}", flush=True)
    if not results:
        return ""

    # Score candidates using dictionary matching
    if WORD_LIST:
        word_set = set(WORD_LIST)
        # First: check if any candidate is an exact dictionary word
        exact_matches = [r for r in results if r in word_set]
        if exact_matches:
            best = Counter(exact_matches).most_common(1)[0][0]
            print(f"[BOT] OCR exact match: '{best}' (count={Counter(exact_matches)[best]})", flush=True)
            return best

        # Second: fuzzy match each unique candidate and pick the best
        best_match = None
        best_score = 0
        best_raw = ""
        for candidate in set(results):
            freq = results.count(candidate)
            matches = difflib.get_close_matches(candidate, WORD_LIST, n=1, cutoff=0.6)
            if matches:
                # Score = frequency × similarity
                sim = difflib.SequenceMatcher(None, candidate, matches[0]).ratio()
                score = freq * sim
                if score > best_score:
                    best_score = score
                    best_match = matches[0]
                    best_raw = candidate
        if best_match:
            print(f"[BOT] OCR: '{best_raw}' → '{best_match}' (score={best_score:.2f})", flush=True)
            return best_match

    # Fallback: most common raw OCR result
    most_common = Counter(results).most_common(1)[0][0]
    print(f"[BOT] OCR fallback (no dict match): '{most_common}'", flush=True)
    return most_common


def parse_wait_time(text):
    mins = re.search(r'(\d+)\s*minute', text)
    secs = re.search(r'(\d+)\s*second', text)
    total = 0
    if mins: total += int(mins.group(1)) * 60
    if secs: total += int(secs.group(1))
    return total


# ═══════════════════════════════════════════════════════════════
#  SESSION MANAGEMENT
# ═══════════════════════════════════════════════════════════════

class Session:
    _counter = 0
    _lock = threading.Lock()

    def __init__(self, video_url, service="views", num_tabs=1):
        with Session._lock:
            Session._counter += 1
            self.id = Session._counter
        self.video_url = video_url
        self.service = service  # key into SERVICES dict
        self.num_tabs = max(1, min(num_tabs, 3))  # clamp 1-3
        self.status = "starting"
        self.total_count = 0
        self.cycles = 0
        self.logs = []       # List of log message strings
        self.countdown = ""  # Current countdown text (updates in-place on frontend)
        self.stop_event = threading.Event()
        self.thread = None
        self.count_lock = threading.Lock()
        self.active_tabs = 0

    @property
    def svc(self):
        return SERVICES.get(self.service, SERVICES["views"])

    def log(self, msg):
        pre = getattr(_tab_prefix, 'value', '')
        full = f"{pre}{msg}"
        self.logs.append(full)
        self.countdown = ""
        print(f"[S{self.id}] {full}", flush=True)

    def set_countdown(self, text):
        self.countdown = text

    def add_count(self, count):
        with self.count_lock:
            self.total_count += count
            return self.total_count

    def add_cycle(self):
        with self.count_lock:
            self.cycles += 1
            return self.cycles

    def to_dict(self):
        return {
            "id": self.id,
            "url": self.video_url,
            "service": self.service,
            "serviceName": self.svc["name"],
            "serviceEmoji": self.svc["emoji"],
            "status": self.status,
            "count": self.total_count,
            "unit": self.svc["unit"],
            "cycles": self.cycles,
            "countdown": self.countdown,
            "numTabs": self.num_tabs,
            "activeTabs": self.active_tabs,
        }


sessions = {}
sessions_lock = threading.Lock()


# ═══════════════════════════════════════════════════════════════
#  BOT LOOP
# ═══════════════════════════════════════════════════════════════

def run_session(session):
    """Orchestrates one or more tabs for this session."""
    session.status = "running"
    svc_name = session.svc["name"]
    nt = session.num_tabs
    engine = session.svc.get("engine", "zefoy")
    tab_func = run_qqtube_tab if engine == "qqtube" else run_tab

    if nt <= 1:
        session.log(f"🚀 Launching browser ({svc_name} mode)...")
        tab_func(session, 0)
    else:
        session.log(f"🚀 Launching {nt} tabs ({svc_name} mode)...")
        threads = []
        for tab_id in range(nt):
            t = threading.Thread(target=tab_func, args=(session, tab_id), daemon=True)
            t.start()
            threads.append(t)
            time.sleep(5)  # stagger launches to reduce memory spikes
        for t in threads:
            t.join()

    if session.status == "running":
        session.log("🛑 Session stopped.")
        session.status = "stopped"


def run_tab(session, tab_id):
    """Runs a single bot tab — each gets its own browser + Tor circuit.
    Wrapped in an outer retry loop so it NEVER permanently dies from crashes."""
    import gc
    svc = session.svc
    svc_name = svc["name"]
    btn_cls = svc["button_class"]
    menu_cls = svc["menu_class"]
    unit = svc["unit"]
    emoji = svc["emoji"]
    multi = session.num_tabs > 1

    # Set thread-local prefix for log messages
    _tab_prefix.value = f"[T{tab_id+1}] " if multi else ""

    MAX_FULL_RESTARTS = 100  # effectively infinite — keep retrying forever
    backoff = 5

    with session.count_lock:
        session.active_tabs += 1

    try:
        for full_restart in range(MAX_FULL_RESTARTS):
            if session.stop_event.is_set():
                return

            if full_restart > 0:
                wait_time = min(int(backoff), 30)
                session.log(f"\u267b\ufe0f Full restart #{full_restart} (waiting {wait_time}s)...")
                time.sleep(wait_time)
                backoff = min(backoff * 1.5, 30)
                gc.collect()
            else:
                if multi:
                    session.log(f"\U0001f680 Starting tab...")

            browser = None
            page = None

            # Acquire a global browser slot (blocks if all 3 are in use)
            got_slot = False
            try:
                if not _browser_semaphore.acquire(timeout=1):
                    session.log("⏳ Waiting for browser slot (max 3 globally)...")
                    _browser_semaphore.acquire()  # block until available
                got_slot = True
                with _active_browsers_lock:
                    global _active_browsers
                    _active_browsers += 1
                    session.log(f"🟢 Browser slot acquired ({_active_browsers}/{MAX_GLOBAL_BROWSERS} in use)")
            except Exception:
                pass  # if acquire fails, still try to launch

            try:
                with sync_playwright() as p:
                    launch_opts = {
                        "headless": True,
                        "args": [
                            "--no-sandbox",
                            "--disable-dev-shm-usage",
                            "--disable-gpu",
                            "--disable-extensions",
                            "--disable-background-networking",
                            "--disable-default-apps",
                            "--disable-sync",
                            "--disable-translate",
                            "--no-first-run",
                            "--disable-background-timer-throttling",
                            "--disable-renderer-backgrounding",
                            "--disable-backgrounding-occluded-windows",
                            "--disable-component-extensions-with-background-pages",
                            "--disable-features=TranslateUI",
                            "--renderer-process-limit=1",
                            "--js-flags=--max-old-space-size=128",
                            "--disable-software-rasterizer",
                            "--disable-logging",
                            "--disable-hang-monitor",
                            "--single-process",
                            "--disable-ipc-flooding-protection",
                            "--memory-pressure-off",
                        ],
                    }
                    if USING_TOR:
                        tor_port = 9050 + (tab_id % 10)
                        if full_restart == 0:
                            session.log(f"\U0001f9c5 Routing through Tor (port {tor_port})...")
                        import os
                        for _tw in range(60):
                            if os.path.exists("/tmp/tor_ready"):
                                break
                            if _tw == 0:
                                session.log("\u23f3 Waiting for Tor to bootstrap...")
                            time.sleep(1)
                        launch_opts["proxy"] = {
                            "server": f"socks5://127.0.0.1:{tor_port}",
                        }
                    elif PROXY_URL:
                        if full_restart == 0:
                            session.log(f"\U0001f310 Using proxy: {PROXY_URL.split('@')[-1] if '@' in PROXY_URL else PROXY_URL}")
                        launch_opts["proxy"] = {"server": PROXY_URL}

                    browser = p.chromium.launch(**launch_opts)
                    page = browser.new_page(viewport={"width": 800, "height": 600})
                    page.on("dialog", lambda d: d.accept())

                    def _safe_check(pg):
                        """Check if page is alive. Returns True if OK, False if crashed."""
                        try:
                            pg.title()
                            return True
                        except:
                            return False

                    # \u2500\u2500 Load zefoy \u2500\u2500
                    session.log("\U0001f310 Loading zefoy.com...")
                    page.goto(ZEFOY, wait_until="domcontentloaded", timeout=60000)
                    time.sleep(5)

                    if not _safe_check(page):
                        session.log("\U0001f4a5 Page crashed on load, restarting...")
                        continue

                    # \u2500\u2500 Check page / Solve captcha \u2500\u2500
                    session.log("\U0001f510 Checking for captcha...")

                    captcha_detected = False
                    page_ready = False

                    for page_attempt in range(10):
                        if session.stop_event.is_set():
                            return

                        if not _safe_check(page):
                            session.log("\U0001f4a5 Crashed during page check, restarting...")
                            break

                        try:
                            page_title = page.title().lower()
                            page_text = page.inner_text("body")[:200].lower()
                            if "502" in page_title or "502 bad gateway" in page_text:
                                session.log(f"\U0001f534 Zefoy is down (502 error), retrying ({page_attempt + 1}/10)...")
                                time.sleep(10 + page_attempt * 3)
                                page.reload(wait_until="domcontentloaded")
                                time.sleep(5)
                                continue
                            if "503" in page_title or "cloudflare" in page_text or "just a moment" in page_text:
                                session.log(f"\U0001f534 Zefoy loading/Cloudflare check ({page_attempt + 1}/10)...")
                                time.sleep(10 + page_attempt * 3)
                                page.reload(wait_until="domcontentloaded")
                                time.sleep(5)
                                continue
                        except:
                            pass

                        try:
                            page.locator("#captcha-img, .wrapper-capth, #captchatoken, img[src*=\"captcha\"], img[src*=\"CAPTCHA\"]").first.wait_for(state="visible", timeout=30000)
                            captcha_detected = True
                            break
                        except:
                            pass

                        try:
                            page.locator(ANY_SERVICE_BUTTON).first.wait_for(timeout=20000)
                            session.log("\u2705 No captcha needed \u2014 service buttons already visible")
                            page_ready = True
                            break
                        except:
                            pass

                        session.log(f"\u26a0\ufe0f Page not ready, reloading (attempt {page_attempt + 1}/10)...")
                        page.reload(wait_until="domcontentloaded")
                        time.sleep(10 + page_attempt * 3)
                    else:
                        session.log("\u26a0\ufe0f Page never became ready, restarting...")
                        continue

                    if not captcha_detected and not page_ready:
                        continue

                    if captcha_detected:
                        session.log("\U0001f510 Captcha detected, solving...")
                        captcha_solved = False
                        for captcha_attempt in range(20):
                            if session.stop_event.is_set():
                                return

                            if not _safe_check(page):
                                session.log("\U0001f4a5 Crashed during captcha, restarting...")
                                break

                            try:
                                captcha_img = page.locator("#captcha-img, img[src*='CAPTCHA'], img[src*='captcha']")
                                try:
                                    captcha_img.first.wait_for(state="visible", timeout=10000)
                                except:
                                    session.log("\u26a0\ufe0f Captcha image not loading, reloading page...")
                                    page.reload(wait_until="domcontentloaded")
                                    time.sleep(5)
                                    continue

                                session.log(f"\U0001f510 Solving captcha (attempt {captcha_attempt + 1})...")
                                time.sleep(2)
                                captcha_bytes = captcha_img.first.screenshot()
                                answer = solve_captcha(captcha_bytes)

                                if not answer:
                                    session.log("\u26a0\ufe0f OCR failed, refreshing captcha...")
                                    try: page.locator(".refresh-capthca-btn-new, [onclick*='refresh'], .captcha-refresh").first.click()
                                    except: page.reload(wait_until="domcontentloaded")
                                    time.sleep(3)
                                    continue

                                session.log(f"\U0001f524 Answer: '{answer}'")
                                captcha_input = page.locator("#captchatoken, input[name='captcha_secure'], input[placeholder*='aptcha']")
                                captcha_input.first.fill(answer)
                                time.sleep(0.5)
                                page.locator("button.submit-captcha, form .btn-primary[type='submit']").first.click()
                                time.sleep(5)

                                try:
                                    page.locator(ANY_SERVICE_BUTTON).first.wait_for(timeout=8000)
                                    session.log("\u2705 Captcha solved!")
                                    captcha_solved = True
                                    break
                                except:
                                    session.log(f"\u274c Wrong answer '{answer}', retrying...")
                                    try: page.locator(".modal .btn-secondary, .modal .close, .swal2-confirm, [class*='close']").first.click()
                                    except: pass
                                    time.sleep(1)
                                    try: page.locator(".refresh-capthca-btn-new, [onclick*='refresh'], .captcha-refresh").first.click()
                                    except: pass
                                    time.sleep(3)
                            except Exception as e:
                                err_str = str(e).lower()
                                if "crash" in err_str or "target closed" in err_str:
                                    session.log(f"\U0001f4a5 Crashed during captcha, restarting...")
                                    break
                                else:
                                    session.log(f"\u26a0\ufe0f Captcha error: {e}")
                                time.sleep(2)

                        if not captcha_solved:
                            continue

                    # \u2500\u2500 Click service button \u2500\u2500
                    session.log(f"{emoji} Looking for {svc_name} button...")
                    try:
                        page.locator(f".{btn_cls}").wait_for(timeout=30000)
                    except:
                        try:
                            btn_el = page.locator(f".{btn_cls}")
                            if btn_el.count() > 0 and btn_el.get_attribute("disabled"):
                                session.log(f"\u274c {svc_name} is currently unavailable on Zefoy. Try a different service.")
                            else:
                                session.log(f"\u274c {svc_name} button not found. Restarting...")
                        except:
                            session.log(f"\u274c {svc_name} button not found. Restarting...")
                        continue

                    page.locator(f".{btn_cls}").click()
                    time.sleep(2)
                    session.log(f"\u2705 {svc_name} panel opened!")

                    backoff = 5

                    # \u2500\u2500 Main loop \u2500\u2500
                    zero_streak = 0
                    no_response_streak = 0
                    MAX_NO_RESPONSE = 5
                    MAX_ZERO_STREAK = 10
                    while not session.stop_event.is_set():
                        if not _safe_check(page):
                            session.log("\U0001f4a5 Page crashed in main loop, restarting...")
                            break

                        cycle = session.add_cycle()
                        session.log(f"\U0001f504 Cycle {cycle}")

                        try:
                            url_input = page.locator(f".{menu_cls} input[type='text'], .{menu_cls} input[placeholder]").first
                            url_input.fill("")
                            time.sleep(0.3)
                            url_input.fill(session.video_url)
                            time.sleep(1)

                            page.locator(f".{menu_cls} button[type='submit']").first.click()
                            time.sleep(3)
                        except Exception as fill_err:
                            err_str = str(fill_err).lower()
                            if "crash" in err_str or "target closed" in err_str or "disposed" in err_str:
                                session.log("\U0001f4a5 Crashed filling URL, restarting...")
                                break
                            session.log(f"\u26a0\ufe0f Error filling URL: {fill_err}")
                            time.sleep(3)
                            continue

                        crashed_in_check = False
                        for check_round in range(120):
                            if session.stop_event.is_set():
                                break

                            try:
                                page_state = page.evaluate("""(menuClass) => {
                                    const body = document.body.innerText || '';
                                    const lower = body.toLowerCase();

                                    const countdown = document.getElementById('login-countdown');
                                    if (countdown && countdown.offsetParent !== null) {
                                        const text = countdown.innerText || '';
                                        if (text && (text.toLowerCase().includes('wait') ||
                                            text.toLowerCase().includes('minute') ||
                                            text.toLowerCase().includes('second'))) {
                                            return {type: 'ratelimit', text: text};
                                        }
                                    }

                                    if (lower.includes('successfully')) {
                                        let count = 0;
                                        const lines = body.split('\\n');
                                        let successLine = '';
                                        for (const line of lines) {
                                            if (line.toLowerCase().includes('successfully')) {
                                                successLine = line;
                                                break;
                                            }
                                        }
                                        // Log the raw success line for debugging
                                        console.log('ZEFOY_SUCCESS_RAW: ' + successLine);
                                        if (successLine) {
                                            const lineNums = successLine.match(/\\d+/g);
                                            if (lineNums) {
                                                // Filter out year-like numbers (2020-2035), month/day (1-31 only if line has date-like pattern)
                                                const hasDate = /\\d{1,2}[\\/-]\\d{1,2}[\\/-]\\d{2,4}|\\d{4}[\\/-]\\d{1,2}|[A-Za-z]+\\s+\\d{1,2},?\\s+\\d{4}/.test(successLine);
                                                const filtered = lineNums.map(Number).filter(n => {
                                                    if (n >= 2020 && n <= 2035) return false; // year
                                                    if (n > 100000) return false; // unreasonably large
                                                    return true;
                                                });
                                                if (filtered.length > 0) {
                                                    count = Math.max(...filtered);
                                                }
                                            }
                                        }
                                        return {type: 'success', count: count, rawLine: successLine};
                                    }

                                    const spinners = document.querySelectorAll('.fa-spinner, .fa-spin, .spinner, [class*="loading"], [class*="spin"]');
                                    for (const s of spinners) {
                                        if (s.offsetParent !== null) return {type: 'loading'};
                                    }

                                    const menu = document.querySelector('.' + menuClass);
                                    if (menu) {
                                        const forms = menu.querySelectorAll('form');
                                        for (const form of forms) {
                                            const action = form.getAttribute('action');
                                            if (action) {
                                                const container = document.getElementById(action);
                                                if (container && container.offsetParent !== null) {
                                                    const btn = container.querySelector('a, button, [onclick]');
                                                    if (btn && btn.offsetParent !== null) {
                                                        const r = btn.getBoundingClientRect();
                                                        if (r.width > 0 && r.height > 0) {
                                                            const sel = container.querySelector('select');
                                                            const selOpts = sel ? Array.from(sel.options).filter(o => o.value).map(o => o.value) : [];
                                                            return {type: 'bar', x: r.x + r.width/2, y: r.y + r.height/2, hasSelect: !!sel, selectOptions: selOpts};
                                                        }
                                                    }
                                                    const divs = container.querySelectorAll('div, span');
                                                    for (const d of divs) {
                                                        const t = d.innerText?.trim();
                                                        if (t && /\\d/.test(t) && t.length < 60 &&
                                                            !t.includes('wait') && !t.includes('minute') &&
                                                            !t.includes('second') && !t.includes('Please')) {
                                                            const r = d.getBoundingClientRect();
                                                            if (r.width > 50 && r.height > 10)
                                                                return {type: 'bar', x: r.x + r.width/2, y: r.y + r.height/2};
                                                        }
                                                    }
                                                }
                                            }
                                        }
                                    }

                                    if (lower.includes('please wait') && (lower.includes('minute') || lower.includes('second'))) {
                                        return {type: 'ratelimit', text: body.substring(0, 500)};
                                    }

                                    return {type: 'waiting'};
                                }""", menu_cls)
                            except Exception as eval_err:
                                err_str = str(eval_err).lower()
                                if "crash" in err_str or "target closed" in err_str or "disposed" in err_str:
                                    session.log("\U0001f4a5 Crashed during page check, restarting...")
                                    crashed_in_check = True
                                    break
                                time.sleep(1)
                                continue

                            state_type = page_state.get('type', 'waiting') if page_state else 'waiting'

                            if state_type == 'ratelimit':
                                no_response_streak = 0
                                timer_text = page_state.get('text', '')
                                wait_secs = parse_wait_time(timer_text)
                                if wait_secs <= 0:
                                    wait_secs = 60
                                wait_secs += 5
                                session.log(f"\u23f3 Rate limited ({wait_secs}s)")

                                for remaining in range(wait_secs, 0, -1):
                                    if session.stop_event.is_set():
                                        break
                                    mins = remaining // 60
                                    secs = remaining % 60
                                    time_str = f"{mins}m {secs:02d}s" if mins > 0 else f"{secs}s"
                                    session.set_countdown(f"\u23f3 {time_str} remaining")
                                    time.sleep(1)

                                session.set_countdown("")
                                session.log("\u2705 Rate limit done, retrying...")

                                try:
                                    time.sleep(1)
                                    url_input.fill("")
                                    time.sleep(0.3)
                                    url_input.fill(session.video_url)
                                    time.sleep(1)
                                    page.locator(f".{menu_cls} button[type='submit']").first.click()
                                    time.sleep(3)
                                except Exception as refill_err:
                                    err_str = str(refill_err).lower()
                                    if "crash" in err_str or "target closed" in err_str or "disposed" in err_str:
                                        crashed_in_check = True
                                        break
                                continue

                            elif state_type == 'success':
                                raw_line = page_state.get('rawLine', '')
                                count = page_state.get('count', 0)
                                if raw_line:
                                    session.log(f"📝 Zefoy raw: {raw_line[:120]}")
                                new_total = session.add_count(count)
                                if count > 0:
                                    zero_streak = 0
                                    no_response_streak = 0
                                    session.log(f"\U0001f389 +{count} {unit}! Total: {new_total:,}")
                                else:
                                    zero_streak += 1
                                    no_response_streak = 0
                                    if zero_streak >= MAX_ZERO_STREAK:
                                        session.log(f"\u26a0\ufe0f {zero_streak} consecutive 0 {unit} \u2014 resetting (not stopping)...")
                                        zero_streak = 0
                                    else:
                                        session.log(f"\u26a0\ufe0f Zefoy returned 0 {unit} (streak: {zero_streak}/{MAX_ZERO_STREAK}) \u2014 retrying...")
                                break

                            elif state_type == 'bar':
                                if page_state.get('hasSelect') and page_state.get('selectOptions'):
                                    try:
                                        best = page_state['selectOptions'][-1]
                                        page.locator("select#selectlimit, select[name='select_lmt'], select.form-select").first.select_option(best)
                                        session.log(f"\U0001f4ca Selected limit: {best}")
                                        time.sleep(0.5)
                                    except Exception as sel_err:
                                        session.log(f"\u26a0\ufe0f Could not set limit dropdown: {sel_err}")

                                x, y = page_state['x'], page_state['y']
                                session.log(f"{emoji} Sending {unit}...")
                                try:
                                    page.mouse.click(x, y)
                                except Exception as click_err:
                                    err_str = str(click_err).lower()
                                    if "crash" in err_str or "target closed" in err_str:
                                        crashed_in_check = True
                                        break
                                time.sleep(2)

                                count = 0
                                for _ in range(30):
                                    try:
                                        body = page.inner_text("body")
                                        if "successfully" in body.lower():
                                            for line in body.split('\n'):
                                                if 'successfully' in line.lower():
                                                    line_nums = [int(n) for n in re.findall(r'\d+', line) if 2020 <= int(n) <= 2035 is False and int(n) < 100000]
                                                    line_nums = [n for n in [int(x) for x in re.findall(r'\d+', line)] if not (2020 <= n <= 2035) and n < 100000]
                                                    if line_nums:
                                                        count = max(line_nums)
                                                break
                                            if count == 0:
                                                all_nums = [int(n) for n in re.findall(r'\d+', body) if not (2020 <= int(n) <= 2035) and int(n) < 100000]
                                                if all_nums:
                                                    count = max(all_nums)
                                            new_total = session.add_count(count)
                                            break
                                    except:
                                        pass
                                    time.sleep(1)

                                if count > 0:
                                    zero_streak = 0
                                    no_response_streak = 0
                                    session.log(f"\U0001f389 +{count} {unit}! Total: {new_total:,}")
                                else:
                                    zero_streak += 1
                                    no_response_streak = 0
                                    if zero_streak >= MAX_ZERO_STREAK:
                                        session.log(f"\u26a0\ufe0f {zero_streak} consecutive 0 {unit} \u2014 resetting (not stopping)...")
                                        zero_streak = 0
                                    else:
                                        session.log(f"\u26a0\ufe0f Zefoy returned 0 {unit} (streak: {zero_streak}/{MAX_ZERO_STREAK}) \u2014 retrying...")
                                break

                            elif state_type == 'loading':
                                time.sleep(1)
                                continue

                            else:
                                if check_round < 30:
                                    time.sleep(1)
                                    continue
                                else:
                                    no_response_streak += 1
                                    if no_response_streak >= MAX_NO_RESPONSE:
                                        session.log(f"\U0001f534 {no_response_streak} consecutive no-responses \u2014 reloading page...")
                                        no_response_streak = 0
                                        try:
                                            page.reload(wait_until="domcontentloaded")
                                            time.sleep(5)
                                            try:
                                                page.locator(f".{btn_cls}").wait_for(timeout=10000)
                                                page.locator(f".{btn_cls}").click()
                                                time.sleep(2)
                                                session.log(f"\u2705 {svc_name} panel re-opened after reload")
                                            except:
                                                session.log(f"\u26a0\ufe0f {svc_name} button not found after reload, restarting...")
                                                crashed_in_check = True
                                                break
                                        except Exception as reload_err:
                                            err_str = str(reload_err).lower()
                                            if "crash" in err_str or "target closed" in err_str:
                                                crashed_in_check = True
                                                break
                                            session.log(f"\u26a0\ufe0f Reload error: {reload_err}")
                                    else:
                                        session.log(f"\u26a0\ufe0f No response, retrying... ({no_response_streak}/{MAX_NO_RESPONSE})")
                                    break

                        if crashed_in_check:
                            session.log("\U0001f4a5 Crashed in main loop, restarting tab...")
                            break

                        time.sleep(3)
                        if cycle % 10 == 0:
                            gc.collect()

            except Exception as inner_err:
                err_str = str(inner_err).lower()
                if "crash" in err_str or "target closed" in err_str or "disposed" in err_str:
                    session.log(f"\U0001f4a5 Browser crashed, restarting tab...")
                else:
                    session.log(f"\u26a0\ufe0f Error: {inner_err} \u2014 restarting tab...")
                import traceback
                traceback.print_exc()
            finally:
                try:
                    if browser:
                        browser.close()
                except:
                    pass
                # Release global browser slot
                if got_slot:
                    with _active_browsers_lock:
                        _active_browsers = max(0, _active_browsers - 1)
                    _browser_semaphore.release()
                    got_slot = False
                gc.collect()

        session.log("\U0001f6d1 Tab exhausted all restart attempts.")

    except Exception as e:
        session.log(f"\u274c Fatal error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        with session.count_lock:
            session.active_tabs = max(0, session.active_tabs - 1)
            if session.active_tabs <= 0 and session.status == "running":
                session.status = "error"


def _resolve_tiktok_url(url, proxy=None):
    """Resolve shortened vm.tiktok.com URLs to full tiktok.com/@user/video/... format."""
    if not url:
        return url
    url = url.strip()
    # Already a full URL
    if "tiktok.com/@" in url:
        return url
    # Only resolve shortened URLs
    if "vm.tiktok.com" not in url and "vt.tiktok.com" not in url:
        return url
    try:
        sess = requests.Session()
        if proxy:
            sess.proxies = {"http": proxy, "https": proxy}
        resp = sess.head(url, allow_redirects=True, timeout=15,
                         headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
        resolved = resp.url
        # Clean off query params
        if "?" in resolved:
            resolved = resolved.split("?")[0]
        sess.close()
        return resolved if "tiktok.com" in resolved else url
    except Exception:
        # Try again with GET if HEAD fails
        try:
            resp = requests.get(url, allow_redirects=True, timeout=15,
                                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
                                proxies={"http": proxy, "https": proxy} if proxy else None)
            resolved = resp.url
            if "?" in resolved:
                resolved = resolved.split("?")[0]
            return resolved if "tiktok.com" in resolved else url
        except Exception:
            return url


def run_qqtube_tab(session, tab_id):
    """QQTube free likes - gets real FingerprintJS ID via browser, then uses HTTP API."""
    import gc
    global _active_browsers
    multi = session.num_tabs > 1
    _tab_prefix.value = f"[T{tab_id+1}] " if multi else ""
    
    QQTUBE_API = "https://www.qqtube.com/fioj"
    PAGE_URL = "https://www.qqtube.com/free-tiktok-likes"
    
    with session.count_lock:
        session.active_tabs += 1
    
    try:
        # ── Resolve shortened TikTok URL ──
        raw_url = session.video_url
        proxy_for_resolve = None
        if USING_TOR:
            proxy_for_resolve = f"socks5h://127.0.0.1:9050"
        elif PROXY_URL:
            proxy_for_resolve = PROXY_URL
        
        resolved_url = _resolve_tiktok_url(raw_url, proxy=proxy_for_resolve)
        if resolved_url != raw_url:
            session.log(f"🔗 Resolved URL: {resolved_url}")
            session.video_url = resolved_url
        
        submission_count = 0
        consecutive_cooldowns = 0
        consecutive_recaptchas = 0
        consecutive_url_rejects = 0
        ips_tried = 0
        tor_port_offset = 0
        start_time = time.time()
        
        while not session.stop_event.is_set():
            # Use different SOCKS port each time for IP diversity
            tor_port = 9050 + ((tab_id + tor_port_offset) % 10)
            tor_port_offset += 1
            
            # Rotate Tor circuit for fresh IP (except first run)
            if USING_TOR and ips_tried > 0:
                session.log("\U0001f504 Rotating Tor circuit for new IP...")
                _rotate_tor_circuit()
                time.sleep(2)
            
            ips_tried += 1
            
            # ── Get REAL FingerprintJS visitorId via headless browser ──
            # QQTube validates the fingerprint against their FingerprintJS Pro
            # backend on the "start" call.  A fake/random hash is rejected with
            # "Unable to verify your request".  We must run the real SDK in a
            # browser to obtain a genuine visitorId tied to our current IP.
            ffpr = ""
            cookies_dict = {}
            session.log(f"\U0001f310 [{ips_tried}] Obtaining browser fingerprint...")
            session.set_countdown("\U0001f50d Getting browser fingerprint...")
            
            got_fp_slot = False
            try:
                if not _browser_semaphore.acquire(timeout=1):
                    session.log("\u23f3 Waiting for browser slot...")
                    _browser_semaphore.acquire()
                got_fp_slot = True
                with _active_browsers_lock:
                    _active_browsers += 1
            except:
                pass
            
            try:
                with sync_playwright() as p:
                    fp_launch_opts = {
                        "headless": True,
                        "args": [
                            "--no-sandbox",
                            "--disable-dev-shm-usage",
                            "--disable-gpu",
                            "--single-process",
                            "--js-flags=--max-old-space-size=128",
                        ],
                    }
                    if USING_TOR:
                        fp_launch_opts["proxy"] = {"server": f"socks5://127.0.0.1:{tor_port}"}
                    elif PROXY_URL:
                        fp_launch_opts["proxy"] = {"server": PROXY_URL}
                    
                    fp_browser = p.chromium.launch(**fp_launch_opts)
                    fp_ctx = fp_browser.new_context()
                    fp_page = fp_ctx.new_page()
                    
                    # Load a minimal HTML page on QQTube's domain (faster than the full page)
                    # We only need to be on the domain so the SDK endpoint calls work
                    try:
                        fp_page.goto("https://www.qqtube.com/robots.txt",
                                     wait_until="commit", timeout=60000)
                    except Exception:
                        # Even if navigation partially fails, the page context may
                        # still be usable for script injection
                        pass
                    
                    # Set a generous default timeout for all page operations
                    fp_page.set_default_timeout(60000)
                    
                    # Run the real FingerprintJS SDK to get a genuine visitorId
                    try:
                        ffpr = fp_page.evaluate("""() => {
                            return new Promise((resolve) => {
                                const timeout = setTimeout(() => resolve(''), 45000);
                                
                                import('https://static1.qqtube.com/web/v3/L7VMDtfAtpoCHApk30SD')
                                    .then(mod => mod.load({
                                        endpoint: [
                                            'https://static1.qqtube.com',
                                            mod.defaultEndpoint
                                        ]
                                    }))
                                    .then(fp => fp.get())
                                    .then(result => {
                                        clearTimeout(timeout);
                                        resolve(result.visitorId || '');
                                    })
                                    .catch(() => {
                                        clearTimeout(timeout);
                                        resolve('');
                                    });
                            });
                        }""")
                    except Exception as fp_err:
                        session.log(f"\u26a0\ufe0f FingerprintJS error: {str(fp_err)[:80]}")
                    
                    # Grab cookies set by QQTube / FingerprintJS
                    try:
                        for c in fp_ctx.cookies():
                            cookies_dict[c['name']] = c['value']
                    except:
                        pass
                    
                    fp_browser.close()
            except Exception as browser_err:
                session.log(f"\u26a0\ufe0f Browser error: {str(browser_err)[:80]}")
            finally:
                if got_fp_slot:
                    _browser_semaphore.release()
                    with _active_browsers_lock:
                        _active_browsers = max(0, _active_browsers - 1)
            
            if not ffpr:
                session.log("\u26a0\ufe0f Could not get fingerprint, retrying in 5s...")
                time.sleep(5)
                gc.collect()
                continue
            
            session.log(f"\u2705 Real fingerprint: {ffpr[:12]}...")
            
            # Create fresh HTTP session with Tor proxy
            http_sess = requests.Session()
            ua = _random_ua()
            http_sess.headers.update({
                "User-Agent": ua,
                "Content-Type": "application/json",
                "Origin": "https://www.qqtube.com",
                "Referer": PAGE_URL,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
            })
            # Forward cookies from browser session
            for cname, cval in cookies_dict.items():
                http_sess.cookies.set(cname, cval)
            
            if USING_TOR:
                http_sess.proxies = {
                    "http": f"socks5h://127.0.0.1:{tor_port}",
                    "https": f"socks5h://127.0.0.1:{tor_port}",
                }
                session.log(f"\U0001f9c5 IP #{ips_tried} via Tor port {tor_port}")
            elif PROXY_URL:
                http_sess.proxies = {"http": PROXY_URL, "https": PROXY_URL}
            
            try:
                # Revisit ping (mimics real browser page-load behavior)
                try:
                    http_sess.post(QQTUBE_API, json={
                        "action": "revisit",
                        "ffpr": ffpr,
                        "page_url": PAGE_URL
                    }, timeout=15)
                except:
                    pass
                
                # STEP 1: Get services + check cooldown
                session.log(f"\U0001f310 [{ips_tried}] Checking services...")
                session.set_countdown("\U0001f50d Checking if this IP can submit...")
                
                r = http_sess.post(QQTUBE_API, json={
                    "action": "services",
                    "provider": "TikTok",
                    "service_type": "Likes",
                    "ffpr": ffpr,
                    "referrer": ""
                }, timeout=30)
                svc_data = r.json()
                
                if svc_data.get("cooldown"):
                    cd_msg = svc_data.get("cooldown_msg", "cooldown")
                    consecutive_cooldowns += 1
                    session.log(f"\u23f0 IP #{ips_tried} on cooldown: {cd_msg}")
                    session.set_countdown(f"\U0001f504 {consecutive_cooldowns} IPs on cooldown, trying next...")
                    
                    if consecutive_cooldowns >= 20:
                        session.log("\u26a0\ufe0f 20 consecutive cooldowns! Waiting 5 min...")
                        for wi in range(300):
                            if session.stop_event.is_set():
                                return
                            if wi % 30 == 0:
                                session.set_countdown(f"\u23f3 Cooldown break: {(300-wi)//60}m {(300-wi)%60}s")
                            time.sleep(1)
                        consecutive_cooldowns = 0
                    continue
                
                if not svc_data.get("valid") or not svc_data.get("services"):
                    session.log("\u26a0\ufe0f No services returned, retrying in 5s...")
                    time.sleep(5)
                    continue
                
                svc = svc_data["services"][0]
                service_id = svc["service"]
                quantity = svc.get("quantity", 100)
                consecutive_cooldowns = 0
                session.log(f"\u2705 IP clean! Service #{service_id}, qty: {quantity}")
                
                # STEP 2: Precheck URL
                session.set_countdown("\U0001f50d Validating TikTok URL...")
                r = http_sess.post(QQTUBE_API, json={
                    "action": "precheck",
                    "url": session.video_url,
                    "service_id": service_id,
                    "ffpr": ffpr
                }, timeout=30)
                pre_data = r.json()
                
                if pre_data.get("valid") == False:
                    msg = pre_data.get("msg", "Invalid URL")
                    consecutive_url_rejects += 1
                    session.log(f"\u274c URL rejected: {msg}")
                    
                    # URL-level errors won't fix with a new IP - stop after 2 tries
                    url_err_lower = msg.lower()
                    is_permanent = any(kw in url_err_lower for kw in [
                        "unable to process", "try a different", "invalid",
                        "not found", "doesn\'t exist", "already",
                    ])
                    if is_permanent or consecutive_url_rejects >= 3:
                        session.log(f"\U0001f6d1 URL cannot be processed. Try a different video URL.")
                        session.set_countdown(f"\U0001f6d1 URL rejected - use a different link")
                        session.status = "error"
                        return
                    
                    session.set_countdown(f"\u274c {msg}")
                    time.sleep(10)
                    continue
                
                # STEP 3: Start session
                session.set_countdown("\U0001f680 Starting order session...")
                flow_start = time.time()
                
                r = http_sess.post(QQTUBE_API, json={
                    "action": "start",
                    "ffpr": ffpr,
                    "page_url": PAGE_URL
                }, timeout=30)
                start_data = r.json()
                
                if not start_data.get("valid"):
                    msg = start_data.get("msg", "")
                    if "already used" in msg.lower() or "cooldown" in msg.lower():
                        consecutive_cooldowns += 1
                        session.log(f"\u23f0 Start blocked: {msg}")
                        continue
                    session.log(f"\u26a0\ufe0f Start failed: {msg}")
                    time.sleep(5)
                    continue
                
                token = start_data.get("token", "")
                wait_time_s = start_data.get("wait_time", 100)
                bonus = start_data.get("a_send_qty", 0)
                total_qty = quantity + bonus
                
                session.log(f"\U0001f3af Token OK! Wait: {wait_time_s}s, qty: {total_qty}")
                
                # STEP 4: Wait the required time
                for wi in range(wait_time_s):
                    if session.stop_event.is_set():
                        return
                    remaining_s = wait_time_s - wi
                    pct = int((wi / max(wait_time_s, 1)) * 100)
                    session.set_countdown(f"\u23f3 Processing: {pct}% \u2014 {remaining_s}s left")
                    time.sleep(1)
                
                # STEP 5: Check session
                session.set_countdown("\U0001f50d Verifying session...")
                r = http_sess.post(QQTUBE_API, json={
                    "action": "check",
                    "token": token
                }, timeout=30)
                check_data = r.json()
                
                if not check_data.get("valid"):
                    msg = check_data.get("msg", "Check failed")
                    session.log(f"\u26a0\ufe0f Verification failed: {msg}")
                    time.sleep(3)
                    continue
                
                recaptcha_on = check_data.get("recaptcha_enabled", True)
                recaptcha_key = check_data.get("recaptcha_key", "")
                recaptcha_response = ""

                if recaptcha_on is not False and recaptcha_key:
                    # Try to solve reCAPTCHA using configured service
                    session.log("\U0001f512 reCAPTCHA required \u2014 solving...")
                    session.set_countdown("\U0001f9e9 Solving reCAPTCHA...")
                    solver_proxy = f"socks5://127.0.0.1:{tor_port}" if USING_TOR else (PROXY_URL or None)
                    recaptcha_response = solve_recaptcha_v2(
                        site_key=recaptcha_key,
                        page_url=PAGE_URL,
                        session=session,
                        timeout=180,
                        proxy=solver_proxy,
                    )
                    if recaptcha_response:
                        consecutive_recaptchas = 0
                        session.log("\u2705 reCAPTCHA solved!")
                    else:
                        consecutive_recaptchas += 1
                        session.log(f"\u26a0\ufe0f Could not solve reCAPTCHA (#{consecutive_recaptchas}), trying anyway...")
                elif recaptcha_on is not False:
                    # reCAPTCHA required but no site key returned
                    consecutive_recaptchas += 1
                    session.log(f"\U0001f512 reCAPTCHA required (no key) \u2014 submitting blind (#{consecutive_recaptchas})...")
                else:
                    consecutive_recaptchas = 0
                    session.log("\U0001f513 No reCAPTCHA needed!")
                
                # STEP 6: Place order
                dwell = int(time.time() - flow_start)
                session.set_countdown("\U0001f4e6 Placing order...")
                
                r = http_sess.post(QQTUBE_API, json={
                    "action": "order",
                    "service_id": service_id,
                    "url": session.video_url,
                    "quantity": quantity,
                    "ffpr": ffpr,
                    "token": token,
                    "recaptcha_response": recaptcha_response,
                    "dwell_seconds": dwell
                }, timeout=30)
                order_data = r.json()
                
                if order_data.get("valid"):
                    total = session.add_count(total_qty)
                    cycle = session.add_cycle()
                    submission_count += 1
                    consecutive_recaptchas = 0
                    elapsed_min = (time.time() - start_time) / 60
                    rate = int(total / elapsed_min) if elapsed_min > 0.5 else 0
                    
                    session.log(f"\U0001f389 +{total_qty} likes ordered! Total: {total:,} | IPs: {ips_tried} | ~{rate}/min")
                    session.set_countdown(f"\u2705 {total:,} likes ordered \u2014 getting fresh IP!")
                    time.sleep(2)
                    session.set_countdown("")
                else:
                    msg = order_data.get("msg", "Order failed")
                    session.log(f"\u274c Order rejected: {msg}")
                    
                    if "captcha" in msg.lower() or "recaptcha" in msg.lower():
                        consecutive_recaptchas += 1
                        session.log(f"\U0001f512 reCAPTCHA bypass failed \u2014 rotating IP...")
                        if consecutive_recaptchas >= 10:
                            session.log("\u26a0\ufe0f 10 reCAPTCHA fails, waiting 2 min...")
                            for wi in range(120):
                                if session.stop_event.is_set():
                                    return
                                time.sleep(1)
                            consecutive_recaptchas = 0
                    elif "cooldown" in msg.lower() or "already" in msg.lower():
                        consecutive_cooldowns += 1
                    
                    time.sleep(3)
                
            except requests.exceptions.RequestException as e:
                session.log(f"\U0001f310 Network error: {str(e)[:80]}")
                time.sleep(5)
            except Exception as e:
                session.log(f"\u26a0\ufe0f Error: {str(e)[:100]}")
                import traceback
                traceback.print_exc()
                time.sleep(5)
            finally:
                try:
                    http_sess.close()
                except:
                    pass
                gc.collect()
            
            if not session.stop_event.is_set():
                time.sleep(1)
    
    finally:
        with session.count_lock:
            session.active_tabs = max(0, session.active_tabs - 1)
        session.log(f"\U0001f3c1 QQTube tab done. Submissions: {submission_count}")

# ═══════════════════════════════════════════════════════════════
#  ROUTES
# ═══════════════════════════════════════════════════════════════

@app.route("/")
def index():
    response = make_response(render_template("index.html"))
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


@app.route("/tor-status")
def tor_status():
    import socket, subprocess as sp
    ready = os.path.exists("/tmp/tor_ready")
    log = ""
    try:
        with open("/tmp/tor.log") as f:
            log = f.read()[-3000:]  # last 3KB
    except:
        log = "No log file yet"
    ports = {}
    for port in range(9050, 9060):
        try:
            s = socket.socket()
            s.settimeout(0.5)
            s.connect(("127.0.0.1", port))
            s.close()
            ports[port] = "OPEN"
        except:
            ports[port] = "CLOSED"
    # Check if tor process exists
    try:
        result = sp.run(["pgrep", "-a", "tor"], capture_output=True, text=True, timeout=3)
        tor_procs = result.stdout.strip()
    except:
        tor_procs = "unknown"
    return jsonify({"ready": ready, "ports": ports, "processes": tor_procs, "log": log})


@app.route("/sessions")
def list_sessions():
    with sessions_lock:
        data = [s.to_dict() for s in sessions.values()]
    return jsonify({"sessions": data, "browsers": _active_browsers, "maxBrowsers": MAX_GLOBAL_BROWSERS})


@app.route("/start", methods=["POST"])
def start():
    data = request.get_json()
    url = data.get("url", "").strip()
    service = data.get("service", "views").strip().lower()
    tabs = int(data.get("tabs", 1))
    if not url:
        return jsonify({"error": "No URL provided"}), 400
    if service not in SERVICES:
        return jsonify({"error": f"Unknown service: {service}. Valid: {', '.join(SERVICES.keys())}"}), 400

    session = Session(url, service=service, num_tabs=tabs)
    with sessions_lock:
        sessions[session.id] = session

    t = threading.Thread(target=run_session, args=(session,), daemon=True)
    session.thread = t
    t.start()

    return jsonify(session.to_dict())


@app.route("/stop/<int:sid>", methods=["POST"])
def stop(sid):
    with sessions_lock:
        session = sessions.get(sid)
    if not session:
        return jsonify({"error": "Not found"}), 404
    session.stop_event.set()
    session.status = "stopping"
    return jsonify({"ok": True})


@app.route("/stream/all")
def stream_all():
    """Single multiplexed SSE stream for ALL sessions — avoids browser connection limits."""
    def generate():
        tracking = {}  # sid → {last_log_idx, last_countdown, ended_sent}

        while True:
            with sessions_lock:
                current_sessions = dict(sessions)

            for sid, session in current_sessions.items():
                if sid not in tracking:
                    tracking[sid] = {"last_log_idx": 0, "last_countdown": "", "ended_sent": False}

                t = tracking[sid]

                # New log lines
                current_len = len(session.logs)
                while t["last_log_idx"] < current_len:
                    data = json.dumps({"type": "log", "sid": sid, "text": session.logs[t["last_log_idx"]]})
                    yield f"data: {data}\n\n"
                    t["last_log_idx"] += 1

                # Countdown update
                cd = session.countdown
                if cd != t["last_countdown"]:
                    t["last_countdown"] = cd
                    data = json.dumps({"type": "countdown", "sid": sid, "text": cd})
                    yield f"data: {data}\n\n"

                # Stats update
                data = json.dumps({
                    "type": "stats",
                    "sid": sid,
                    "count": session.total_count,
                    "unit": session.svc["unit"],
                    "cycles": session.cycles,
                    "status": session.status,
                })
                yield f"data: {data}\n\n"

                # Ended signal (once)
                if session.status in ("stopped", "error") and not t["ended_sent"]:
                    data = json.dumps({"type": "ended", "sid": sid, "status": session.status})
                    yield f"data: {data}\n\n"
                    t["ended_sent"] = True

            # Clean up tracking for removed sessions
            tracking = {sid: v for sid, v in tracking.items() if sid in current_sessions}

            time.sleep(0.5)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@app.route("/remove/<int:sid>", methods=["POST"])
def remove_session(sid):
    """Remove a stopped/error session from the list."""
    with sessions_lock:
        session = sessions.get(sid)
        if not session:
            return jsonify({"error": "Not found"}), 404
        if session.status not in ("stopped", "error"):
            return jsonify({"error": "Can only remove stopped sessions"}), 400
        del sessions[sid]
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=8080)
