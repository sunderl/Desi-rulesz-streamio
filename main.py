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

# DesiSerials is a WordPress-like site whose theme has changed several times.
# The scraper deliberately relies on semantic URL/text signals as well as the
# common article/card markup, instead of one brittle CSS class.
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("desiserials")

TARGET_SITE = os.getenv("TARGET_SITE", "https://www.desiserials.ru").rstrip("/")
TARGET_HOST = (urlparse(TARGET_SITE).hostname or "www.desiserials.ru").lower()
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
DEFAULT_POSTER = os.getenv(
    "DEFAULT_POSTER",
    "https://images.unsplash.com/photo-1593784991095-a205069470b6?w=500&q=80",
)
USER_AGENT = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
)

MAX_URL_LENGTH = 4096
MAX_CATALOG_ITEMS = 80
MAX_VIDEOS = 150
MAX_STREAMS_PER_REQUEST = 30
MAX_HTTP_RETRIES = 3
STREAM_CANDIDATES_LIMIT = 20
STREAM_CONCURRENCY = 6

# Keep this explicit to prevent the proxy becoming an open SSRF proxy. Add a
# provider/CDN domain with MEDIA_HOSTS=host1,host2 when the site changes hosts.
DEFAULT_MEDIA_HOSTS = {
    TARGET_HOST,
    "desiserials.ru",
    "vk.com", "vkvideo.ru", "vkuser.net",
    "streamwish.to", "streamwish.com", "filelions.to", "filelions.site",
    "doodstream.com", "dood.so", "streamtape.com", "streamtape.site",
    "vidoza.net", "vidsrc.me", "dailymotion.com", "dmcdn.net",
    "ok.ru", "odnoklassniki.ru", "mega.nz", "mixdrop.co", "mixdrop.to",
    "akamaized.net", "cloudfront.net", "googlevideo.com", "gvideo.com",
}
ALLOWED_MEDIA_HOSTS = DEFAULT_MEDIA_HOSTS | {
    x.strip().lower().rstrip(".")
    for x in os.getenv("MEDIA_HOSTS", "").split(",")
    if x.strip()
}

