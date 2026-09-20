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
from urllib.parse import quote_plus, unquote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("desiserials")
TARGET_SITE = os.getenv("TARGET_SITE", "https://www.desiserials.ru").rstrip("/")
TARGET_HOST = (urlparse(TARGET_SITE).hostname or "").lower()
ZEE_SOURCE = "https://watch.desitashan.ru"
ZEE_HOST = (urlparse(ZEE_SOURCE).hostname or "").lower()
ZEE_CATEGORY = f"{ZEE_SOURCE}/category/zee-tv/"
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
DEFAULT_POSTER = os.getenv("DEFAULT_POSTER", "https://images.unsplash.com/photo-1593784991095-a205069470b6?w=500&q=80")
USER_AGENT = os.getenv("USER_AGENT", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36")
MAX_CATALOG_ITEMS, MAX_VIDEOS = 100, 250
MAX_STREAMS_PER_REQUEST, STREAM_CANDIDATES_LIMIT, STREAM_CONCURRENCY = 30, 40, 8
CHANNELS = {
    "star_plus": ("Star Plus", "star-plus"), "colors_tv": ("Colors TV", "colors-tv"),
    "zee_tv": ("Zee TV", "zee-tv"), "sony_tv": ("Sony TV", "sony-tv"),
    "sab_tv": ("SAB TV", "sab-tv"), "and_tv": ("And TV", "and-tv"),
    "dangal_tv": ("Dangal TV", "dangal-tv"),
}
CHANNEL_LABELS = {"zee tv", "zeetv", "sony tv", "sonytv", "colors tv", "colors", "star plus", "starplus", "sab tv", "sabtv", "and tv", "&tv", "dangal", "dangal tv", "star bharat", "sony sab"}
NAV_LABELS = {"home", "menu", "next", "previous", "read more", "login", "contact", "about", "privacy", "privacy policy", "dmca", "watch now", "more", "new videos"}
MEDIA_HOSTS = {TARGET_HOST, "desiserials.ru", ZEE_HOST, "showdetails.org", "showdetails.net", "vk.com", "vkvideo.ru", "vkuser.net", "streamwish.to", "streamwish.com", "filelions.to", "filelions.site", "dood.la", "dood.watch", "mycloud.to", "mp4upload.com", "vimeo.com", "youtube.com", "youtu.be"}
MEDIA_HOSTS.update(x.strip().lower().rstrip(".") for x in os.getenv("MEDIA_HOSTS_EXTRA", "").split(",") if x.strip())
SITE_HEADERS = {"User-Agent": USER_AGENT, "Referer": TARGET_SITE + "/", "Accept-Language": "en-US,en;q=0.8", "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
MANIFEST = {"id": "org.desiserials.streamio", "version": "7.5.0", "name": "DesiSerials TV", "description": "Desi serials grouped by channel and series.", "logo": DEFAULT_POSTER, "resources": ["catalog", "meta", "stream"], "types": ["tv"], "idPrefixes": ["dr_show_", "dr_ep_"], "catalogs": [{"type": "tv", "id": "desiserials_shows", "name": "All Serials", "extra": [{"name": "search", "isRequired": False}]}, {"type": "tv", "id": "desiserials_latest", "name": "Latest Episodes"}, {"type": "tv", "id": "zee_tv", "name": "Zee TV", "extra": [{"name": "search", "isRequired": False}]}]}
CACHE: dict[str, tuple[float, object]] = {}


def cache_get(key):
    item = CACHE.get(key)
    if not item: return None
    if time.monotonic() >= item[0]: CACHE.pop(key, None); return None
    return item[1]


def cache_set(key, value, ttl): CACHE[key] = (time.monotonic() + ttl, value)


def dedupe(items, key="url"):
    seen, result = set(), []
    for item in items:
        value = item.get(key) or item.get("id")
        if value and value in seen: continue
        if value: seen.add(value)
        result.append(item)
    return result


@asynccontextmanager
async def lifespan(app):
    app.state.http = httpx.AsyncClient(headers=SITE_HEADERS, follow_redirects=True, timeout=httpx.Timeout(20, connect=8, read=16), limits=httpx.Limits(max_connections=80, max_keepalive_connections=30))
    yield
    await app.state.http.aclose()


app = FastAPI(title="DesiSerials TV Addon", version=MANIFEST["version"], lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=False, allow_methods=["GET", "HEAD", "OPTIONS"], allow_headers=["*"])


def encode_url(value): return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def decode_url(value):
    try: return base64.urlsafe_b64decode((value + "=" * (-len(value) % 4)).encode()).decode()
    except (binascii.Error, UnicodeError, ValueError) as exc: raise HTTPException(400, "Malformed URL token") from exc


def normalize(raw, base):
    if not raw: return None
    value = html_lib.unescape(str(raw)).strip().strip('"\'').replace("\\/", "/")
    value = re.sub(r"\\u00(26|3[fF]|3[dD])", lambda m: {"26": "&", "3f": "?", "3F": "?", "3d": "=", "3D": "="}[m.group(1)], value)
    value = unquote(value)
    if value.startswith("//"): value = "https:" + value
    return urljoin(base, value)


def host_allowed(url):
    try:
        parsed = urlparse(url); host = (parsed.hostname or "").lower().rstrip(".")
        if not url or len(url) > 4096 or parsed.scheme not in {"http", "https"} or not host: return False
        if not any(host == allowed or host.endswith("." + allowed) for allowed in MEDIA_HOSTS): return False
        try:
            ip = ipaddress.ip_address(host)
            if any((ip.is_private, ip.is_loopback, ip.is_link_local, ip.is_reserved, ip.is_multicast)): return False
        except ValueError: pass
        return True
    except ValueError: return False


def safe_url(url): return url if url and host_allowed(url) else None

def text_of(tag): return re.sub(r"\s+", " ", " ".join(tag.stripped_strings)).strip() if tag else ""

def clean_title(value): return re.sub(r"\s+", " ", html_lib.unescape(value or "")).strip(" -|:")

def same_site(url):
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    return host in {TARGET_HOST, "desiserials.ru", ZEE_HOST} or host.endswith("." + TARGET_HOST)

def is_playlist(url): return urlparse(url).path.lower().endswith((".m3u8", ".m3u")) or "m3u8" in url.lower()

def proxy_link(request, target): return f"{(PUBLIC_BASE_URL or str(request.base_url).rstrip('/')).rstrip('/')}/{('proxy/hls' if is_playlist(target) else 'proxy')}?url={encode_url(target)}"

def page_id(value, prefix="dr_ep_"): return prefix + encode_url(value)

def id_value(value, prefix):
    if not value.startswith(prefix): raise HTTPException(400, "Invalid addon id")
    return decode_url(value[len(prefix):])


def date_from_title(value):
    months = {name: idx for idx, names in enumerate(("january jan", "february feb", "march mar", "april apr", "may", "june jun", "july jul", "august aug", "september sep", "october oct", "november nov", "december dec"), 1) for name in names.split()}
    patterns = (r"\b(\d{1,2})[\s,.-]+([A-Za-z]{3,9})[\s,.-]+(20\d{2})\b", r"\b(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})\b", r"\b(\d{1,2})[-/.](\d{1,2})[-/.](20\d{2})\b")
    for n, pattern in enumerate(patterns):
        match = re.search(pattern, value, re.I)
        if not match: continue
        try:
            p = match.groups(); day, month, year = ((int(p[0]), months.get(p[1].lower()), int(p[2])) if n == 0 else ((int(p[2]), int(p[1]), int(p[0])) if n == 1 else (int(p[0]), int(p[1]), int(p[2]))))
            return datetime(year, month, day).date().isoformat() if month else None
        except (TypeError, ValueError): pass
    return None


def is_episode_url(url, title=""):
    value = (urlparse(url).path + " " + title).lower().replace("_", "-")
    return bool(re.search(r"episode|watch|video|serial|full-episode|zee-tv|\bep\.?\s*\d+|\b\d{1,4}(?:st|nd|rd|th)?[- .]?(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|20\d{2})", value))


def show_title(raw):
    title = clean_title(raw)
    title = re.sub(r"(?i)\s*\|.*$", "", title)
    title = re.sub(r"(?i)\s*[-–:]\s*(?:zee\s*tv|star\s*plus|colors\s*tv|sony\s*tv|sab\s*tv|and\s*tv|dangal\s*tv).*$", "", title)
    title = re.sub(r"(?i)^(?:zee\s*tv|star\s*plus|colors\s*tv|sony\s*tv|sab\s*tv|and\s*tv|dangal\s*tv)\s*[-:|–]?\s*", "", title)
    title = re.sub(r"(?i)\b(?:watch\s+online|watch\s+now|full\s+episode|episode\s*\d+)\b.*$", "", title)
    title = re.sub(r"(?i)\b\d{1,2}(?:st|nd|rd|th)?\s+(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+20\d{2}.*$", "", title)
    title = re.sub(r"(?i)\s+20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}.*$", "", title)
    return clean_title(title)


def is_channel_or_nav(title):
    value = re.sub(r"[^a-z0-9&]+", " ", clean_title(title).lower()).strip()
    return value in CHANNEL_LABELS or value in NAV_LABELS or value.startswith(("read more", "watch "))


def poster_for(anchor, base):
    image = anchor.find("img") or (anchor.parent.find("img") if anchor.parent else None)
    if image:
        for key in ("data-src", "data-lazy-src", "data-original", "src", "srcset", "data-image"):
            raw = image.get(key)
            if key == "srcset" and raw: raw = raw.split(",")[0].strip().split(" ")[0]
            if value := normalize(raw, base): return value
    return DEFAULT_POSTER


def zee_episode_items(source, base):
    """Parse Zee posts without relying on one particular WordPress card layout."""
    soup = BeautifulSoup(source, "html.parser")
    result, seen = [], set()
    cards = soup.select("main article, .site-main article, article.post, article, .post, .post-item, .entry, .card, .blog-post, .item")
    anchors = [a for card in cards for a in card.select("a[href]")] if cards else soup.select("a[href]")
    for anchor in anchors:
        url = normalize(anchor.get("href"), base)
        if not url or url.split("#")[0] in seen or not same_site(url): continue
        card = anchor.find_parent(["article", ".post", ".post-item", ".entry", ".card", ".blog-post", ".item"])
        heading = card.select_one("h1,h2,h3,h4,h5,.entry-title,.post-title,.title,.name") if card else None
        raw = (text_of(heading) if heading else "") or anchor.get("title") or anchor.get("aria-label") or text_of(anchor)
        if not raw or is_channel_or_nav(raw):
            raw = anchor.get("title") or anchor.get("aria-label") or text_of(heading)
        title = clean_title(raw)
        if not title or is_channel_or_nav(title) or not is_episode_url(url, title): continue
        name = show_title(title)
        if not name or is_channel_or_nav(name) or len(name) < 3: continue
        clean_url = url.split("#")[0]; seen.add(clean_url)
        result.append({"url": clean_url, "title": title, "show": name, "poster": poster_for(anchor, base)})
    return result


async def get_text(client, url, headers=None):
    last = None
    for attempt in range(3):
        try:
            response = await client.get(url, headers=headers, timeout=16); response.raise_for_status(); return response.text, str(response.url)
        except httpx.HTTPError as exc:
            last = exc
            if attempt < 2: await asyncio.sleep(.3 * (attempt + 1))
    raise last or RuntimeError("request failed")


async def collect_zee(client):
    urls = [ZEE_CATEGORY] + [f"{ZEE_SOURCE}/category/zee-tv/page/{n}/" for n in range(2, 11)]
    async def fetch(url):
        try: return await get_text(client, url, {**SITE_HEADERS, "Referer": ZEE_SOURCE + "/"})
        except Exception as exc:
            logger.warning("Zee catalog page failed: %s (%s)", url, exc)
            return None
    pages = [x for x in await asyncio.gather(*(fetch(url) for url in urls)) if x]
    logger.info("Zee catalog fetched %d pages and %d posts", len(pages), sum(len(zee_episode_items(s, f)) for s, f in pages))
    return pages


async def collect_pages(client, base, query="", slug=None):
    first = f"{base}/?s={quote_plus(query)}" if query else base
    urls = [first] + ([f"{base}/category/{slug}/", f"{base}/category/{slug}/page/2/", f"{base}/category/{slug}/page/3/"] if slug else [f"{base}/page/{n}/" for n in range(2, 6)])
    async def fetch(url):
        try: return await get_text(client, url)
        except Exception: return None
    return [x for x in await asyncio.gather(*(fetch(url) for url in urls)) if x]


def grouped(items):
    groups = {}
    for item in items:
        key = re.sub(r"[^a-z0-9]+", " ", item["show"].lower()).strip()
        if key: groups.setdefault(key, {"show": item["show"], "poster": item["poster"], "episodes": []})["episodes"].append(item)
    return list(groups.values())


MEDIA_RE = re.compile(r"(?:https?:)?//[^\"'<>\\\s]+?(?:\.m3u8|\.m3u|\.mp4|\.m4v)(?:\?[^\"'<>\\\s]*)?", re.I)


def extract_media(source, base):
    cleaned = html_lib.unescape(source).replace("\\/", "/").replace("\\u0026", "&")
    found = [normalize(x, base) for x in MEDIA_RE.findall(cleaned)]
    soup = BeautifulSoup(cleaned, "html.parser")
    for tag in soup.find_all(["iframe", "video", "source", "embed"]):
        for attr in ("src", "data-src", "data-url", "data-video", "data-file", "data-m3u8", "data-stream", "data-hls", "href"): found.append(normalize(tag.get(attr), base))
    for pattern in (r"[\"'](?:file|source|src|hls|hlsUrl|stream|videoUrl|playlist)[\"']\s*:\s*[\"']([^\"']+)", r"(?:file|source|src|hls|hlsUrl|stream|videoUrl|playlist)\s*=\s*[\"']([^\"']+)"):
        for match in re.findall(pattern, cleaned, re.I): found.append(normalize(match, base))
    return list(dict.fromkeys(x for x in found if safe_url(x)))


def gateway_links(source, base):
    soup = BeautifulSoup(source, "html.parser"); result = []
    for tag in soup.select("a[href], [data-url], [data-href], [data-video], [data-embed]"):
        raw = tag.get("href") or tag.get("data-url") or tag.get("data-href") or tag.get("data-video") or tag.get("data-embed")
        url = normalize(raw, base); label = (text_of(tag) + " " + str(tag.get("title") or "")).lower()
        if url and safe_url(url) and any(word in label for word in ("watch", "player", "embed", "part")): result.append(url)
    return list(dict.fromkeys(result))


async def streams_from_url(client, url, referer, request):
    media = []
    if is_playlist(url) or re.search(r"\.(?:mp4|m4v)(?:$|\?)", url, re.I):
        media.append(url)
    else:
        try: source, final = await get_text(client, url, {**SITE_HEADERS, "Referer": referer})
        except Exception: return []
        media.extend(extract_media(source, final))
        for gateway in gateway_links(source, final):
            try:
                nested, nested_final = await get_text(client, gateway, {**SITE_HEADERS, "Referer": url}); media.extend(extract_media(nested, nested_final))
            except Exception: pass
    media = list(dict.fromkeys(media)); media.sort(key=lambda value: 0 if is_playlist(value) else 1)
    result = [{"name": "DesiSerials Zee TV", "title": "HLS Stream" if is_playlist(x) else "MP4 Stream", "url": proxy_link(request, x), "behaviorHints": {"bingeGroup": "desiserials-zee", "notWebReady": False}} for x in media]
    if not result and safe_url(url) and url != referer: result.append({"name": "DesiSerials Zee TV", "title": "Embedded Player", "url": url, "behaviorHints": {"bingeGroup": "desiserials-zee", "notWebReady": True}})
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
        output_headers = {k: response.headers[k] for k in ("content-type", "content-length", "content-range", "accept-ranges", "cache-control", "etag") if k in response.headers}; output_headers["Access-Control-Allow-Origin"] = "*"
        async def body():
            try:
                async for chunk in response.aiter_bytes(262144): yield chunk
            finally: await response.aclose()
        return StreamingResponse(body(), status_code=response.status_code, headers=output_headers)
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
            if line.startswith("#"):
                output.append(re.sub(r"URI\s*=\s*([\"'])(.*?)\1", lambda m: f"URI={m.group(1)}{proxy_link(request, x)}{m.group(1)}" if (x := normalize(m.group(2), final)) and safe_url(x) else m.group(0), raw))
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


@app.get("/catalog/tv/{catalog_id}.json")
@app.get("/catalog/tv/{catalog_id}/search={query}.json")
async def catalog(catalog_id: str, request: Request, query: str | None = None):
    query = (query or "").strip(); key = f"catalog:{catalog_id}:{query.lower()}"
    if cached := cache_get(key): return cached
    channel = CHANNELS.get(catalog_id)
    if catalog_id not in {"desiserials_shows", "desiserials_latest", "zee_tv"} and not channel: return {"metas": []}
    if catalog_id == "zee_tv":
        pages = await collect_zee(request.app.state.http)
        items = [item for source, final in pages for item in zee_episode_items(source, final)]
        if query:
            query_key = re.sub(r"[^a-z0-9]+", " ", query.lower()).strip()
            items = [item for item in items if query_key in re.sub(r"[^a-z0-9]+", " ", item["show"].lower()).strip()]
    else:
        pages = await collect_pages(request.app.state.http, TARGET_SITE, query, channel[1] if channel else None)
        items = [item for source, final in pages for item in zee_episode_items(source, final)]
    groups = grouped(dedupe(items))
    result = {"metas": [{"id": page_id(f"{catalog_id}|{group['show']}", "dr_show_"), "type": "tv", "name": group["show"], "poster": group["poster"], "description": f"{group['show']} episodes"} for group in groups[:MAX_CATALOG_ITEMS]]}
    cache_set(key, result, 300); return result


@app.get("/meta/tv/{id}.json")
async def meta(id: str, request: Request):
    if cached := cache_get("meta:" + id): return cached
    if not id.startswith("dr_show_"): return {"meta": {"id": id, "type": "tv", "name": "Unknown"}}
    token = id_value(id, "dr_show_"); catalog_id, wanted = token.split("|", 1) if "|" in token else ("desiserials_shows", token)
    pages = await collect_zee(request.app.state.http) if catalog_id == "zee_tv" else await collect_pages(request.app.state.http, TARGET_SITE, "", CHANNELS.get(catalog_id, ("", ""))[1] if catalog_id in CHANNELS else None)
    items = [item for source, final in pages for item in zee_episode_items(source, final)]
    wanted_key = re.sub(r"[^a-z0-9]+", " ", wanted.lower()).strip(); matched = [x for x in dedupe(items) if re.sub(r"[^a-z0-9]+", " ", x["show"].lower()).strip() == wanted_key]
    videos = []
    for number, item in enumerate(matched[:MAX_VIDEOS], 1):
        video = {"id": page_id(item["url"]), "title": item["title"], "season": 1, "episode": number}
        if released := date_from_title(item["title"]): video["released"] = released
        videos.append(video)
    result = {"meta": {"id": id, "type": "tv", "name": wanted, "poster": matched[0]["poster"] if matched else DEFAULT_POSTER, "videos": videos}}
    cache_set("meta:" + id, result, 300); return result


@app.get("/stream/tv/{id}.json")
async def stream(id: str, request: Request):
    if not id.startswith("dr_ep_"): return {"streams": []}
    if cached := cache_get("stream:" + id): return cached
    page, client, found = id_value(id, "dr_ep_"), request.app.state.http, []
    try:
        source, final = await get_text(client, page); soup = BeautifulSoup(source, "html.parser"); candidates = [final]
        for tag in soup.select("iframe[src], iframe[data-src], video[src], source[src], embed[src]"):
            for attr in ("src", "data-src"):
                candidate = normalize(tag.get(attr), final)
                if candidate and safe_url(candidate) and candidate not in candidates: candidates.append(candidate)
        for candidate in extract_media(source, final) + gateway_links(source, final):
            if candidate not in candidates: candidates.append(candidate)
        candidates.sort(key=lambda value: (0 if is_playlist(value) else 1, 0 if re.search(r"watch\.desitashan\.ru", value, re.I) else 1))
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
