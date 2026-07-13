from flask import Flask, render_template, request, jsonify, Response
from playwright.sync_api import sync_playwright
import threading, time, re, sys, difflib, json, base64, os, resource
from PIL import Image, ImageOps
from io import BytesIO
from collections import Counter, deque
import numpy as np

def _is_dead(e):
    """Return True if the exception means the browser/page is gone."""
    s = str(e).lower()
    t = type(e).__name__.lower()
    return ("target page" in s or "browser has been closed" in s or
            "target closed" in s or "crash" in s or "disposed" in s or
            "connection closed" in s or "browser disconnected" in s or
            "frame was detached" in s or "err_aborted" in s or
            "net::err" in s or "page crash" in s or
            "eagain" in s or "resource temporarily unavailable" in s or
            "failed to launch" in s or "spawn" in s or
            "targetclosed" in t)

# Thread-local tab prefix for log messages
_tab_prefix = threading.local()

# ═══════════════════════════════════════════════════════════════
#  LIVE SCREENSHOT HELPER
# ═══════════════════════════════════════════════════════════════

def save_live_screenshot(page, filename="live.png"):
    """Save screenshot for live preview on frontend"""
    try:
        os.makedirs("static", exist_ok=True)
        page.screenshot(path=f"static/{filename}")
    except:
        pass  # Ignore screenshot errors

# ═══════════════════════════════════════════════════════════════
#  CONCURRENCY LIMITS
# ═══════════════════════════════════════════════════════════════

# Captcha solving semaphore — each solve spawns ~80 tesseract processes,
# so cap at CAPTCHA_CONCURRENCY simultaneous solves to avoid OS fork bomb.
# Override via CAPTCHA_CONCURRENCY env var.
CAPTCHA_CONCURRENCY = int(os.environ.get("CAPTCHA_CONCURRENCY", "3"))
_captcha_semaphore = threading.Semaphore(CAPTCHA_CONCURRENCY)

# Reuse captcha concurrency for OCR calls
_ocr_semaphore = threading.Semaphore(CAPTCHA_CONCURRENCY)

# Global limit: max Chromium browsers across ALL sessions at once.
# Override via MAX_BROWSERS env var (e.g. set to a lower value on small instances).
MAX_GLOBAL_BROWSERS = int(os.environ.get("MAX_BROWSERS", "12"))
_browser_semaphore = threading.Semaphore(MAX_GLOBAL_BROWSERS)
_active_browsers = 0
_active_browsers_lock = threading.Lock()

# Attempt to raise OS process/file limits (best-effort, ignored if not permitted)
try:
    soft, hard = resource.getrlimit(resource.RLIMIT_NPROC)
    resource.setrlimit(resource.RLIMIT_NPROC, (hard, hard))
    print(f"[SYS] Raised RLIMIT_NPROC to {hard}", flush=True)
except:
    pass

try:
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (hard, hard))
    print(f"[SYS] Raised RLIMIT_NOFILE to {hard}", flush=True)
except:
    pass

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
if not PROXY_URL and USE_TOR:
    PROXY_URL = "socks5://127.0.0.1:9050"
    USING_TOR = True
else:
    USING_TOR = False



def renew_tor_circuit():
    """Signal Tor to build new circuits (get a fresh IP)."""
    import socket
    try:
        cookie_path = "/tmp/tor-data/control_auth_cookie"
        if not os.path.exists(cookie_path):
            print("[TOR] No control cookie found — cannot renew circuit", flush=True)
            return False
        with open(cookie_path, "rb") as f:
            cookie = f.read()

        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(10)
        s.connect(("127.0.0.1", 9060))
        s.send(b"AUTHENTICATE " + cookie.hex().encode() + b"\r\n")
        resp = s.recv(256)
        if b"250" not in resp:
            print(f"[TOR] Auth failed: {resp}", flush=True)
            s.close()
            return False

        s.send(b"SIGNAL NEWNYM\r\n")
        resp = s.recv(256)
        s.close()

        if b"250" in resp:
            print("[TOR] ✅ New circuit requested — fresh IP incoming!", flush=True)
            time.sleep(5)  # Give Tor time to build new circuits
            return True
        else:
            print(f"[TOR] NEWNYM failed: {resp}", flush=True)
            return False
    except Exception as e:
        print(f"[TOR] Circuit renewal error: {e}", flush=True)
        return False

# ═══════════════════════════════════════════════════════════════
#  OVERLAY REMOVAL HELPER
# ═══════════════════════════════════════════════════════════════

def remove_overlays(page):
    """Strip ad iframes and consent dialogs that can intercept clicks (2026 DOM)."""
    try:
        page.evaluate("""() => {
            // Remove all iframes (ads, consent dialogs)
            document.querySelectorAll('iframe').forEach(el => el.remove());
            // Remove Funding Choices and Google consent popups
            document.querySelectorAll('.fc-dialog-overlay, .fc-monetization-dialog-container, .fc-message-root, .fc-consent-root').forEach(el => el.remove());
            // Remove Google ads containers
            document.querySelectorAll('.adsbygoogle, .ad-container, iframe[src*="googleads"], iframe[src*="ads"], iframe.adsbygoogle').forEach(el => el.remove());
            // Remove any floating overlays with high z-index
            document.querySelectorAll('[style*="position: fixed"], [style*="position: absolute"]').forEach(el => {
                if (el.style.zIndex && parseInt(el.style.zIndex) > 9000) el.remove();
            });
            // Auto-dismiss consent buttons
            document.querySelectorAll('button').forEach(btn => {
                if (btn.textContent.includes('Consent') && btn.offsetParent !== null) btn.click();
            });
        }""")
    except:
        pass


