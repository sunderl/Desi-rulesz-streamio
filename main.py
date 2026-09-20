import asyncio
import base64
import binascii
import ipaddress
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("desirulez")

TARGET_SITE = os.getenv("TARGET_SITE", "https://desiruleztv.net").rstrip("/")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
DEFAULT_POSTER = os.getenv(
    "DEFAULT_POSTER",
    "https://images.unsplash.com/photo-1593784991095-a205069470b6?w=500&q=80",
)

MAX_URL_LENGTH = 2048
MAX_CATALOG_ITEMS = 60
MAX_STREAMS_PER_REQUEST = 30

ALLOWED_MEDIA_HOSTS = {
    "desiruleztv.net",
    "vk.com",
    "vkvideo.ru",
    "vkprime.com",
    "streamwish.to",
    "streamwish.com",
    "filelions.to",
    "filelions.site",
    "doodstream.com",
    "dood.so",
    "streamtape.com",
    "streamtape.site",
    "vidoza.net",
    "vidsrc.me",
    "akamaized.net",
    "cloudfront.net",
}

POPULAR_SERIALS = [
    {
        "name": "Anupamaa",
        "url": f"{TARGET_SITE}/category/anupama/",
        "poster": "https://upload.wikimedia.org/wikipedia/en/8/80/Anupamaa_TV_Series.jpg",
    },
    {
        "name": "Yeh Rishta Kya Kehlata Hai",
        "url": f"{TARGET_SITE}/category/yeh-rishta-kya-kehlata-hai/",
        "poster": DEFAULT_POSTER,
    },
    {
        "name": "Taarak Mehta Ka Ooltah Chashmah",
        "url": f"{TARGET_SITE}/category/taarak-mehta-ka-ooltah-chashmah/",
        "poster": DEFAULT_POSTER,
    },
]

USER_AGENT = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
)
SITE_HEADERS = {
    "User-Agent": USER_AGENT,
    "Referer": TARGET_SITE + "/",
    "Accept-Language": "en-US,en;q=0.8",
}

