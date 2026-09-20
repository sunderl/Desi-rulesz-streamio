import asyncio
import base64
import binascii
import html as html_lib
import ipaddress
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any
from urllib.parse import quote_plus, unquote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("desiserials")
TARGET_SITE = os.getenv("TARGET_SITE", "https://www.desiserials.ru").rstrip("/")
TARGET_HOST = (urlparse(TARGET_SITE).hostname or "www.desiserials.ru").lower()
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
DEFAULT_POSTER = os.getenv("DEFAULT_POSTER", "https://images.unsplash.com/photo-1593784991095-a205069470b6?w=500&q=80")
USER_AGENT = os.getenv("USER_AGENT", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36")
MAX_CATALOG_ITEMS, MAX_VIDEOS = 100, 250
MAX_STREAMS_PER_REQUEST, STREAM_CANDIDATES_LIMIT, STREAM_CONCURRENCY = 30, 40, 8
CHANNELS = {
    "star_plus": ("Star Plus", "star-plus"),
    "colors_tv": ("Colors TV", "colors-tv"),
    "zee_tv": ("Zee TV", "zee-tv"),
    "sony_tv": ("Sony TV", "sony-tv"),
    "sab_tv": ("SAB TV", "sab-tv"),
    "and_tv": ("And TV", "and-tv"),
    "dangal_tv": ("Dangal TV", "dangal-tv"),
}
CHANNEL_LABELS = {"zee tv", "zeetv", "sony tv", "sonytv", "colors tv", "colors", "star plus", "starplus", "sab tv", "sabtv", "and tv", "&tv", "dangal", "dangal tv", "star bharat", "sony sab", "channel", "channels", "all serials", "latest episodes"}
NAV_LABELS = {"home", "menu", "next", "previous", "read more", "login", "contact", "about", "privacy", "privacy policy", "dmca", "watch now", "more", "new videos"}
MEDIA_HOSTS = {TARGET_HOST, "desiserials.ru", "showdetails.org", "showdetails.net", "vk.com", "vkvideo.ru", "vkuser.net", "streamwish.to", "streamwish.com", "filelions.to", "filelions.site", "doodstream.com", "dood.so", "streamtape.com", "streamtape.to"}
MEDIA_HOSTS.update(x.strip().lower().rstrip(".") for x in os.getenv("MEDIA_HOSTS_EXTRA", "").split(",") if x.strip())
SITE_HEADERS = {"User-Agent": USER_AGENT, "Referer": TARGET_SITE + "/", "Accept-Language": "en-US,en;q=0.8", "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
MANIFEST = {"id": "org.desiserials.streamio", "version": "7.2.0", "name": "DesiSerials TV", "description": "Desi serials grouped by channel with real show and episode titles.", "logo": DEFAULT_POSTER, "resources": ["catalog", "meta", "stream"], "types": ["tv"], "idPrefixes": ["dr_show_", "dr_ep_"], "catalogs": [{"type": "tv", "id": "desiserials_shows", "name": "All Serials", "extra": [{"name": "search", "isRequired": False}]}, {"type": "tv", "id": "desiserials_latest", "name": "Latest Episodes", "extra": [{"name": "search", "isRequired": False}]}] + [{"type": "tv", "id": key, "name": label, "extra": [{"name": "search", "isRequired": False}]} for key, (label, _) in CHANNELS.items()]}
CACHE: dict[str, tuple[float, Any]] = {}


def cache_get(key: str) -> Any | None:
    item = CACHE.get(key)
    if not item: return None
    if time.monotonic() >= item[0]: CACHE.pop(key, None); return None
    return item[1]


def cache_set(key: str, value: Any, ttl: int) -> None: CACHE[key] = (time.monotonic() + ttl, value)


def dedupe(items: list[dict], key: str = "url") -> list[dict]:
    seen, result = set(), []
    for item in items:
        value = item.get(key) or item.get("id")
        if value and value in seen: continue
        if value: seen.add(value)
        result.append(item)
    return result


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http = httpx.AsyncClient(headers=SITE_HEADERS, follow_redirects=True, timeout=httpx.Timeout(20, connect=8, read=16), limits=httpx.Limits(max_connections=80, max_keepalive_connections=30))
    yield
    await app.state.http.aclose()


app = FastAPI(title="DesiSerials TV Addon", version=MANIFEST["version"], lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=False, allow_methods=["GET", "HEAD", "OPTIONS"], allow_headers=["*"])


def encode_url(value: str) -> str: return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")
def decode_url(value: str) -> str:
    try: return base64.urlsafe_b64decode((value + "=" * (-len(value) % 4)).encode()).decode()
    except (binascii.Error, UnicodeError, ValueError) as exc: raise HTTPException(400, "Malformed URL token") from exc


def host_allowed(url: str) -> bool:
    try:
        parsed = urlparse(url); host = (parsed.hostname or "").lower().rstrip(".")
        if not url or len(url) > 4096 or parsed.scheme not in {"http", "https"} or not host: return False
        if not any(host == item or host.endswith("." + item) for item in MEDIA_HOSTS): return False
        try:
            address = ipaddress.ip_address(host)
            if any((address.is_private, address.is_loopback, address.is_link_local, address.is_reserved, address.is_multicast)): return False
        except ValueError: pass
        return True
    except ValueError: return False


def safe_url(url: str | None) -> str | None: return url if url and host_allowed(url) else None
def normalize(raw: str | None, base: str) -> str | None:
    if not raw: return None
    value = html_lib.unescape(str(raw)).strip().strip('"\'').replace("\\/", "/").replace("\\u0026", "&").replace("\\u003F", "?").replace("\\u003f", "?").replace("\\u003D", "=").replace("\\u003d", "=")
    value = unquote(value)
    if value.startswith("//"): value = "https:" + value
    return urljoin(base, value) or None

def text_of(tag) -> str: return re.sub(r"\s+", " ", " ".join(tag.stripped_strings)).strip() if tag else ""
def clean_title(value: str) -> str: return re.sub(r"\s+", " ", html_lib.unescape(value or "")).strip(" -|:")
def same_site(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    return host in {TARGET_HOST, "desiserials.ru"} or host.endswith("." + TARGET_HOST)
def is_playlist(url: str) -> bool: return urlparse(url).path.lower().endswith((".m3u8", ".m3u")) or "m3u8" in url.lower()
def proxy_link(request: Request, target: str) -> str: return f"{(PUBLIC_BASE_URL or str(request.base_url).rstrip('/')).rstrip('/')}/{'proxy/hls' if is_playlist(target) else 'proxy'}?url={encode_url(target)}"
def page_id(url: str, prefix: str = "dr_ep_") -> str: return prefix + encode_url(url)
def id_url(value: str, prefix: str) -> str:
    if not value.startswith(prefix): raise HTTPException(400, "Invalid addon id")
    return decode_url(value[len(prefix):])


def date_from_title(value: str) -> str | None:
    months = {name: idx for idx, names in enumerate(("january jan", "february feb", "march mar", "april apr", "may", "june jun", "july jul", "august aug", "september sep", "october oct", "november nov", "december dec"), 1) for name in names.split()}
    for index, pattern in enumerate((r"\b(\d{1,2})[\s,.-]+([A-Za-z]{3,9})[\s,.-]+(20\d{2})\b", r"\b(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})\b", r"\b(\d{1,2})[-/.](\d{1,2})[-/.](20\d{2})\b")):
        match = re.search(pattern, value, re.I)
        if not match: continue
        try:
            p = match.groups()
            if index == 0: day, month, year = int(p[0]), months.get(p[1].lower()), int(p[2])
            elif index == 1: year, month, day = map(int, p)
            else: day, month, year = int(p[0]), int(p[1]), int(p[2])
            return datetime(year, month, day).date().isoformat() if month else None
        except (TypeError, ValueError): pass
    return None


def is_episode_url(url: str, title: str = "") -> bool:
    value = (urlparse(url).path + " " + title).lower().replace("_", "-")
    return bool(re.search(r"episode|watch|video|serial|\bep\.?\s*\d+|\b\d{1,4}(?:st|nd|rd|th)?[- .]?(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|20\d{2})", value))
def is_channel_title(title: str) -> bool:
    value = re.sub(r"[^a-z0-9&]+", " ", clean_title(title).lower()).strip()
    return value in CHANNEL_LABELS or value.replace("&", "and") in {x.replace("&", "and") for x in CHANNEL_LABELS}
def is_navigation(url: str, title: str) -> bool:
    path = urlparse(url).path.lower().rstrip("/"); value = clean_title(title).lower()
    return path in {"", "/category", "/contact", "/about", "/privacy-policy", "/dmca", "/all-serials"} or value in NAV_LABELS or value.startswith(("read more", "watch ")) or is_channel_title(value)
def looks_like_show(title: str, url: str) -> bool:
    value = clean_title(title)
    return bool(value and not is_channel_title(value) and value.lower() not in NAV_LABELS and not is_episode_url(url, value) and re.search(r"[a-zA-Z]", value) and (len(value.split()) >= 2 or len(value) >= 7))


def poster_for(anchor, base: str) -> str:
    image = anchor.find("img") or (anchor.parent.find("img") if anchor.parent else None)
    if image:
        for key in ("data-src", "data-lazy-src", "data-original", "src", "srcset", "data-image"):
            raw = image.get(key)
            if key == "srcset" and raw: raw = raw.split(",")[0].strip().split(" ")[0]
            if (value := normalize(raw, base)): return value
    return DEFAULT_POSTER


def episode_title(anchor, container=None) -> str:
    """Use the card heading, not the channel menu or SEO suffix."""
    scope = container or anchor
    node = scope.select_one("h1, h2, h3, h4, .entry-title, .post-title, .title, .name") if hasattr(scope, "select_one") else None
    title = clean_title(text_of(node) if node else text_of(anchor) or anchor.get("title", ""))
    title = re.sub(r"\s+[-|]\s+(?:bigg?\s*boss|episode)\b.*$", "", title, flags=re.I)
    title = re.sub(r"\s+[-|]\s+(?:star plus|colors tv|zee tv|sony tv|sab tv|and tv|dangal tv)\b.*$", "", title, flags=re.I)
    return clean_title(title)


def catalog_links(source: str, base: str, episodes: bool) -> list[dict[str, str]]:
    soup = BeautifulSoup(source, "html.parser"); result = []
    containers = soup.select("article, .post, .post-item, .item, .card, .serial, .show, .movie, .grid-item, .entry")
    if not containers: containers = [soup]
    for container in containers:
        anchors = container.select("a[href]")
        for anchor in anchors[:4]:
            url = normalize(anchor.get("href"), base)
            title = episode_title(anchor, container) if episodes else clean_title(text_of(container.select_one("h1, h2, h3, h4, .entry-title, .post-title, .title, .name") or anchor))
            if not url or not same_site(url) or is_navigation(url, title): continue
            if episodes and is_episode_url(url, title):
                result.append({"url": url.split("#")[0], "title": title, "poster": poster_for(anchor, base)})
            elif not episodes and looks_like_show(title, url):
                result.append({"url": url.split("#")[0], "title": title, "poster": poster_for(anchor, base)})
    return dedupe(result)


async def get_text(client: httpx.AsyncClient, url: str, headers: dict | None = None) -> tuple[str, str]:
    last = None
    for attempt in range(3):
        try:
            response = await client.get(url, headers=headers, timeout=16); response.raise_for_status(); return response.text, str(response.url)
        except httpx.HTTPError as exc:
            last = exc
            if attempt < 2: await asyncio.sleep(.3 * (attempt + 1))
    raise last or RuntimeError("request failed")

MEDIA_RE = re.compile(r"(?:https?:)?//[^\"'<>\\\s]+?(?:\.m3u8|\.mp4|\.m4v)(?:\?[^\"'<>\\\s]*)?", re.I)
def extract_media(source: str, base: str) -> list[str]:
    cleaned = html_lib.unescape(source).replace("\\/", "/").replace("\\u0026", "&"); found = [normalize(x, base) for x in MEDIA_RE.findall(cleaned)]
    soup = BeautifulSoup(cleaned, "html.parser")
    for tag in soup.find_all(["iframe", "video", "source", "embed"]):
        for attr in ("src", "data-src", "data-url", "data-video", "data-file", "data-m3u8", "href"): found.append(normalize(tag.get(attr), base))
    return list(dict.fromkeys(x for x in found if safe_url(x)))
def gateway_links(source: str, base: str) -> list[str]:
    soup = BeautifulSoup(source, "html.parser"); result = []
    for tag in soup.select("a[href], [data-url], [data-href], [data-video], [data-embed]"):
        raw = tag.get("href") or tag.get("data-url") or tag.get("data-href") or tag.get("data-video") or tag.get("data-embed"); url = normalize(raw, base); label = (text_of(tag) + " " + str(tag.get("title", "")) + " " + str(tag.get("class", ""))).lower()
        if url and safe_url(url) and any(word in label for word in ("watch", "player", "embed", "part")): result.append(url)
    return list(dict.fromkeys(result))
async def streams_from_url(client: httpx.AsyncClient, url: str, referer: str, request: Request) -> list[dict]:
    try: source, final = await get_text(client, url, {**SITE_HEADERS, "Referer": referer})
    except Exception: return []
    media = extract_media(source, final)
    for gateway in gateway_links(source, final):
        try: nested, nested_final = await get_text(client, gateway, {**SITE_HEADERS, "Referer": url}); media.extend(extract_media(nested, nested_final))
        except Exception: pass
    result = [{"name": "DesiSerials", "title": "HLS Stream" if is_playlist(x) else "MP4 Stream", "url": proxy_link(request, x), "behaviorHints": {"bingeGroup": "desiserials", "notWebReady": False}} for x in dict.fromkeys(media)]
    if not result and safe_url(url) and url != referer: result.append({"name": "DesiSerials", "title": "Embedded Player", "url": url, "behaviorHints": {"bingeGroup": "desiserials", "notWebReady": True}})
    return dedupe(result)


@app.get("/proxy")
async def proxy_media(url: str, request: Request, range_header: str | None = Header(None, alias="Range")):
    target = decode_url(url)
    if not safe_url(target): raise HTTPException(403, "Media host is not allowed")
    headers = {"User-Agent": USER_AGENT, "Referer": TARGET_SITE + "/", "Origin": TARGET_SITE, "Accept": "*/*"}
    if range_header: headers["Range"] = range_header
    try:
        response = await request.app.state.http.send(request.app.state.http.build_request("GET", target, headers=headers), stream=True)
        if not safe_url(str(response.url)) or response.status_code >= 400:
            status = response.status_code if response.status_code >= 400 else 403; await response.aclose(); raise HTTPException(status, "Upstream media request failed")
        out = {k: response.headers[k] for k in ("content-type", "content-length", "content-range", "accept-ranges", "cache-control", "etag") if k in response.headers}; out.update({"Access-Control-Allow-Origin": "*", "Access-Control-Expose-Headers": "Content-Length,Content-Range,Accept-Ranges"})
        async def body():
            try:
                async for chunk in response.aiter_bytes(262144): yield chunk
            finally: await response.aclose()
        return StreamingResponse(body(), status_code=response.status_code, headers=out)
    except HTTPException: raise
    except httpx.HTTPError as exc: raise HTTPException(502, "Upstream media connection failed") from exc

@app.get("/proxy/hls")
async def proxy_hls(url: str, request: Request):
    playlist = decode_url(url)
    if not safe_url(playlist): raise HTTPException(403, "Playlist host is not allowed")
    try:
        response = await request.app.state.http.get(playlist, headers={**SITE_HEADERS, "Accept": "application/vnd.apple.mpegurl,*/*"}, timeout=16); response.raise_for_status(); final = str(response.url)
        if not safe_url(final): raise HTTPException(403, "Redirected playlist host is not allowed")
        output = []
        for raw in response.text.splitlines():
            line = raw.strip()
            if line.startswith("#"): output.append(re.sub(r"URI\s*=\s*([\"'])(.*?)\1", lambda m: f"URI={m.group(1)}{proxy_link(request, x)}{m.group(1)}" if (x := normalize(m.group(2), final)) and safe_url(x) else m.group(0), line, flags=re.I))
            elif line:
                target = normalize(line, final); output.append(proxy_link(request, target) if target and safe_url(target) else raw)
            else: output.append("")
        return Response("\n".join(output) + "\n", media_type="application/vnd.apple.mpegurl", headers={"Cache-Control": "no-cache", "Access-Control-Allow-Origin": "*"})
    except HTTPException: raise
    except httpx.HTTPError as exc: raise HTTPException(502, "HLS playlist request failed") from exc

@app.get("/")
def home(): return {"status": "DesiSerials addon active", "target": TARGET_SITE, "manifest": "/manifest.json"}
@app.get("/health")
def health(): return {"status": "ok", "version": MANIFEST["version"], "target": TARGET_SITE}
@app.get("/manifest.json")
def manifest(): return MANIFEST


async def collect_pages(client: httpx.AsyncClient, base: str, query: str = "", channel_slug: str | None = None) -> list[tuple[str, str]]:
    if channel_slug:
        urls = [f"{base}/category/{channel_slug}/", f"{base}/{channel_slug}/", f"{base}/?s={quote_plus(query)}" if query else f"{base}/category/{channel_slug}/"]
    else:
        first = f"{base}/?s={quote_plus(query)}" if query else base
        urls = [first] + [f"{base}/page/{n}/" for n in range(2, 11)] + [f"{base}/{x}/" for x in ("all-serials", "category", "tv-show", "latest-episodes", "series")]
    try: home, final = await get_text(client, urls[0])
    except Exception: home = ""
    if home:
        soup = BeautifulSoup(home, "html.parser")
        for a in soup.select("nav a[href], header a[href], .menu a[href], .nav a[href]"):
            href = normalize(a.get("href"), final); label = clean_title(text_of(a)); path = urlparse(href or "").path.lower()
            if href and same_site(href) and ("serial" in (label + path).lower() or "/category/" in path or "/show" in path): urls.append(href)
    async def fetch(url):
        try: return await get_text(client, url)
        except Exception: return None
    pages = await asyncio.gather(*(fetch(x) for x in list(dict.fromkeys(urls))[:25]))
    return [x for x in pages if x]


@app.get("/catalog/tv/{catalog_id}.json")
@app.get("/catalog/tv/{catalog_id}/search={query}.json")
async def catalog(catalog_id: str, request: Request, query: str | None = None):
    query = (query or "").strip(); key = f"catalog:{catalog_id}:{query.lower()}"
    if cached := cache_get(key): return cached
    channel = CHANNELS.get(catalog_id)
    if catalog_id not in {"desiserials_shows", "desiserials_latest"} and not channel: return {"metas": []}
    items = []
    pages = await collect_pages(request.app.state.http, TARGET_SITE, query, channel[1] if channel else None)
    for source, final in pages: items.extend(catalog_links(source, final, True if channel or catalog_id == "desiserials_latest" else False))
    items = dedupe(items)
    metas = []
    for item in items[:MAX_CATALOG_ITEMS]:
        metas.append({"id": page_id(item["url"], "dr_ep_"), "type": "tv", "name": item["title"], "poster": item["poster"], "description": item["title"], "releaseInfo": date_from_title(item["title"]) or ""})
    result = {"metas": metas}; cache_set(key, result, 300); return result


@app.get("/meta/tv/{id}.json")
async def meta(id: str, request: Request):
    if cached := cache_get("meta:" + id): return cached
    if id.startswith("dr_ep_"):
        page = id_url(id, "dr_ep_")
        try: source, _ = await get_text(request.app.state.http, page); title = clean_title(text_of(BeautifulSoup(source, "html.parser").select_one("h1, .entry-title, .post-title, title"))) or "Serial Episode"
        except Exception: title = "Serial Episode"
        result = {"meta": {"id": id, "type": "tv", "name": title, "poster": DEFAULT_POSTER, "videos": [{"id": id, "title": title, "season": 1, "episode": 1, **({"released": date_from_title(title)} if date_from_title(title) else {})}]}}
    else:
        result = {"meta": {"id": id, "type": "tv", "name": "Unknown"}}
    cache_set("meta:" + id, result, 300); return result

@app.get("/stream/tv/{id}.json")
async def stream(id: str, request: Request):
    if not id.startswith("dr_ep_"): return {"streams": []}
    if cached := cache_get("stream:" + id): return cached
    page, client, found = id_url(id, "dr_ep_"), request.app.state.http, []
    try:
        source, final = await get_text(client, page); soup = BeautifulSoup(source, "html.parser"); candidates = [final]
        for tag in soup.select("iframe[src], iframe[data-src], video[src], source[src], embed[src]"):
            for attr in ("src", "data-src"):
                candidate = normalize(tag.get(attr), final)
                if candidate and safe_url(candidate) and candidate not in candidates: candidates.append(candidate)
        candidates.extend(x for x in gateway_links(source, final) if x not in candidates); candidates.extend(x for x in extract_media(source, final) if x not in candidates)
        sem = asyncio.Semaphore(STREAM_CONCURRENCY)
        async def inspect(candidate):
            async with sem: return await streams_from_url(client, candidate, page, request)
        batches = await asyncio.gather(*(inspect(x) for x in candidates[:STREAM_CANDIDATES_LIMIT]), return_exceptions=True)
        for batch in batches:
            if isinstance(batch, list): found.extend(batch)
    except Exception as exc: logger.warning("Stream extraction failed for %s: %s", page, exc)
    result = {"streams": dedupe(found)[:MAX_STREAMS_PER_REQUEST]}
    if result["streams"]: cache_set("stream:" + id, result, 120)
    return result

@app.exception_handler(Exception)
async def unhandled(_: Request, exc: Exception):
    logger.exception("Unhandled addon error", exc_info=exc); return JSONResponse({"error": "Internal addon error"}, status_code=500)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