# ═══════════════════════════════════════════════════════════════
#  ANTI-DETECTION SCRIPTS (from reference)
# ═══════════════════════════════════════════════════════════════

DISMISS_ALERTS_JS = "window.alert = function() { return true; }; window.confirm = function() { return true; };"

BLOCK_FC_POPUPS_JS = """(() => {
    const cleanPage = () => {
        document.querySelectorAll('iframe').forEach(el => el.remove());
        document.querySelectorAll('.fc-monetization-dialog-container, .fc-message-root, .fc-dialog-overlay, .fc-consent-root').forEach(el => el.remove());
        document.querySelectorAll('.adsbygoogle').forEach(el => el.remove());
        document.querySelectorAll('button').forEach(btn => {
            if (btn.textContent.includes('Consent') && btn.offsetParent !== null) btn.click();
        });
    };
    setTimeout(cleanPage, 800);
    const observer = new MutationObserver(cleanPage);
    if (document.body) observer.observe(document.body, { childList: true, subtree: true });
    else document.addEventListener('DOMContentLoaded', () => observer.observe(document.body, { childList: true, subtree: true }));
})();"""

MOUSE_SIMULATION_K9X_JS = """(() => {
    function generateK9xMouseData() {
        const points = [];
        const numPoints = Math.floor(Math.random() * 16) + 12;
        for (let i = 0; i < numPoints; i++) {
            const x = Math.floor(Math.random() * 1850) + 50;
            const y = Math.floor(Math.random() * 950) + 50;
            const d = (Math.random() * 2.75 + 0.05).toFixed(4);
            const g = Math.random() > 0.65 ? "True" : "False";
            points.push(`x=${x}&y=${y}&d=${d}&g=${g}`);
        }
        const raw = points.join("|");
        let xored = "";
        for (let i = 0; i < raw.length; i++) {
            xored += String.fromCharCode(raw.charCodeAt(i) ^ ((i % 5) + 77));
        }
        const wrapped = "K9x!" + xored + "K9x!";
        const encoded = btoa(wrapped);
        let reversed = encoded.split("").reverse().join("");
        while (reversed.length % 4 !== 0) reversed += "=";
        return reversed;
    }
    function injectMouseData() {
        const mouseData = generateK9xMouseData();
        document.querySelectorAll('input[type="hidden"]').forEach(input => {
            if (!input.value && input.name !== 'captcha_encoded') input.value = mouseData;
        });
        window.__zefoyMouseData = mouseData;
    }
    setTimeout(injectMouseData, 500);
    setTimeout(injectMouseData, 1500);
    setTimeout(injectMouseData, 3000);
    document.addEventListener('submit', function(e) { injectMouseData(); }, true);
    document.addEventListener('click', function(e) {
        if (e.target.tagName === 'BUTTON' || e.target.closest('button')) setTimeout(injectMouseData, 50);
    }, true);
    const observer = new MutationObserver(function(mutations) {
        mutations.forEach(function(m) { if (m.addedNodes.length > 0) setTimeout(injectMouseData, 100); });
    });
    if (document.body) observer.observe(document.body, { childList: true, subtree: true });
    else document.addEventListener('DOMContentLoaded', function() {
        observer.observe(document.body, { childList: true, subtree: true }); injectMouseData();
    });
    window.generateK9xMouseData = generateK9xMouseData;
    window.injectMouseData = injectMouseData;
})();"""

GENERATE_CF_OB_TE_JS = """(() => {
    function generateCfObTeCookie() {
        const source = "HTMLButtonElement.onclick@https://zefoy.com/:1:1";
        const kod = "DOMContentLoaded";
        const payload = `Kod: ${kod}\nsource: ${source}`;
        const cookieValue = btoa(payload);
        const expiry = new Date(Date.now() + 5 * 60 * 60 * 1000).toUTCString();
        document.cookie = `cf_ob_te=${cookieValue}; Path=/; Expires=${expiry}`;
        return cookieValue;
    }
    generateCfObTeCookie();
    setInterval(generateCfObTeCookie, 60000);
    window.generateCfObTeCookie = generateCfObTeCookie;
})();"""


def inject_anti_detection(page):
    """Inject all anti-detection scripts into the page."""
    try:
        for script in [DISMISS_ALERTS_JS, BLOCK_FC_POPUPS_JS, MOUSE_SIMULATION_K9X_JS, GENERATE_CF_OB_TE_JS]:
            page.evaluate(script)
    except:
        pass