MANIFEST = {
    "id": "org.desiruleztv.production.addon",
    "version": "5.0.1",
    "name": "DesiRulez TV",
    "description": "Indian TV catalog with resilient extraction and HLS/HTTP proxying.",
    "logo": DEFAULT_POSTER,
    "resources": ["catalog", "meta", "stream"],
    "types": ["tv"],
    "idPrefixes": ["dr_"],
    "catalogs": [
        {
            "type": "tv",
            "id": "desirulez_popular",
            "name": "Top Serials",
            "extra": [{"name": "search", "isRequired": False}],
        },
        {
            "type": "tv",
            "id": "desirulez_latest",
            "name": "Latest Episodes",
            "extra": [{"name": "search", "isRequired": False}],
        },
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http = httpx.AsyncClient(
        headers=SITE_HEADERS,
        follow_redirects=True,
        timeout=httpx.Timeout(18.0, connect=7.0, read=15.0),
        limits=httpx.Limits(max_connections=80, max_keepalive_connections=30),
    )
    yield
    await app.state.http.aclose()


app = FastAPI(title="DesiRulez TV Addon", version=MANIFEST["version"], lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "HEAD", "OPTIONS"],
    allow_headers=["*"],
)


# -----------------------------------------------------------------------------
# URL, encoding and safety helpers
# -----------------------------------------------------------------------------
def encode_url(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def decode_url(value: str) -> str:
    try:
        padded = value + "=" * (-len(value) % 4)
        return base64.urlsafe_b64decode(padded.encode()).decode()
    except (binascii.Error, UnicodeError, ValueError) as exc:
        raise HTTPException(400, "Malformed URL token") from exc


def host_allowed(url: str) -> bool:
    try:
        if len(url) > MAX_URL_LENGTH:
            return False
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme not in {"http", "https"} or not host:
            return False
        if not any(host == x or host.endswith("." + x) for x in ALLOWED_MEDIA_HOSTS):
            return False
        try:
            address = ipaddress.ip_address(host)
            if (
                address.is_private
                or address.is_loopback
                or address.is_link_local
                or address.is_reserved
                or address.is_multicast
            ):
                return False
        except ValueError:
            pass
        return True
    except ValueError:
        return False


def safe_media_url(url: str) -> str | None:
    return url if host_allowed(url) else None


def normalize(raw: str | None, base: str) -> str | None:
    if not raw:
        return None
    value = str(raw).strip().strip("\"'")
    value = value.replace("\\/", "/").replace("\\u0026", "&")
    value = value.replace("\\u003F", "?").replace("\\u003f", "?")
    value = value.replace("\\u003D", "=").replace("\\u003d", "=")
    value = unquote(value)
    if value.startswith("//"):
        value = "https:" + value
    return urljoin(base, value)


def is_playlist(url: str) -> bool:
    return urlparse(url).path.lower().endswith((".m3u8", ".m3u")) or "m3u8" in url.lower()


def public_base(request: Request) -> str:
    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    forwarded_host = request.headers.get("x-forwarded-host", "").split(",")[0].strip()
    if forwarded_host:
        return f"{forwarded_proto or request.url.scheme}://{forwarded_host}/"
    return (PUBLIC_BASE_URL or str(request.base_url).rstrip("/")).rstrip("/") + "/"


def proxy_link(request: Request, target: str) -> str:
    encoded = encode_url(target)
    if is_playlist(target):
        return f"{public_base(request)}proxy/hls?url={encoded}"
    return f"{public_base(request)}proxy?url={encoded}"


def page_id(url: str, prefix: str = "dr_ep_") -> str:
    return prefix + encode_url(url)


def id_url(value: str, prefix: str) -> str:
    if not value.startswith(prefix):
        raise HTTPException(400, "Invalid addon id")
    return decode_url(value[len(prefix):])


# -----------------------------------------------------------------------------
# Source parsing
# -----------------------------------------------------------------------------
def text_of(tag) -> str:
    return " ".join(tag.stripped_strings) if tag else ""


def title_from_page(soup: BeautifulSoup, fallback: str = "Serial Episode") -> str:
    for selector in ("h1", "article h1", ".entry-title", ".post-title", "title"):
        tag = soup.select_one(selector)
        if text_of(tag):
            return text_of(tag)
    return fallback


def date_from_title(value: str) -> str | None:
    months = {name.lower(): number for number, names in enumerate(
        (
            "january jan",
            "february feb",
            "march mar",
            "april apr",
            "may",
            "june jun",
            "july jul",
            "august aug",
            "september sep",
            "october oct",
            "november nov",
            "december dec",
        ),
        1,
    ) for name in names.split()}
    match = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?[\s,.-]+([A-Za-z]+)[\s,.-]+(\d{4})\b", value, re.I)
    if not match:
        return None
    day, month, year = match.groups()
    month_number = months.get(month.lower())
    return f"{year}-{month_number}-{int(day):02d}" if month_number else None


def is_episode_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    return any(x in path for x in ("/episode", "/watch-online", "-episode-", "/serial/", "/videos/"))


def poster_for(anchor, base: str) -> str:
    image = anchor.find("img") or (anchor.parent.find("img") if anchor.parent else None)
    if image:
        for key in ("data-src", "data-lazy-src", "src", "data-original"):
            value = normalize(image.get(key), base)
            if value:
                return value
    return DEFAULT_POSTER


def episode_links(html: str, base: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    result, seen = [], set()
    for anchor in soup.select("a[href]"):
        url = normalize(anchor.get("href"), base)
        title = text_of(anchor)
        if not url or url in seen or len(title) < 4 or not is_episode_url(url):
            continue
        if urlparse(url).netloc.lower() != urlparse(TARGET_SITE).netloc.lower():
            continue
        seen.add(url)
        result.append({"url": url, "title": title, "poster": poster_for(anchor, base)})
    return result


MEDIA_RE = re.compile(r"(?:https?:)?//[^\"'<>\\ ]+?(?:\.m3u8|\.mp4|\.m4v)(?:\?[^\"'<>\\ ]*)?", re.I)


def extract_media(html: str, base: str) -> list[str]:
    cleaned = (
        html.replace("\\/", "/")
        .replace("\\u0026", "&")
        .replace("\\u003F", "?")
        .replace("\\u003f", "?")
        .replace("\\u003D", "=")
        .replace("\\u003d", "=")
    )
    candidates: set[str] = set()
    for raw in MEDIA_RE.findall(cleaned):
        url = normalize(raw, base)
        if url and safe_media_url(url):
            candidates.add(url)
    soup = BeautifulSoup(cleaned, "html.parser")
    for tag in soup.find_all(["video", "source"]):
        for attribute in ("src", "data-src", "data-video", "data-file", "data-url"):
            url = normalize(tag.get(attribute), base)
            if url and safe_media_url(url):
                candidates.add(url)
    for key in ("file", "src", "source", "hls", "playlist"):
        for match in re.finditer(rf"[\"']{key}[\"']\s*:\s*[\"']([^\"']+)", cleaned, re.I):
            url = normalize(match.group(1), base)
            if url and (is_playlist(url) or ".mp4" in url.lower()) and safe_media_url(url):
                candidates.add(url)
    return list(candidates)


async def get_text(client: httpx.AsyncClient, url: str, headers: dict | None = None) -> tuple[str, str]:
    last: Exception | None = None
    for attempt in range(3):
        try:
            response = await client.get(url, headers=headers, timeout=15.0)
            response.raise_for_status()
            return response.text, str(response.url)
        except httpx.HTTPError as exc:
            last = exc
            if attempt < 2:
                await asyncio.sleep(0.25 * (attempt + 1))
    raise last or RuntimeError("request failed")


async def streams_from_url(client: httpx.AsyncClient, url: str, referer: str, request: Request) -> list[dict]:
    try:
        html, final_url = await get_text(client, url, {**SITE_HEADERS, "Referer": referer})
    except Exception as exc:
        logger.info("Could not inspect %s: %s", url, exc)
        return []
    result = []
    for media in extract_media(html, final_url):
        result.append(
            {
                "name": "DesiRulez",
                "title": "HLS Stream" if is_playlist(media) else "MP4 Stream",
                "url": proxy_link(request, media),
                "behaviorHints": {"bingeGroup": "desirulez", "notWebReady": False},
            }
        )
    return result


# -----------------------------------------------------------------------------
# Proxy endpoints
# -----------------------------------------------------------------------------
@app.get("/proxy")
async def proxy_media(url: str, request: Request, range_header: str | None = Header(None, alias="Range")):
    target = decode_url(url)
    if not safe_media_url(target):
        raise HTTPException(403, "Media host is not allowed")

    headers = {
        "User-Agent": USER_AGENT,
        "Referer": TARGET_SITE + "/",
        "Origin": TARGET_SITE,
        "Accept": "*/*",
    }
    if range_header:
        headers["Range"] = range_header

    client: httpx.AsyncClient = request.app.state.http
    try:
        response = await client.send(client.build_request("GET", target, headers=headers), stream=True)
        if not safe_media_url(str(response.url)) or response.status_code >= 400:
            status = response.status_code if response.status_code >= 400 else 403
            await response.aclose()
            raise HTTPException(status, "Upstream media request failed")

        passthrough = {
            key: response.headers[key]
            for key in (
                "content-type",
                "content-length",
                "content-range",
                "accept-ranges",
                "cache-control",
                "etag",
            )
            if key in response.headers
        }
        passthrough.update({"Access-Control-Allow-Origin": "*", "Access-Control-Expose-Headers": "*"})

        async def body():
            try:
                async for chunk in response.aiter_bytes(1024 * 256):
                    yield chunk
            finally:
                await response.aclose()

        return StreamingResponse(body(), status_code=response.status_code, headers=passthrough)
    except HTTPException:
        raise
    except httpx.HTTPError as exc:
        logger.warning("Proxy failed: %s", exc)
        raise HTTPException(502, "Upstream media connection failed") from exc


@app.get("/proxy/hls")
async def proxy_hls(url: str, request: Request):
    playlist = decode_url(url)
    if not safe_media_url(playlist):
        raise HTTPException(403, "Playlist host is not allowed")

    client: httpx.AsyncClient = request.app.state.http
    try:
        response = await client.get(
            playlist,
            headers={**SITE_HEADERS, "Accept": "application/vnd.apple.mpegurl,*/*"},
            timeout=15.0,
        )
        response.raise_for_status()
        final_url = str(response.url)
        if not safe_media_url(final_url):
            raise HTTPException(403, "Redirected playlist host is not allowed")

        def rewrite_hls_uri(line: str) -> str:
            def replace(match):
                quote = match.group(1)
                value = match.group(2)
                target = normalize(value, final_url)
                if not target or not safe_media_url(target):
                    return match.group(0)
                proxied = proxy_link(request, target)
                return f"URI={quote}{proxied}{quote}"

            return re.sub(r'URI\s*=\s*(["\'])(.*?)\1', replace, line, flags=re.I)

        output: list[str] = []
        for original in response.text.splitlines():
            line = original.strip()
            if not line:
                output.append("")
                continue
            if line.startswith("#"):
                output.append(rewrite_hls_uri(line))
                continue
            target = normalize(line, final_url)
            output.append(proxy_link(request, target) if target and safe_media_url(target) else original)
        return Response(
            "\n".join(output) + "\n",
            media_type="application/vnd.apple.mpegurl",
            headers={"Cache-Control": "no-cache", "Access-Control-Allow-Origin": "*"},
        )
    except HTTPException:
        raise
    except httpx.HTTPError as exc:
        raise HTTPException(502, "HLS playlist request failed") from exc


# -----------------------------------------------------------------------------
# Stremio routes
# -----------------------------------------------------------------------------
@app.get("/")
def home():
    return {"status": "DesiRulez addon active", "manifest": "/manifest.json"}


@app.get("/health")
def health():
    return {"status": "ok", "version": MANIFEST["version"]}


@app.get("/manifest.json")
def manifest():
    return MANIFEST


@app.get("/catalog/tv/{catalog_id}.json")
@app.get("/catalog/tv/{catalog_id}/search={query}.json")
async def catalog(catalog_id: str, request: Request, query: str | None = None):
    query = (query or "").strip()
    key = f"catalog:{catalog_id}:{query.lower()}"
    if cached := cache_get(key):
        return cached

    if catalog_id == "desirulez_popular" and not query:
        payload = {
            "metas": [
                {
                    "id": page_id(x["url"], "dr_cat_"),
                    "type": "tv",
                    "name": x["name"],
                    "poster": x["poster"],
                    "description": f"Episodes for {x['name']}",
                }
                for x in POPULAR_SERIALS
            ]
        }
        cache_set(key, payload, 900)
        return payload

    if catalog_id == "desirulez_latest":
        target = f"{TARGET_SITE}/"
        try:
            html, final = await get_text(request.app.state.http, target)
            items = episode_links(html, final)
        except Exception as exc:
            logger.warning("Latest catalog failed: %s", exc)
            items = []
        payload = {
            "metas": [
                {
                    "id": page_id(x["url"]),
                    "type": "tv",
                    "name": x["title"],
                    "poster": x["poster"],
                    "description": x["title"],
                }
                for x in items[:MAX_CATALOG_ITEMS]
            ]
        }
        cache_set(key, payload, 300)
        return payload

    target = f"{TARGET_SITE}/?s={query.replace(' ', '+')}" if query else TARGET_SITE
    try:
        html, final = await get_text(request.app.state.http, target)
        items = episode_links(html, final)
    except Exception as exc:
        logger.warning("Catalog failed: %s", exc)
        items = []

    payload = {
        "metas": [
            {
                "id": page_id(x["url"]),
                "type": "tv",
                "name": x["title"],
                "poster": x["poster"],
                "description": x["title"],
            }
            for x in items[:MAX_CATALOG_ITEMS]
        ]
    }
    cache_set(key, payload, 300)
    return payload


@app.get("/meta/tv/{id}.json")
async def meta(id: str, request: Request):
    key = "meta:" + id
    if cached := cache_get(key):
        return cached

    client = request.app.state.http

    if id.startswith("dr_cat_"):
        category = id_url(id, "dr_cat_")
        show = next((x for x in POPULAR_SERIALS if x["url"] == category), None)
        try:
            html, final = await get_text(client, category)
            links = episode_links(html, final)
        except Exception:
            links = []

        videos = []
        for number, item in enumerate(links[:100], 1):
            video = {"id": page_id(item["url"]), "title": item["title"], "season": 1, "episode": number}
            if released := date_from_title(item["title"]):
                video["released"] = released
            videos.append(video)

        result = {
            "meta": {
                "id": id,
                "type": "tv",
                "name": show["name"] if show else "Indian Serial",
                "poster": show["poster"] if show else DEFAULT_POSTER,
                "videos": videos,
            }
        }
    elif id.startswith("dr_ep_"):
        episode = id_url(id, "dr_ep_")
        try:
            html, _ = await get_text(client, episode)
            title = title_from_page(BeautifulSoup(html, "html.parser"))
        except Exception:
            title = "Serial Episode"

        video = {"id": id, "title": title, "season": 1, "episode": 1}
        if released := date_from_title(title):
            video["released"] = released

        result = {
            "meta": {
                "id": id,
                "type": "tv",
                "name": title,
                "poster": DEFAULT_POSTER,
                "videos": [video],
            }
        }
    else:
        result = {"meta": {"id": id, "type": "tv", "name": "Unknown"}}

    cache_set(key, result, 300)
    return result


@app.get("/stream/tv/{id}.json")
async def stream(id: str, request: Request):
    if not id.startswith("dr_ep_"):
        return {"streams": []}

    key = "stream:" + id
    if cached := cache_get(key):
        return cached

    page = id_url(id, "dr_ep_")
    client = request.app.state.http
    found: list[dict] = []

    try:
        html, final = await get_text(client, page)
        soup = BeautifulSoup(html, "html.parser")
        candidates = [final]
        for iframe in soup.select("iframe[src], video[src], source[src]"):
            candidate = normalize(iframe.get("src"), final)
            if candidate and candidate not in candidates:
                candidates.append(candidate)

        sem = asyncio.Semaphore(6)

        async def inspect(candidate: str):
            async with sem:
                return await streams_from_url(client, candidate, page, request)

        batches = await asyncio.gather(*(inspect(x) for x in candidates[:12]), return_exceptions=True)
        for batch in batches:
            if isinstance(batch, list):
                found.extend(batch)
    except Exception as exc:
        logger.warning("Stream extraction failed for %s: %s", page, exc)

    unique, seen = [], set()
    for item in found:
        if item["url"] not in seen:
            seen.add(item["url"])
            unique.append(item)

    result = {"streams": unique[:MAX_STREAMS_PER_REQUEST]}
    if unique:
        cache_set(key, result, 120)
    return result


@app.exception_handler(Exception)
async def unhandled(_: Request, exc: Exception):
    logger.exception("Unhandled addon error", exc_info=exc)
    return JSONResponse({"error": "Internal addon error"}, status_code=500)