SITE_HEADERS = {
    "User-Agent": USER_AGENT,
    "Referer": TARGET_SITE + "/",
    "Accept-Language": "en-US,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

MANIFEST = {
    "id": "org.desiserials.streamio",
    "version": "6.0.0",
    "name": "DesiSerials TV",
    "description": "DesiSerials catalog with resilient episode and HLS extraction.",
    "logo": DEFAULT_POSTER,
    "resources": ["catalog", "meta", "stream"],
    "types": ["tv"],
    "idPrefixes": ["dr_"],
    "catalogs": [
        {"type": "tv", "id": "desiserials_shows", "name": "DesiSerials Shows",
         "extra": [{"name": "search", "isRequired": False}]},
        {"type": "tv", "id": "desiserials_latest", "name": "Latest Episodes",
         "extra": [{"name": "search", "isRequired": False}]},
    ],
}
CACHE: dict[str, tuple[float, Any]] = {}


def cache_get(key: str) -> Any | None:
    item = CACHE.get(key)
    if not item:
        return None
    if time.monotonic() >= item[0]:
        CACHE.pop(key, None)
        return None
    return item[1]


def cache_set(key: str, value: Any, ttl: int) -> None:
    CACHE[key] = (time.monotonic() + ttl, value)


def dedupe(items: list[dict], key: str = "url") -> list[dict]:
    seen, result = set(), []
    for item in items:
        value = item.get(key) or item.get("id")
        if value and value in seen:
            continue
        if value:
            seen.add(value)
        result.append(item)
    return result


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http = httpx.AsyncClient(
        headers=SITE_HEADERS, follow_redirects=True,
        timeout=httpx.Timeout(20.0, connect=8.0, read=16.0),
        limits=httpx.Limits(max_connections=80, max_keepalive_connections=30),
    )
    yield
    await app.state.http.aclose()


app = FastAPI(title="DesiSerials TV Addon", version=MANIFEST["version"], lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=False,
                   allow_methods=["GET", "HEAD", "OPTIONS"], allow_headers=["*"])


# -----------------------------------------------------------------------------
# Safe URL and Stremio ID helpers
# -----------------------------------------------------------------------------
def encode_url(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def decode_url(value: str) -> str:
    try:
        return base64.urlsafe_b64decode((value + "=" * (-len(value) % 4)).encode()).decode()
    except (binascii.Error, UnicodeError, ValueError) as exc:
        raise HTTPException(400, "Malformed URL token") from exc


def host_allowed(url: str) -> bool:
    try:
        if not url or len(url) > MAX_URL_LENGTH:
            return False
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme not in {"http", "https"} or not host:
            return False
        if not any(host == allowed or host.endswith("." + allowed) for allowed in ALLOWED_MEDIA_HOSTS):
            return False
        try:
            address = ipaddress.ip_address(host)
            if any((address.is_private, address.is_loopback, address.is_link_local,
                    address.is_reserved, address.is_multicast)):
                return False
        except ValueError:
            pass
        return True
    except ValueError:
        return False


def safe_url(url: str | None) -> str | None:
    return url if url and host_allowed(url) else None


def normalize(raw: str | None, base: str) -> str | None:
    if not raw:
        return None
    value = html_lib.unescape(str(raw)).strip().strip("\"'")
    value = (value.replace("\\/", "/").replace("\\u0026", "&")
             .replace("\\u003F", "?").replace("\\u003f", "?")
             .replace("\\u003D", "=").replace("\\u003d", "="))
    value = unquote(value)
    if value.startswith("//"):
        value = "https:" + value
    return urljoin(base, value) or None


def is_playlist(url: str) -> bool:
    lower = url.lower()
    return urlparse(url).path.lower().endswith((".m3u8", ".m3u")) or "m3u8" in lower


def public_base(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    forwarded = request.headers.get("x-forwarded-host", "").split(",")[0].strip()
    if forwarded:
        return f"{proto or request.url.scheme}://{forwarded}/"
    return (PUBLIC_BASE_URL or str(request.base_url).rstrip("/")).rstrip("/") + "/"


def proxy_link(request: Request, target: str) -> str:
    route = "proxy/hls" if is_playlist(target) else "proxy"
    return f"{public_base(request)}{route}?url={encode_url(target)}"


def page_id(url: str, prefix: str = "dr_ep_") -> str:
    return prefix + encode_url(url)


def id_url(value: str, prefix: str) -> str:
    if not value.startswith(prefix):
        raise HTTPException(400, "Invalid addon id")
    return decode_url(value[len(prefix):])


# -----------------------------------------------------------------------------
# DesiSerials parsing
# -----------------------------------------------------------------------------
def text_of(tag) -> str:
    return re.sub(r"\s+", " ", " ".join(tag.stripped_strings)).strip() if tag else ""


def clean_title(value: str) -> str:
    return re.sub(r"\s+", " ", html_lib.unescape(value)).strip(" -|:")


def title_from_page(soup: BeautifulSoup, fallback: str = "Serial Episode") -> str:
    for selector in ("h1", ".entry-title", ".post-title", "article h1", "meta[property='og:title']", "title"):
        tag = soup.select_one(selector)
        value = tag.get("content", "") if tag and tag.name == "meta" else text_of(tag)
        if value:
            return clean_title(value)
    return fallback


def date_from_title(value: str) -> str | None:
    patterns = [
        r"\b(\d{1,2})[\s,.-]+([A-Za-z]{3,9})[\s,.-]+(20\d{2})\b",
        r"\b(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})\b",
        r"\b(\d{1,2})[-/.](\d{1,2})[-/.](20\d{2})\b",
    ]
    months = {name: number for number, names in enumerate(
        ("january jan", "february feb", "march mar", "april apr", "may",
         "june jun", "july jul", "august aug", "september sep", "october oct",
         "november nov", "december dec"), 1) for name in names.split()}
    for index, pattern in enumerate(patterns):
        match = re.search(pattern, value, re.I)
        if not match:
            continue
        parts = match.groups()
        try:
            if index == 0:
                day, month, year = int(parts[0]), months.get(parts[1].lower()), int(parts[2])
            elif index == 1:
                year, month, day = map(int, parts)
            else:
                day, month, year = map(int, parts)
            if month:
                return datetime(year, month, day).date().isoformat()
        except (TypeError, ValueError):
            pass
    return None


def poster_for(anchor, base: str) -> str:
    image = anchor.find("img") or (anchor.parent.find("img") if anchor.parent else None)
    if image:
        for key in ("data-src", "data-lazy-src", "data-original", "src", "srcset"):
            raw = image.get(key)
            if key == "srcset" and raw:
                raw = raw.split(",")[0].strip().split(" ")[0]
            value = normalize(raw, base)
            if value:
                return value
    return DEFAULT_POSTER


def same_site(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == TARGET_HOST or host.endswith("." + TARGET_HOST) or host == "desiserials.ru"


def is_episode_url(url: str, title: str = "") -> bool:
    path = urlparse(url).path.lower()
    text = (path + " " + title).replace("_", "-")
    return bool(re.search(r"episode|watch|serial|video|\bep\.?\s*\d+|\b\d{1,4}\b", text))


def is_navigation(url: str, title: str) -> bool:
    path = urlparse(url).path.lower().rstrip("/")
    if path in {"", "/category", "/contact", "/about", "/privacy-policy", "/dmca"}:
        return True
    return clean_title(title).lower() in {"home", "menu", "next", "previous", "read more", "login"}


def episode_links(source: str, base: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(source, "html.parser")
    result = []
    for anchor in soup.select("a[href]"):
        url = normalize(anchor.get("href"), base)
        title = clean_title(text_of(anchor) or anchor.get("title", ""))
        if not url or not same_site(url) or not title or len(title) < 3 or is_navigation(url, title):
            continue
        if is_episode_url(url, title):
            result.append({"url": url.split("#", 1)[0], "title": title, "poster": poster_for(anchor, base)})
    return dedupe(result)


def show_links(source: str, base: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(source, "html.parser")
    result = []
    for anchor in soup.select("article a[href], .item a[href], .post a[href], .card a[href], a[href]"):
        url = normalize(anchor.get("href"), base)
        title = clean_title(text_of(anchor) or anchor.get("title", ""))
        if not url or not same_site(url) or not title or len(title) < 3 or is_navigation(url, title):
            continue
        path = urlparse(url).path.lower()
        if is_episode_url(url, title) or path.count("/") > 3:
            continue
        result.append({"url": url.split("#", 1)[0], "title": title, "poster": poster_for(anchor, base)})
    return dedupe(result)


MEDIA_RE = re.compile(r"(?:https?:)?//[^\"'<>\\\s]+?(?:\.m3u8|\.mp4|\.m4v)(?:\?[^\"'<>\\\s]*)?", re.I)


def extract_media(source: str, base: str) -> list[str]:
    cleaned = html_lib.unescape(source).replace("\\/", "/").replace("\\u0026", "&")
    candidates: list[str] = []
    for raw in MEDIA_RE.findall(cleaned):
        value = normalize(raw, base)
        if safe_url(value):
            candidates.append(value)
    soup = BeautifulSoup(cleaned, "html.parser")
    for tag in soup.find_all(["iframe", "video", "source", "embed"]):
        for attr in ("src", "data-src", "data-url", "data-video", "data-file", "data-m3u8", "href"):
            value = normalize(tag.get(attr), base)
            if safe_url(value):
                candidates.append(value)
    for key in ("file", "src", "source", "hls", "playlist", "url", "video_url"):
        for match in re.finditer(rf"[\"']{key}[\"']\s*:\s*[\"']([^\"']+)", cleaned, re.I):
            value = normalize(match.group(1), base)
            if safe_url(value) and (is_playlist(value) or re.search(r"\.(?:mp4|m4v)(?:$|\?)", value, re.I)):
                candidates.append(value)
    return dedupe([{"url": x} for x in candidates]) and [x["url"] for x in dedupe([{"url": x} for x in candidates])]


async def get_text(client: httpx.AsyncClient, url: str, headers: dict | None = None) -> tuple[str, str]:
    last: Exception | None = None
    for attempt in range(MAX_HTTP_RETRIES):
        try:
            response = await client.get(url, headers=headers, timeout=16.0)
            response.raise_for_status()
            return response.text, str(response.url)
        except httpx.HTTPError as exc:
            last = exc
            if attempt < MAX_HTTP_RETRIES - 1:
                await asyncio.sleep(0.3 * (attempt + 1))
    raise last or RuntimeError("request failed")


async def streams_from_url(client: httpx.AsyncClient, url: str, referer: str, request: Request) -> list[dict]:
    try:
        source, final = await get_text(client, url, {**SITE_HEADERS, "Referer": referer})
    except Exception as exc:
        logger.info("Could not inspect %s: %s", url, exc)
        return []
    streams = []
    for media in extract_media(source, final):
        streams.append({"name": "DesiSerials", "title": "HLS Stream" if is_playlist(media) else "MP4 Stream",
                        "url": proxy_link(request, media),
                        "behaviorHints": {"bingeGroup": "desiserials", "notWebReady": False}})
    return dedupe(streams)


# -----------------------------------------------------------------------------
# Media proxy (HLS playlists rewrite both segment lines and URI attributes)
# -----------------------------------------------------------------------------
@app.get("/proxy")
async def proxy_media(url: str, request: Request, range_header: str | None = Header(None, alias="Range")):
    target = decode_url(url)
    if not safe_url(target):
        raise HTTPException(403, "Media host is not allowed")
    headers = {"User-Agent": USER_AGENT, "Referer": TARGET_SITE + "/", "Origin": TARGET_SITE, "Accept": "*/*"}
    if range_header:
        headers["Range"] = range_header
    client = request.app.state.http
    try:
        response = await client.send(client.build_request("GET", target, headers=headers), stream=True)
        if not safe_url(str(response.url)) or response.status_code >= 400:
            status = response.status_code if response.status_code >= 400 else 403
            await response.aclose()
            raise HTTPException(status, "Upstream media request failed")
        passthrough = {key: response.headers[key] for key in
                       ("content-type", "content-length", "content-range", "accept-ranges", "cache-control", "etag")
                       if key in response.headers}
        passthrough.update({"Access-Control-Allow-Origin": "*", "Access-Control-Expose-Headers": "*"})
        async def body():
            try:
                async for chunk in response.aiter_bytes(262144):
                    yield chunk
            finally:
                await response.aclose()
        return StreamingResponse(body(), status_code=response.status_code, headers=passthrough)
    except HTTPException:
        raise
    except httpx.HTTPError as exc:
        raise HTTPException(502, "Upstream media connection failed") from exc


@app.get("/proxy/hls")
async def proxy_hls(url: str, request: Request):
    playlist = decode_url(url)
    if not safe_url(playlist):
        raise HTTPException(403, "Playlist host is not allowed")
    try:
        response = await request.app.state.http.get(playlist, headers={**SITE_HEADERS, "Accept": "application/vnd.apple.mpegurl,*/*"}, timeout=16.0)
        response.raise_for_status()
        final = str(response.url)
        if not safe_url(final):
            raise HTTPException(403, "Redirected playlist host is not allowed")
        def rewrite(line: str) -> str:
            def replace(match):
                target = normalize(match.group(2), final)
                return (f"URI={match.group(1)}{proxy_link(request, target)}{match.group(1)}"
                        if target and safe_url(target) else match.group(0))
            return re.sub(r"URI\s*=\s*([\"'])(.*?)\1", replace, line, flags=re.I)
        output = []
        for raw in response.text.splitlines():
            line = raw.strip()
            if line.startswith("#"):
                output.append(rewrite(line))
            elif line:
                target = normalize(line, final)
                output.append(proxy_link(request, target) if target and safe_url(target) else raw)
            else:
                output.append("")
        return Response("\n".join(output) + "\n", media_type="application/vnd.apple.mpegurl",
                        headers={"Cache-Control": "no-cache", "Access-Control-Allow-Origin": "*"})
    except HTTPException:
        raise
    except httpx.HTTPError as exc:
        raise HTTPException(502, "HLS playlist request failed") from exc


# -----------------------------------------------------------------------------
# Stremio API
# -----------------------------------------------------------------------------
@app.get("/")
def home():
    return {"status": "DesiSerials addon active", "target": TARGET_SITE, "manifest": "/manifest.json"}


@app.get("/health")
def health():
    return {"status": "ok", "version": MANIFEST["version"], "target": TARGET_SITE}


@app.get("/manifest.json")
def manifest():
    return MANIFEST


@app.get("/catalog/tv/{catalog_id}.json")
@app.get("/catalog/tv/{catalog_id}/search={query}.json")
async def catalog(catalog_id: str, request: Request, query: str | None = None):
    query = (query or "").strip()
    cache_key = f"catalog:{catalog_id}:{query.lower()}"
    if cached := cache_get(cache_key):
        return cached
    client = request.app.state.http
    items: list[dict[str, str]] = []
    try:
        if catalog_id == "desiserials_latest":
            source, final = await get_text(client, TARGET_SITE)
            items = episode_links(source, final)
        elif catalog_id == "desiserials_shows":
            target = f"{TARGET_SITE}/?s={quote_plus(query)}" if query else TARGET_SITE
            source, final = await get_text(client, target)
            items = show_links(source, final)
            # Some themes expose only posts on the home page. Present them as
            # searchable TV entries rather than returning an empty catalog.
            if not items:
                items = episode_links(source, final)
        else:
            return {"metas": []}
    except Exception as exc:
        logger.warning("Catalog %s failed: %s", catalog_id, exc)
    metas = []
    for item in items[:MAX_CATALOG_ITEMS]:
        is_episode = catalog_id == "desiserials_latest" or is_episode_url(item["url"], item["title"])
        metas.append({"id": page_id(item["url"]) if is_episode else page_id(item["url"], "dr_show_"),
                      "type": "tv", "name": item["title"], "poster": item["poster"],
                      "description": item["title"]})
    payload = {"metas": metas}
    cache_set(cache_key, payload, 300)
    return payload


@app.get("/meta/tv/{id}.json")
async def meta(id: str, request: Request):
    if cached := cache_get("meta:" + id):
        return cached
    client = request.app.state.http
    if id.startswith("dr_show_"):
        page = id_url(id, "dr_show_")
        try:
            source, final = await get_text(client, page)
            soup = BeautifulSoup(source, "html.parser")
            name = title_from_page(soup, "Indian Serial")
            links = episode_links(source, final)
            poster = (soup.select_one("meta[property='og:image']") or {}).get("content", DEFAULT_POSTER)
        except Exception:
            name, links, poster = "Indian Serial", [], DEFAULT_POSTER
        videos = []
        for number, item in enumerate(links[:MAX_VIDEOS], 1):
            video = {"id": page_id(item["url"]), "title": item["title"], "season": 1, "episode": number}
            if released := date_from_title(item["title"]):
                video["released"] = released
            videos.append(video)
        result = {"meta": {"id": id, "type": "tv", "name": name, "poster": poster, "videos": videos}}
    elif id.startswith("dr_ep_"):
        page = id_url(id, "dr_ep_")
        try:
            source, _ = await get_text(client, page)
            title = title_from_page(BeautifulSoup(source, "html.parser"))
        except Exception:
            title = "Serial Episode"
        video = {"id": id, "title": title, "season": 1, "episode": 1}
        if released := date_from_title(title):
            video["released"] = released
        result = {"meta": {"id": id, "type": "tv", "name": title, "poster": DEFAULT_POSTER, "videos": [video]}}
    else:
        result = {"meta": {"id": id, "type": "tv", "name": "Unknown"}}
    cache_set("meta:" + id, result, 300)
    return result


@app.get("/stream/tv/{id}.json")
async def stream(id: str, request: Request):
    if not id.startswith("dr_ep_"):
        return {"streams": []}
    if cached := cache_get("stream:" + id):
        return cached
    page, client = id_url(id, "dr_ep_"), request.app.state.http
    found: list[dict] = []
    try:
        source, final = await get_text(client, page)
        soup = BeautifulSoup(source, "html.parser")
        candidates = [final]
        for tag in soup.select("iframe[src], iframe[data-src], video[src], source[src], embed[src]"):
            for attr in ("src", "data-src"):
                candidate = normalize(tag.get(attr), final)
                if candidate and safe_url(candidate) and candidate not in candidates:
                    candidates.append(candidate)
        # Also inspect media/iframe URLs hidden in inline JavaScript.
        candidates.extend(x for x in extract_media(source, final) if x not in candidates)
        sem = asyncio.Semaphore(STREAM_CONCURRENCY)
        async def inspect(candidate: str):
            async with sem:
                return await streams_from_url(client, candidate, page, request)
        batches = await asyncio.gather(*(inspect(x) for x in candidates[:STREAM_CANDIDATES_LIMIT]), return_exceptions=True)
        for batch in batches:
            if isinstance(batch, list):
                found.extend(batch)
    except Exception as exc:
        logger.warning("Stream extraction failed for %s: %s", page, exc)
    result = {"streams": dedupe(found)[:MAX_STREAMS_PER_REQUEST]}
    if result["streams"]:
        cache_set("stream:" + id, result, 120)
    return result


@app.exception_handler(Exception)
async def unhandled(_: Request, exc: Exception):
    logger.exception("Unhandled addon error", exc_info=exc)
    return JSONResponse({"error": "Internal addon error"}, status_code=500)