# ═══════════════════════════════════════════════════════════════
#  HEARTS SEND BUTTON (reference HTML from zefoy.com)
#  After a successful Search, zefoy shows this button with the
#  video's current heart count. Click it to send hearts.
#
#  <button type="submit" class="wbutton btn btn-dark rounded-0 font-weight-bold p-2">
#    <i class="fa fa-heart text-danger fa-lg"></i> 58,600
#  </button>
#
#  Primary selector : button.wbutton.btn-dark  (has both wbutton and btn-dark classes)
#  Fallback selector: button.wbutton           (broader match)
# ═══════════════════════════════════════════════════════════════
HEARTS_BTN_SEL = "button.wbutton.btn-dark"

# ═══════════════════════════════════════════════════════════════
#  SERVICES
# ═══════════════════════════════════════════════════════════════

SERVICES = {
    "hearts": {
        "name": "Hearts",
        "emoji": "❤️",
        "button_class": "t-hearts-button",
        "menu_class": "t-hearts-menu",
        "unit": "hearts",
        "engine": "zefoy",
    },
    "views": {
        "name": "Views",
        "emoji": "👁️",
        "button_class": "t-views-button",
        "menu_class": "t-views-menu",
        "unit": "views",
        "engine": "zefoy",
    },
    "comment_hearts": {
        "name": "Comment Hearts",
        "emoji": "💬",
        "button_class": "t-chearts-button",
        "menu_class": "t-chearts-menu",
        "unit": "hearts",
        "engine": "zefoy",
    },
    "shares": {
        "name": "Shares",
        "emoji": "🔄",
        "button_class": "t-shares-button",
        "menu_class": "t-shares-menu",
        "unit": "shares",
        "engine": "zefoy",
    },
    "favorites": {
        "name": "Favorites",
        "emoji": "⭐",
        "button_class": "t-favorites-button",
        "menu_class": "t-favorites-menu",
        "unit": "favorites",
        "engine": "zefoy",
    },
    "followers": {
        "name": "Followers",
        "emoji": "👥",
        "button_class": "t-followers-button",
        "menu_class": "t-followers-menu",
        "unit": "followers",
        "engine": "zefoy",
    },
}

# CSS selector that matches ANY service button (used for captcha-solved check)
ANY_SERVICE_BUTTON = ", ".join(f".{s['button_class']}" for s in SERVICES.values() if 'button_class' in s)


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
    with _ocr_semaphore:
        return _solve_captcha_inner(img_bytes)

def _solve_captcha_inner(img_bytes):
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
        """Run tesseract with multiple PSM modes and collect results.
        Retries on EAGAIN (resource temporarily unavailable) with backoff."""
        found = []
        for psm in [7, 8, 13, 6]:
            config = f'--psm {psm} --oem 3 -c tessedit_char_whitelist=abcdefghijklmnopqrstuvwxyz'
            # Retry up to 4 times on EAGAIN
            for attempt in range(4):
                try:
                    text = pytesseract.image_to_string(pil_img, config=config).strip()
                    text = re.sub(r'[^a-z]', '', text.lower())
                    if 3 <= len(text) <= 12:
                        found.append(text)
                    break  # Success, move to next PSM
                except Exception as ocr_err:
                    err_str = str(ocr_err).lower()
                    # EAGAIN = transient fork failure, retry with backoff
                    if "eagain" in err_str or "resource temporarily unavailable" in err_str or "errno 11" in err_str:
                        if attempt < 3:
                            backoff = 0.3 * (attempt + 1)  # 0.3s, 0.6s, 0.9s, 1.2s
                            print(f"[BOT] OCR {tag} psm={psm} EAGAIN, retrying in {backoff}s (attempt {attempt+1}/4)...", flush=True)
                            time.sleep(backoff)
                            continue
                        else:
                            print(f"[BOT] OCR {tag} psm={psm} EAGAIN exhausted after 4 attempts", flush=True)
                    else:
                        print(f"[BOT] OCR {tag} psm={psm} error: {ocr_err}", flush=True)
                    break  # Fatal error or exhausted retries
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




