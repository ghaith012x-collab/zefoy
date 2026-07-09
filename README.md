# 🎥 Live Browser Cam

Multi-tab browser with live screenshot streaming — every tab is a live cam feed.

## Setup

```bash
pip install -r requirements.txt
playwright install chromium
python app.py
```

Then open **http://localhost:5000**

## How it works

| Part | What it does |
|------|------------|
| Background thread | Captures screenshots of all tabs every 0.5 s → `static/screenshots/live_{tab_id}.png` |
| `/api/live-screenshot/<tab_id>` | Serves the latest PNG with `Cache-Control: no-store` |
| Frontend | `<img>` refreshes every 1 s with `?t=Date.now()` cache-buster |

## API

| Method | Endpoint | Description |
|--------|----------|----------|
| GET | `/api/tabs` | List all tabs |
| POST | `/api/tabs` | Open new tab `{url}` |
| DELETE | `/api/tabs/:id` | Close tab |
| POST | `/api/tabs/:id/navigate` | Go to URL `{url}` |
| POST | `/api/tabs/:id/click` | Click `{selector}` or `{x,y}` |
| POST | `/api/tabs/:id/type` | Type text `{text}` or fill `{selector,text}` |
| POST | `/api/tabs/:id/scroll` | Scroll `{dy}` |
| POST | `/api/tabs/:id/back` | Browser back |
| POST | `/api/tabs/:id/forward` | Browser forward |
| POST | `/api/tabs/:id/refresh` | Reload page |
| GET | `/api/live-screenshot/:id` | Latest screenshot PNG |

## Railway / Production notes

- Screenshots are stored on the same filesystem — Railway's ephemeral disk is shared between processes, so the screenshot written by the Playwright thread is immediately readable by Flask.
- For persistent storage, mount a Railway volume at `./static/screenshots`.
- Set `PORT` env var; Railway sets it automatically: `app.run(port=int(os.environ.get("PORT", 5000)))`.