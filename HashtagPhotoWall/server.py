#!/usr/bin/env python3
"""Hashtag Photo Wall - local server.

Runs on your Mac with only the Python standard library.
  Control panel (your laptop screen):   http://localhost:8765/
  Display (drag to the extended screen): http://localhost:8765/display

Approved photos are saved in the ApprovedPhotos folder next to this file.
Anything placed in that folder (even by hand in Finder) shows on the display.
"""

import hashlib
import html
import json
import mimetypes
import os
import re
import shutil
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(APP_DIR, "static")
PHOTO_DIR = os.path.join(APP_DIR, "ApprovedPhotos")
REMOVED_DIR = os.path.join(PHOTO_DIR, "Removed")
META_FILE = os.path.join(PHOTO_DIR, "photos.json")
SETTINGS_FILE = os.path.join(APP_DIR, "settings.json")
CONFIG_FILE = os.path.join(APP_DIR, "config.json")

PORT = int(os.environ.get("PHOTOWALL_PORT", "8765"))
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif"}
MAX_UPLOAD = 40 * 1024 * 1024
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)

DEFAULT_SETTINGS = {
    "title": "Youth Night",
    "hashtag": "#youthnight",
    "interval": 7,
    "showCaptions": True,
    "showBanner": True,
    "newestFirst": True,
}
DEFAULT_CONFIG = {"access_token": "", "ig_user_id": "", "graph_version": ""}

lock = threading.Lock()


# ---------------------------------------------------------------- storage