def resolve_comment_link(url):
    """Resolve a TikTok comment short URL to extract comment_id.
    Tries multiple methods: urllib redirect following, then regex on response body."""
    if not url:
        return None
    try:
        import urllib.request
        from urllib.parse import urlparse, parse_qs, unquote

        # Method 1: Follow redirects with urllib
        final_url = url
        try:
            req = urllib.request.Request(url)
            req.add_header('User-Agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
            response = urllib.request.urlopen(req, timeout=15)
            final_url = response.url
            body_text = ""
            try:
                body_text = response.read(50000).decode('utf-8', errors='ignore')
            except:
                pass
        except urllib.error.HTTPError as e:
            final_url = e.headers.get('Location', url) if hasattr(e, 'headers') else url
            body_text = ""
        except Exception:
            body_text = ""

        print(f"[BOT] Comment link resolved to: {final_url}", flush=True)

        # Parse comment_id from URL query params
        parsed = urlparse(final_url)
        params = parse_qs(parsed.query)
        comment_id = params.get('comment', [None])[0] or params.get('reply_comment_id', [None])[0]

        # If no comment_id in URL, try to find it in the page body (redirect meta, JS)
        if not comment_id and body_text:
            # Look for comment= in any URL in the body
            import re as _re
            comment_matches = _re.findall(r'comment=(\d+)', body_text)
            if comment_matches:
                comment_id = comment_matches[0]
            # Also try reply_comment_id
            if not comment_id:
                reply_matches = _re.findall(r'reply_comment_id=(\d+)', body_text)
                if reply_matches:
                    comment_id = reply_matches[0]
            # Try canonical URL or og:url meta tag
            if not comment_id:
                og_matches = _re.findall(r'(?:canonical|og:url)["\']?\s*(?:content|href)=["\']([^"\']+)["\']', body_text)
                for og_url in og_matches:
                    og_parsed = urlparse(unquote(og_url))
                    og_params = parse_qs(og_parsed.query)
                    cid = og_params.get('comment', [None])[0]
                    if cid:
                        comment_id = cid
                        break

        # Extract video creator username from path: /@username/video/...
        path_parts = parsed.path.strip('/').split('/')
        video_creator = path_parts[0].lstrip('@') if path_parts else None
        # Extract video ID
        video_id = None
        if 'video' in path_parts:
            idx = path_parts.index('video')
            if idx + 1 < len(path_parts):
                video_id = path_parts[idx + 1]

        print(f"[BOT] Resolved comment: comment_id={comment_id}, video_id={video_id}, creator={video_creator}", flush=True)
        return {
            'final_url': final_url,
            'comment_id': comment_id,
            'video_creator': video_creator,
            'video_id': video_id,
        }
    except Exception as e:
        print(f"[BOT] Comment link resolution failed: {e}", flush=True)
        return None

# ═══════════════════════════════════════════════════════════════
#  SESSION MANAGEMENT
# ════════════════════════════════════════════════��══════════════

class Session:
    _counter = 0
    _lock = threading.Lock()

    def __init__(self, video_url, service="views", num_tabs=1, username=""):
        with Session._lock:
            Session._counter += 1
            self.id = Session._counter
        self.video_url = video_url
        self.service = service  # key into SERVICES dict
        self.username = username  # Target username for comment_hearts

        self.num_tabs = max(1, min(num_tabs, 20))  # clamp 1-20
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
            "username": self.username,
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

    if nt <= 1:
        session.log(f"🚀 Launching browser ({svc_name} mode)...")
        run_tab(session, 0)
    else:
        session.log(f"🚀 Launching {nt} tabs ({svc_name} mode)...")
        threads = []
        for tab_id in range(nt):
            t = threading.Thread(target=run_tab, args=(session, tab_id), daemon=True)
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
    menu_cls = svc.get("menu_class", "")
    unit = svc["unit"]
    emoji = svc["emoji"]
    # Panel-scoped selectors (2026 DOM)
    panel_sel = f".{menu_cls}" if menu_cls else ""
    input_panel_sel = f".{menu_cls} input[placeholder='Enter Video URL']:visible" if menu_cls else 'input[placeholder="Enter Video URL"]:visible, input[type="search"]:visible'
    submit_panel_sel = f".{menu_cls} button[type='submit']" if menu_cls else 'button:has-text("Search"):visible'
    results_panel_sel = f".{menu_cls} div[id]" if menu_cls else ""
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
                # Force a fresh Tor IP on every restart so a blocked/dead exit
                # node isn't reused.
                if USING_TOR:
                    session.log("🧅 Requesting fresh Tor IP...")
                    renew_tor_circuit()
            else:
                if multi:
                    session.log(f"\U0001f680 Starting tab...")

            browser = None
            page = None

            # Acquire a global browser slot (blocks if all 3 are in use)
            got_slot = False
            try:
                if not _browser_semaphore.acquire(timeout=1):
                    session.log(f"⏳ Waiting for browser slot (max {MAX_GLOBAL_BROWSERS} globally)...")
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

                    browser = p.chromium.launch(slow_mo=100, **launch_opts)  # 0.1s delay between every Playwright action
                    page = browser.new_page(viewport={"width": 800, "height": 600})
                    page.on("dialog", lambda d: d.accept())

                    def _safe_check(pg):
                        """Check if page is alive. Returns True if OK, False if crashed."""
                        try:
                            pg.title()
                            return True
                        except:
                            return False

                    # ── Load zefoy ──
                    session.log("\U0001f310 Loading zefoy.com...")
                    try:
                        page.goto(ZEFOY, wait_until="domcontentloaded", timeout=60000)
                    except Exception as _goto_err:
                        session.log(f"\U0001f4a5 Page crashed on load ({_goto_err}), restarting...")
                        continue
                    time.sleep(5)

                    # Inject anti-detection scripts (mouse simulation, cookie, overlay cleanup)
                    inject_anti_detection(page)

                    if not _safe_check(page):
                        session.log("\U0001f4a5 Page crashed on load, restarting...")
                        continue

                    # ── Check page / Solve captcha ──
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
                        try:
                            page.reload(wait_until="domcontentloaded")
                        except Exception as _reload_err:
                            session.log(f"\u26a0\ufe0f Error: {_reload_err} \u2014 restarting tab...")
                            break
                        time.sleep(10 + page_attempt * 3)
                    else:
                        session.log("\u26a0\ufe0f Page never became ready, restarting...")
                        continue

                    if not captcha_detected and not page_ready:
                        continue

                    if captcha_detected:
                        session.log(f"⏳ Waiting for a captcha-solving slot (max {CAPTCHA_CONCURRENCY} concurrent)...")
                        try:
                            _captcha_semaphore.acquire()
                            session.log("\U0001f510 Acquired captcha-solving slot, starting...")
                            try:
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
                                        
                                        # Remove overlays before clicking input
                                        remove_overlays(page)
                                        time.sleep(0.5)
                                        
                                        # Try multiple captcha input selectors with fallback
                                        captcha_input = None
                                        selectors = [
                                            "#captchatoken",
                                            "input[name='captcha_secure']",
                                            "input[placeholder*='aptcha' i]",
                                            "input[placeholder*='answer' i]",
                                            "form input[type='text']:nth-child(1)",
                                            ".captcha-input",
                                            "input.form-control[name*='captcha']"
                                        ]
                                        
                                        for selector in selectors:
                                            try:
                                                test_input = page.locator(selector).first
                                                test_input.wait_for(state="visible", timeout=3000)
                                                captcha_input = test_input
                                                session.log(f"✓ Found captcha input: {selector}")
                                                break
                                            except:
                                                continue
                                        
                                        if captcha_input:
                                            captcha_input.fill(answer)
                                            time.sleep(0.5)
                                        else:
                                            session.log(f"⚠️ Could not find captcha input, taking screenshot for debug...")
                                            page.screenshot(path="static/captcha_debug.png")
                                            continue
                                        
                                        # Remove overlays before clicking submit
                                        remove_overlays(page)
                                        time.sleep(0.3)
                                        
                                        page.locator("button.submit-captcha, form .btn-primary[type='submit']").first.click()
                                        time.sleep(5)

                                        try:
                                            page.locator(ANY_SERVICE_BUTTON).first.wait_for(timeout=8000)
                                            session.log("\u2705 Captcha solved!")
                                            save_live_screenshot(page)
                                            inject_anti_detection(page)
                                            captcha_solved = True
                                            break
                                        except:
                                            session.log(f"\u274c Wrong answer '{answer}', retrying...")
                                            try: 
                                                # Tightened selector for dismiss button
                                                page.locator(".swal2-popup .swal2-confirm, .swal2-confirm, .modal.show .btn-secondary, .modal.show .close").first.click()
                                            except: pass
                                            time.sleep(1)
                                            try: page.locator(".refresh-capthca-btn-new, [onclick*='refresh'], .captcha-refresh").first.click()
                                            except: pass
                                            time.sleep(3)
                                    except Exception as e:
                                        if _is_dead(e):
                                            session.log(f"\U0001f4a5 Crashed during captcha, restarting...")
                                            break
                                        else:
                                            session.log(f"\u26a0\ufe0f Captcha error: {e}")
                                        time.sleep(2)

                                if not captcha_solved:
                                    continue

                            finally:
                                _captcha_semaphore.release()
                                session.log("🟢 Released captcha-solving slot")
                        except Exception as e:
                            session.log(f"\u274c Captcha semaphore error: {e}")
                            continue

                    # ── Click service button (with overlay removal) ──
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

                    # Safe click: scroll into view, try normal click first, then forced if needed
                    try:
                        btn_element = page.locator(f".{btn_cls}").first
                        btn_element.scroll_into_view_if_needed()
                        time.sleep(0.5)
                        remove_overlays(page)
                        time.sleep(0.3)
                        try:
                            btn_element.click(timeout=5000)
                        except:
                            # Fallback to forced click after clearing overlays again
                            remove_overlays(page)
                            time.sleep(0.3)
                            btn_element.click(force=True, timeout=10000)
                    except Exception as btn_err:
                        if _is_dead(btn_err):
                            session.log(f"\U0001f4a5 Crashed clicking {svc_name} button, restarting...")
                            continue
                        session.log(f"\u26a0\ufe0f Error clicking button: {btn_err}, restarting...")
                        continue

                    time.sleep(2)
                    inject_anti_detection(page)
                    session.log(f"\u2705 {svc_name} panel opened!")

                    backoff = 5
                    url_filled = False
                    input_fail_count = 0
                    MAX_INPUT_FAILS = 5

                    # ── Main loop ──
                    while not session.stop_event.is_set():
                        if not _safe_check(page):
                            session.log("💥 Page crashed in main loop, restarting...")
                            break

                        cycle = session.add_cycle()
                        session.log(f"🔄 Cycle {cycle}")

                        try:
                            url_input = page.locator(input_panel_sel).first

                            try:
                                url_input.wait_for(state="visible", timeout=5000)
                                input_fail_count = 0
                            except:
                                # Input not visible — re-open panel
                                session.log(f"⚠️ Input not visible, re-opening {svc_name} panel...")
                                try:
                                    remove_overlays(page)
                                    time.sleep(0.3)
                                    remove_overlays(page)
                                    time.sleep(0.3)
                                    page.locator(f".{btn_cls}").first.click(force=True)
                                    time.sleep(2)
                                    url_input.wait_for(state="visible", timeout=10000)
                                    input_fail_count = 0
                                except:
                                    input_fail_count += 1
                                    # Detect why the panel is broken: zefoy down, session
                                    # kicked back to captcha, or page navigated away
                                    try:
                                        body_snip = page.inner_text("body")[:300].lower()
                                    except:
                                        session.log("💥 Page unreadable, restarting browser...")
                                        break
                                    if "502" in body_snip or "bad gateway" in body_snip or "503" in body_snip:
                                        session.log("🔴 Zefoy is down (502/503), restarting browser...")
                                        break
                                    if page.locator("#captcha-img, img[src*='captcha'], img[src*='CAPTCHA']").count() > 0:
                                        session.log("🔐 Session expired (captcha shown again), restarting browser...")
                                        break
                                    if input_fail_count >= MAX_INPUT_FAILS:
                                        session.log(f"❌ Input not found after {MAX_INPUT_FAILS} attempts, restarting browser...")
                                        break
                                    session.log(f"⚠️ Still can't find input after re-open, retrying ({input_fail_count}/{MAX_INPUT_FAILS})...")
                                    time.sleep(3)
                                    continue
                                url_filled = False  # panel reopened, need to fill again

                            # Only fill URL the first time (or after panel re-open)
                            if not url_filled:
                                url_input.fill("")
                                time.sleep(0.3)
                                url_input.fill(session.video_url)
                                time.sleep(1)
                                url_filled = True
                                session.log(f"✅ URL filled")
                                save_live_screenshot(page)

                            # Click Search (with overlay removal) — panel-scoped
                            submit_sel = submit_panel_sel
                            remove_overlays(page)
                            time.sleep(0.3)
                            page.locator(submit_sel).first.click()
                            time.sleep(3)
                            save_live_screenshot(page)
                        except Exception as fill_err:
                            if _is_dead(fill_err):
                                session.log("💥 Crashed filling URL, restarting...")
                                break
                            session.log(f"⚠️ Error: {fill_err}")
                            time.sleep(3)
                            continue

                        # ── Comment Hearts: click 💬, find user, select 100, click heart, loop ──
                        if session.service == "comment_hearts":
                            target_user = session.username.lstrip('@').lower()

                            # A: Check page state
                            try:
                                body_check = page.inner_text("body").lower()
                            except:
                                session.log("💥 Page crash, restarting...")
                                break

                            # Too many requests? Just click Search again
                            if "too many" in body_check or "slow down" in body_check:
                                session.log("⚠️ Too many requests, clicking Search...")
                                try:
                                    remove_overlays(page)
                                    time.sleep(0.3)
                                    page.locator(submit_panel_sel).first.click()
                                except:
                                    pass
                                time.sleep(3)
                                continue

                            # Countdown? Wait it out, then click Search
                            if "please wait" in body_check and ("minute" in body_check or "second" in body_check):
                                wait_secs = parse_wait_time(body_check)
                                if wait_secs <= 0:
                                    wait_secs = 60
                                wait_secs += 3
                                session.log(f"⏳ Countdown: {wait_secs}s")
                                for remaining in range(wait_secs, 0, -1):
                                    if session.stop_event.is_set():
                                        break
                                    mins = remaining // 60
                                    secs = remaining % 60
                                    time_str = f"{mins}m {secs:02d}s" if mins > 0 else f"{secs}s"
                                    session.set_countdown(f"⏳ {time_str}")
                                    time.sleep(1)
                                session.set_countdown("")
                                session.log("✅ Countdown done — clicking Search...")
                                try:
                                    remove_overlays(page)
                                    time.sleep(0.3)
                                    page.locator(submit_panel_sel).first.click()
                                except:
                                    pass
                                time.sleep(3)
                                continue

                            # B: Wait for 💬 count button and click it
                            if page.locator(".kadi-rengi").count() > 0:
                                session.log("💬 Comments already visible")
                            else:
                                try:
                                    count_btn = page.locator(f"{HEARTS_BTN_SEL}:visible, button.wbutton:visible").first
                                    count_btn.wait_for(state="visible", timeout=20000)
                                    remove_overlays(page)
                                    time.sleep(0.3)
                                    count_btn.click()
                                    time.sleep(4)
                                    save_live_screenshot(page)
                                    session.log("💬 Comments loaded")
                                except:
                                    try:
                                        snippet = page.inner_text("body")[:150]
                                        session.log(f"⚠️ 💬 button not found. Panel: {snippet}")
                                    except:
                                        session.log("⚠️ 💬 button not found, panel unreadable")
                                    # Click Search to retry
                                    try:
                                        remove_overlays(page)
                                        time.sleep(0.3)
                                        page.locator(submit_panel_sel).first.click()
                                    except:
                                        pass
                                    time.sleep(3)
                                    continue

                            # C: Find target username — paginate through ALL comment pages
                            found_user = False
                            crashed = False
                            max_pages = 250  # up to ~10,000 comments at 40/page
                            for pg in range(max_pages):
                                if session.stop_event.is_set():
                                    break
                                try:
                                    result = page.evaluate("""(targetUser) => {
                                        const forms = document.querySelectorAll('form.w1a');
                                        const users = [];
                                        for (let i = 0; i < forms.length; i++) {
                                            const userEl = forms[i].querySelector('.kadi-rengi');
                                            if (!userEl) continue;
                                            const uname = userEl.innerText.trim().replace('@','').toLowerCase();
                                            users.push(uname);
                                            if (uname === targetUser) {
                                                return {found: true, index: i, total: forms.length};
                                            }
                                        }
                                        const nextBtn = document.querySelector('li[title="Next"] button');
                                        const hasNext = nextBtn && !nextBtn.disabled;
                                        return {found: false, total: forms.length, users: users, hasNext: hasNext};
                                    }""", target_user)

                                    if result.get('found'):
                                        idx = result['index']
                                        form_loc = page.locator("form.w1a").nth(idx)
                                        form_loc.locator("select[name='select_lmt']").select_option("100")
                                        time.sleep(1)
                                        remove_overlays(page)
                                        time.sleep(0.3)
                                        form_loc.locator("button[type='submit']").click()
                                        session.log(f"💬 Sent 100 hearts to @{target_user} (page {pg + 1})")
                                        found_user = True
                                        time.sleep(3)
                                        break

                                    if result.get('hasNext'):
                                        if pg == 0:
                                            session.log(f"🔍 @{target_user} not on page 1, paginating...")
                                        page.locator('li[title="Next"] button').click()
                                        time.sleep(4)
                                    else:
                                        total_scanned = (pg * 40) + result.get('total', 0)
                                        session.log(f"❌ @{target_user} not found in {total_scanned} comments ({pg + 1} pages)")
                                        break
                                except Exception as ce:
                                    if _is_dead(ce):
                                        crashed = True
                                        session.log("💥 Crashed during pagination, restarting...")
                                        break
                                    session.log(f"⚠️ Pagination error: {ce}")
                                    break

                            if crashed:
                                break
                            if not found_user:
                                time.sleep(2)
                                try:
                                    remove_overlays(page)
                                    time.sleep(0.3)
                                    page.locator(submit_panel_sel).first.click()
                                except:
                                    pass
                                time.sleep(3)
                                continue

                            # D: Check result — no countdown for comment hearts
                            try:
                                body = page.inner_text("body").lower()
                            except:
                                break

                            if "successfully" in body:
                                session.add_count(100)
                                session.log(f"💬 +100 hearts to @{target_user} (total: {session.total_count})")
                            elif "too many" in body or "slow down" in body:
                                session.log("⚠️ Too many requests")

                            # Click Search to go again
                            time.sleep(2)
                            try:
                                remove_overlays(page)
                                time.sleep(0.3)
                                page.locator(submit_panel_sel).first.click()
                            except:
                                pass
                            time.sleep(3)
                            continue  # Skip regular hearts response handler

                        # ── Step 2: Check what happened after Search ──
                        max_checks = 60
                        crashed = False
                        for check_i in range(max_checks):
                            if session.stop_event.is_set():
                                break

                            try:
                                body = page.inner_text("body")
                            except Exception as e:
                                if _is_dead(e):
                                    crashed = True
                                    break
                                time.sleep(1)
                                continue

                            lower_body = body.lower()

                            # ── "Too many requests" → just click Search again ──
                            if "too many" in lower_body or "slow down" in lower_body:
                                session.log("⚠️ Too many requests — clicking Search again...")
                                time.sleep(2)
                                try:
                                    submit_sel = submit_panel_sel
                                    remove_overlays(page)
                                    time.sleep(0.3)
                                    page.locator(submit_sel).first.click()
                                    time.sleep(3)
                                except:
                                    pass
                                continue

                            # ── Countdown / rate limit → wait, then click Search 2x ──
                            if ("please wait" in lower_body and ("minute" in lower_body or "second" in lower_body)):
                                wait_secs = parse_wait_time(body)
                                if wait_secs <= 0:
                                    wait_secs = 60
                                wait_secs += 3
                                session.log(f"⏳ Countdown: {wait_secs}s")

                                for remaining in range(wait_secs, 0, -1):
                                    if session.stop_event.is_set():
                                        break
                                    mins = remaining // 60
                                    secs = remaining % 60
                                    time_str = f"{mins}m {secs:02d}s" if mins > 0 else f"{secs}s"
                                    session.set_countdown(f"⏳ {time_str}")
                                    time.sleep(1)
                                session.set_countdown("")

                                # Click Search 2 times after countdown
                                session.log("✅ Countdown done — clicking Search 2x...")
                                try:
                                    submit_sel = submit_panel_sel
                                    remove_overlays(page)
                                    time.sleep(0.3)
                                    page.locator(submit_sel).first.click()
                                    time.sleep(1)
                                    page.locator(submit_sel).first.click()
                                    time.sleep(3)
                                except:
                                    pass
                                continue

                            # ── "READY" text → click Search ──
                            if "ready" in lower_body and "next submit" in lower_body:
                                session.log("✅ Ready — clicking Search...")
                                try:
                                    submit_sel = submit_panel_sel
                                    remove_overlays(page)
                                    time.sleep(0.3)
                                    page.locator(submit_sel).first.click()
                                    time.sleep(3)
                                except:
                                    pass
                                continue

                            # ── Success message ──
                            if "successfully" in lower_body:
                                count = 0
                                for line in body.split('\n'):
                                    if 'successfully' in line.lower():
                                        session.log(f"📝 Raw: {line.strip()[:120]}")
                                        try:
                                            nums = [int(m) for m in re.findall(r'\d+', line) if not (2020 <= int(m) <= 2035) and int(m) < 100000]
                                        except:
                                            nums = []
                                        if nums:
                                            count = max(nums)
                                        break
                                new_total = session.add_count(count)
                                if count > 0:
                                    session.log(f"🎉 +{count} {unit}! Total: {new_total:,}")
                                else:
                                    session.log(f"✅ Success (count not captured). Total: {new_total:,}")
                                break

                            # ── Send button visible → click it (btn-dark / wbutton / btn-success) ──
                            send_clicked = False
                            try:
                                remove_overlays(page)
                                time.sleep(0.3)
                                # Look for send buttons inside the panel's results container first, then globally
                                _send_selectors = []
                                if panel_sel:
                                    _send_selectors += [f'{panel_sel} {HEARTS_BTN_SEL}:visible', f'{panel_sel} button.btn-dark:visible', f'{panel_sel} button.wbutton:visible', f'{panel_sel} button.btn-success:visible']
                                _send_selectors += [f'{HEARTS_BTN_SEL}:visible', 'button.btn-dark:visible', 'button.wbutton:visible', 'button.btn-success:visible']
                                for send_sel in _send_selectors:
                                    try:
                                        send_btn = page.locator(send_sel).first
                                        if send_btn.is_visible(timeout=2000):
                                            send_btn.click()
                                            send_clicked = True
                                            session.log(f"{emoji} Clicked send button!")
                                            time.sleep(3)
                                            save_live_screenshot(page)
                                            break
                                    except:
                                        continue
                                if not send_clicked:
                                    # JS fallback: click any visible btn-dark or wbutton
                                    send_clicked = page.evaluate("""() => {
                                        const btns = document.querySelectorAll('button');
                                        for (const b of btns) {
                                            const cls = b.className || '';
                                            const rect = b.getBoundingClientRect();
                                            if (cls.includes('btn-dark') && rect.width > 0 && rect.height > 0) {
                                                b.click(); return true;
                                            }
                                        }
                                        for (const b of btns) {
                                            const cls = b.className || '';
                                            const rect = b.getBoundingClientRect();
                                            if (cls.includes('wbutton') && rect.width > 0 && rect.height > 0) {
                                                b.click(); return true;
                                            }
                                        }
                                        return false;
                                    }""")
                                    if send_clicked:
                                        session.log(f"{emoji} Clicked send button (JS fallback)!")
                                        time.sleep(3)
                                if send_clicked:
                                    continue
                            except:
                                pass

                            # ── Still loading / waiting ──
                            if check_i < 30:
                                time.sleep(1)
                                continue
                            else:
                                session.log(f"⚠️ No response after {check_i}s, breaking to retry...")
                                break

                        if crashed:
                            session.log("💥 Crashed in main loop, restarting tab...")
                            break

                        time.sleep(2)
                        if cycle % 10 == 0:
                            gc.collect()


            except Exception as inner_err:
                if _is_dead(inner_err):
                    session.log(f"\U0001f4a5 Browser crashed, restarting tab...")
                else:
                    import traceback
                    session.log(f"\u26a0\ufe0f Error: {inner_err} \u2014 restarting tab...")
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


# ═══════════════════════════════════════════════════════════════
#  ROUTES
# ═══════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return render_template("index.html")


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
    return jsonify({"sessions": data, "browsers": _active_browsers, "maxBrowsers": MAX_GLOBAL_BROWSERS, "captchaConcurrency": CAPTCHA_CONCURRENCY})


def normalize_url(url):
    """Clean and normalize URL: remove whitespace, newlines, query params"""
    # Remove all whitespace and newlines
    url = url.strip().replace('\n', '').replace('\r', '').replace('\t', '')
    # Remove query parameters (everything after ?)
    if '?' in url:
        url = url.split('?')[0]
    # Remove trailing slashes for consistency
    url = url.rstrip('/')
    return url

@app.route("/start", methods=["POST"])
def start():
    data = request.get_json()
    url = normalize_url(data.get("url", ""))
    service = data.get("service", "views").strip().lower()
    username = data.get("username", "").strip()
    tabs = int(data.get("tabs", 1))
    if not url:
        return jsonify({"error": "No URL provided"}), 400
    if service not in SERVICES:
        return jsonify({"error": f"Unknown service: {service}"}), 400
    if service == "comment_hearts" and not username:
        return jsonify({"error": "Username is required for Comment Hearts"}), 400

    session = Session(url, service=service, num_tabs=tabs, username=username)

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


@app.route("/api/live-screenshot")
def get_live_screenshot():
    """Serve the latest screenshot for live preview on the frontend"""
    import os
    screenshot_path = "static/live.png"
    if os.path.exists(screenshot_path):
        return send_file(screenshot_path, mimetype='image/png')
    else:
        # Return empty/placeholder if no screenshot yet
        return jsonify({"error": "No screenshot available yet"}), 404


@app.route("/remove/<int:sid>", methods=["POST"])
def remove_session(sid):
    """Delete a session. If it's still running, signal its tabs to stop first;
    the daemon threads wind down and release their browser slots on their own."""
    with sessions_lock:
        session = sessions.get(sid)
        if not session:
            return jsonify({"error": "Not found"}), 404
        session.stop_event.set()
        session.status = "stopping"
        del sessions[sid]
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=8080)