def read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        merged = dict(default)
        merged.update(data)
        return merged
    except (OSError, ValueError):
        return dict(default)


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def load_meta():
    try:
        with open(META_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_meta(meta):
    write_json(META_FILE, meta)


def list_photos():
    meta = load_meta()
    photos = []
    for name in os.listdir(PHOTO_DIR):
        path = os.path.join(PHOTO_DIR, name)
        ext = os.path.splitext(name)[1].lower()
        if name.startswith(".") or not os.path.isfile(path) or ext not in IMAGE_EXTS:
            continue
        info = meta.get(name, {})
        photos.append({
            "name": name,
            "url": "/photos/" + urllib.parse.quote(name),
            "added": info.get("added") or os.path.getmtime(path),
            "caption": info.get("caption", ""),
            "username": info.get("username", ""),
            "permalink": info.get("permalink", ""),
            "source": info.get("source", "folder"),
        })
    photos.sort(key=lambda p: p["added"])
    return photos


def safe_name(name):
    name = os.path.basename(name or "")
    name = re.sub(r"[^A-Za-z0-9._ -]", "_", name).strip(" .")
    return name or "photo"


def unique_path(name):
    base, ext = os.path.splitext(name)
    candidate = name
    n = 2
    while os.path.exists(os.path.join(PHOTO_DIR, candidate)):
        candidate = f"{base}-{n}{ext}"
        n += 1
    return candidate


def ext_for(content_type, fallback_url=""):
    ct = (content_type or "").split(";")[0].strip().lower()
    mapping = {
        "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png",
        "image/gif": ".gif", "image/webp": ".webp", "image/heic": ".heic",
        "image/heif": ".heif",
    }
    if ct in mapping:
        return mapping[ct]
    path_ext = os.path.splitext(urllib.parse.urlparse(fallback_url).path)[1].lower()
    return path_ext if path_ext in IMAGE_EXTS else ".jpg"


def store_image(data, ext, info, preferred_name=None):
    if not data:
        raise ValueError("The image was empty.")
    with lock:
        meta = load_meta()
        digest = hashlib.sha1(data).hexdigest()[:12]
        for existing, m in meta.items():
            if m.get("sha1") == digest and os.path.exists(os.path.join(PHOTO_DIR, existing)):
                return existing, False
        name = safe_name(preferred_name or f"photo_{digest}")
        if os.path.splitext(name)[1].lower() not in IMAGE_EXTS:
            name += ext
        name = unique_path(name)
        with open(os.path.join(PHOTO_DIR, name), "wb") as f:
            f.write(data)
        entry = {k: v for k, v in info.items() if v}
        entry["sha1"] = digest
        entry["added"] = time.time()
        meta[name] = entry
        save_meta(meta)
        return name, True


# ---------------------------------------------------------------- network

def http_get(url, max_bytes=MAX_UPLOAD):
    """GET a URL. Falls back to macOS curl if Python's SSL certificates are missing
    (common with python.org installs that skipped 'Install Certificates')."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = resp.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise ValueError("File is too large.")
            return resp.status, resp.headers.get("Content-Type", ""), data
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", ""), e.read()
    except urllib.error.URLError as e:
        if not isinstance(getattr(e, "reason", None), ssl.SSLError) or not shutil.which("curl"):
            raise
    return curl_get(url, max_bytes)


def curl_get(url, max_bytes):
    marker = "\n__CT__:%{content_type}__CODE__:%{http_code}"
    out = subprocess.run(
        ["curl", "-sSL", "--max-time", "25", "-A", USER_AGENT, "-w", marker, url],
        capture_output=True, check=True,
    ).stdout
    idx = out.rfind(b"\n__CT__:")
    body, tail = out[:idx], out[idx:].decode("utf-8", "replace")
    m = re.search(r"__CT__:(.*)__CODE__:(\d+)", tail)
    if len(body) > max_bytes:
        raise ValueError("File is too large.")
    return int(m.group(2)), m.group(1), body


def graph(path, params):
    cfg = read_json(CONFIG_FILE, DEFAULT_CONFIG)
    if not cfg["access_token"]:
        raise ValueError("Instagram is not connected yet. Add your access token in step 5.")
    version = cfg.get("graph_version", "").strip().strip("/")
    base = "https://graph.facebook.com/" + (version + "/" if version else "")
    params = dict(params, access_token=cfg["access_token"])
    status, _, body = http_get(base + path.lstrip("/") + "?" + urllib.parse.urlencode(params), 5_000_000)
    try:
        data = json.loads(body.decode("utf-8"))
    except ValueError:
        raise ValueError(f"Instagram returned an unexpected response (HTTP {status}).")
    if "error" in data:
        err = data["error"]
        raise ValueError(f"Instagram error: {err.get('message', 'unknown')} (code {err.get('code')})")
    return data


def hashtag_search(tag, kind):
    cfg = read_json(CONFIG_FILE, DEFAULT_CONFIG)
    if not cfg["access_token"]:
        raise ValueError("Instagram is not connected yet. Add your access token in step 5.")
    if not cfg["ig_user_id"]:
        raise ValueError("Missing Instagram Business account ID. Use 'Save token & find my account' in step 5.")
    tag = tag.strip().lstrip("#").replace(" ", "")
    if not tag:
        raise ValueError("Type a hashtag first.")
    found = graph("ig_hashtag_search", {"user_id": cfg["ig_user_id"], "q": tag})
    if not found.get("data"):
        return []
    tag_id = found["data"][0]["id"]
    edge = "top_media" if kind == "top" else "recent_media"
    fields = "id,caption,media_type,media_url,permalink,timestamp,children{media_url,media_type}"
    media = graph(f"{tag_id}/{edge}", {"user_id": cfg["ig_user_id"], "fields": fields, "limit": 50})
    results = []
    for item in media.get("data", []):
        images = []
        if item.get("media_type") == "IMAGE" and item.get("media_url"):
            images.append(item["media_url"])
        elif item.get("media_type") == "CAROUSEL_ALBUM":
            for child in item.get("children", {}).get("data", []):
                if child.get("media_type") == "IMAGE" and child.get("media_url"):
                    images.append(child["media_url"])
        for i, url in enumerate(images):
            results.append({
                "key": f"{item['id']}_{i}",
                "image": url,
                "caption": item.get("caption", ""),
                "permalink": item.get("permalink", ""),
                "timestamp": item.get("timestamp", ""),
            })
    return results


def find_accounts():
    data = graph("me/accounts", {"fields": "name,instagram_business_account{id,username}"})
    out = []
    for page in data.get("data", []):
        ig = page.get("instagram_business_account")
        if ig:
            out.append({"page": page.get("name", ""), "id": ig["id"], "username": ig.get("username", "")})
    return out


def download_image(url, info, preferred_name=None):
    status, ctype, data = http_get(url)
    if status >= 400:
        raise ValueError(f"Could not download the image (HTTP {status}).")
    if not ctype.lower().startswith("image/"):
        raise ValueError("That link is not an image.")
    return store_image(data, ext_for(ctype, url), info, preferred_name)


def add_from_link(url):
    url = url.strip()
    if not re.match(r"^https?://", url):
        raise ValueError("Paste a full link starting with https://")
    status, ctype, data = http_get(url)
    if status >= 400:
        raise ValueError(f"Could not open that link (HTTP {status}).")
    if ctype.lower().startswith("image/"):
        return store_image(data, ext_for(ctype, url), {"source": "link"})
    # A web page (e.g. an Instagram post): look for its preview image.
    page = data.decode("utf-8", "replace")
    img = meta_tag(page, "og:image") or meta_tag(page, "twitter:image")
    if not img:
        raise ValueError(
            "Couldn't get a photo from that page. Instagram often hides photos from "
            "logged-out visitors: save or screenshot the photo and drag it in instead."
        )
    desc = meta_tag(page, "og:description") or ""
    title = meta_tag(page, "og:title") or ""
    username = ""
    m = re.search(r"@([A-Za-z0-9._]{2,30})", title + " " + desc)
    if m:
        username = m.group(1)
    caption = ""
    m = re.search(r':\s*["“](.+)["”]\s*\.?$', desc, re.S)
    if m:
        caption = m.group(1).strip()
    info = {"source": "link", "permalink": url, "username": username, "caption": caption}
    return download_image(html.unescape(img), info)


def meta_tag(page, prop):
    for pattern in (
        r'<meta[^>]+(?:property|name)=["\']%s["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']%s["\']',
    ):
        m = re.search(pattern % re.escape(prop), page, re.I)
        if m:
            return html.unescape(m.group(1))
    return None


# ---------------------------------------------------------------- web server

class Handler(BaseHTTPRequestHandler):
    server_version = "PhotoWall/1.0"

    def log_message(self, fmt, *args):
        pass

    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, cache=False):
        if not os.path.isfile(path):
            return self.send_error(404)
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        with open(path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "max-age=86400" if cache else "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_UPLOAD:
            raise ValueError("File is too large (40 MB max).")
        return self.rfile.read(length) if length else b""

    def read_json_body(self):
        try:
            return json.loads(self.read_body().decode("utf-8") or "{}")
        except ValueError:
            return {}

    def same_origin(self):
        # Only accept changes from pages served by this app (blocks other websites).
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = urllib.parse.urlparse(origin).netloc
        return host == self.headers.get("Host")

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/control"):
            return self.send_file(os.path.join(STATIC_DIR, "control.html"))
        if path == "/display":
            return self.send_file(os.path.join(STATIC_DIR, "display.html"))
        if path == "/api/photos":
            photos = list_photos()
            stamp = hashlib.sha1("|".join(p["name"] for p in photos).encode()).hexdigest()[:10]
            return self.send_json({"photos": photos, "version": stamp})
        if path == "/api/settings":
            return self.send_json(read_json(SETTINGS_FILE, DEFAULT_SETTINGS))
        if path == "/api/config":
            cfg = read_json(CONFIG_FILE, DEFAULT_CONFIG)
            token = cfg["access_token"]
            return self.send_json({
                "connected": bool(token and cfg["ig_user_id"]),
                "has_token": bool(token),
                "token_hint": ("…" + token[-6:]) if token else "",
                "ig_user_id": cfg["ig_user_id"],
                "graph_version": cfg["graph_version"],
            })
        if path.startswith("/photos/"):
            name = urllib.parse.unquote(path[len("/photos/"):])
            if name != os.path.basename(name):
                return self.send_error(404)
            return self.send_file(os.path.join(PHOTO_DIR, name), cache=True)
        if path.startswith("/static/"):
            name = os.path.basename(path)
            return self.send_file(os.path.join(STATIC_DIR, name), cache=True)
        self.send_error(404)

    def do_POST(self):
        if not self.same_origin():
            return self.send_json({"error": "Blocked"}, 403)
        path = urllib.parse.urlparse(self.path).path
        try:
            if path == "/api/upload":
                name = urllib.parse.unquote(self.headers.get("X-Filename", "photo.jpg"))
                data = self.read_body()
                ext = ext_for(self.headers.get("Content-Type"), name)
                saved, new = store_image(data, ext, {"source": "upload"}, name)
                return self.send_json({"ok": True, "name": saved, "new": new})

            body = self.read_json_body()
            if path == "/api/search":
                return self.send_json({"results": hashtag_search(body.get("hashtag", ""), body.get("kind"))})
            if path == "/api/approve":
                info = {
                    "source": "instagram",
                    "caption": body.get("caption", ""),
                    "permalink": body.get("permalink", ""),
                    "username": body.get("username", ""),
                }
                saved, new = download_image(body["image"], info, "ig_" + safe_name(body.get("key", "")))
                return self.send_json({"ok": True, "name": saved, "new": new})
            if path == "/api/link":
                saved, new = add_from_link(body.get("url", ""))
                return self.send_json({"ok": True, "name": saved, "new": new})
            if path == "/api/remove":
                name = os.path.basename(body.get("name", ""))
                src = os.path.join(PHOTO_DIR, name)
                if os.path.isfile(src):
                    os.makedirs(REMOVED_DIR, exist_ok=True)
                    shutil.move(src, os.path.join(REMOVED_DIR, name))
                return self.send_json({"ok": True})
            if path == "/api/caption":
                with lock:
                    meta = load_meta()
                    entry = meta.setdefault(os.path.basename(body.get("name", "")), {})
                    entry["caption"] = body.get("caption", "")
                    entry["username"] = body.get("username", "").lstrip("@")
                    save_meta(meta)
                return self.send_json({"ok": True})
            if path == "/api/settings":
                settings = read_json(SETTINGS_FILE, DEFAULT_SETTINGS)
                for key in DEFAULT_SETTINGS:
                    if key in body:
                        settings[key] = body[key]
                settings["interval"] = max(2, min(120, int(settings.get("interval") or 7)))
                write_json(SETTINGS_FILE, settings)
                return self.send_json(settings)
            if path == "/api/config":
                cfg = read_json(CONFIG_FILE, DEFAULT_CONFIG)
                for key in DEFAULT_CONFIG:
                    if body.get(key) is not None and (key != "access_token" or body[key]):
                        cfg[key] = str(body[key]).strip()
                write_json(CONFIG_FILE, cfg)
                return self.send_json({"ok": True})
            if path == "/api/accounts":
                return self.send_json({"accounts": find_accounts()})
            if path == "/api/open-folder":
                if sys.platform == "darwin":
                    subprocess.Popen(["open", PHOTO_DIR])
                return self.send_json({"ok": True, "path": PHOTO_DIR})
            self.send_error(404)
        except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as e:
            msg = str(e) if isinstance(e, ValueError) else f"{type(e).__name__}: {e}"
            self.send_json({"error": msg}, 400)


def main():
    os.makedirs(PHOTO_DIR, exist_ok=True)
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        print(f"Port {PORT} is busy - the Photo Wall is probably already running.")
        webbrowser.open(f"http://localhost:{PORT}/")
        return
    url = f"http://localhost:{PORT}/"
    print("=" * 56)
    print("  Hashtag Photo Wall is running")
    print(f"  Control panel: {url}")
    print(f"  Display:       {url}display")
    print(f"  Photo folder:  {PHOTO_DIR}")
    print("  Keep this window open. Press Ctrl+C to stop.")
    print("=" * 56)
    if "--no-browser" not in sys.argv:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
