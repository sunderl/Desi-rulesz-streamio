import os
import re
import time
import base64
import logging
import asyncio
import ipaddress
from contextlib import asynccontextmanager
from urllib.parse import urlparse, urljoin, unquote
from fastapi import FastAPI, Request, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
import httpx
from bs4 import BeautifulSoup

# Logging Setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("desirulez")

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
TARGET_SITE = "https://desiruleztv.net"
DEFAULT_POSTER = "https://images.unsplash.com/photo-1593784991095-a205069470b6?w=500&q=80"

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
    "vidoza.net",
}

POPULAR_SERIALS = [
    {"name": "Anupamaa", "url": "https://desiruleztv.net/category/anupama/", "poster": "https://upload.wikimedia.org/wikipedia/en/8/80/Anupamaa_TV_Series.jpg"},
    {"name": "Yeh Rishta Kya Kehlata Hai", "url": "https://desiruleztv.net/category/yeh-rishta-kya-kehlata-hai/", "poster": "https://upload.wikimedia.org/wikipedia/en/b/b8/Yeh_Rishta_Kya_Kehlata_Hai_logo.jpg"},
    {"name": "Taarak Mehta Ka Ooltah Chashmah", "url": "https://desiruleztv.net/category/taarak-mehta-ka-ooltah-chashmah/", "poster": "https://upload.wikimedia.org/wikipedia/en/8/86/Taarak_Mehta_Ka_Ooltah_Chashmah_logo.jpg"},
]

MANIFEST = {
    "id": "org.desiruleztv.production.addon",
    "version": "4.1.0",
    "name": "DesiRulez TV (Production)",
    "description": "Watch Indian Serials via fast parallel extraction, memory-safe streaming, and HLS proxying.",
    "resources": ["catalog", "meta", "stream"],
    "types": ["tv"],
    "catalogs": [
        {"type": "tv", "id": "desirulez_popular", "name": "Top Serials", "extra": [{"name": "search", "isRequired": False}]},
        {"type": "tv", "id": "desirulez_latest", "name": "Latest Daily Episodes", "extra": [{"name": "search", "isRequired": False}]}
    ],
    "idPrefixes": ["dr_"]
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": TARGET_SITE
}

# Monotonic Cache Store
CACHE_STORE: dict[str, tuple[float, dict]] = {}

def get_cache(key: str) -> dict | None:
    item = CACHE_STORE.get(key)
    if not item:
        return None
    expires_at, data = item
    if time.monotonic() >= expires_at:
        CACHE_STORE.pop(key, None)
        return None
    return data

def set_cache(key: str, data: dict, ttl_seconds: int = 300) -> None:
    CACHE_STORE[key] = (time.monotonic() + ttl_seconds, data)

# Shared HTTPX Client Lifespan
@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http = httpx.AsyncClient(
        headers=HEADERS,
        follow_redirects=True,
        timeout=httpx.Timeout(20.0, connect=8.0),
        limits=httpx.Limits(max_keepalive_connections=20, max_connections=100)
    )
    yield
    await app.state.http.aclose()

app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# SSRF and URL Helpers
def is_allowed_media_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return False

    if parsed.scheme not in {"http", "https"}:
        return False

    host = (parsed.hostname or "").lower()
    if not host or not any(host == allowed or host.endswith("." + allowed) for allowed in ALLOWED_MEDIA_HOSTS):
        return False

    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_reserved or ip.is_link_local:
            return False
    except ValueError:
        pass

    return True

def validate_target_url(target_url: str) -> None:
    if not is_allowed_media_url(target_url):
        raise HTTPException(status_code=403, detail="Target media URL host is restricted or invalid")

def get_base_url(request: Request) -> str:
    if PUBLIC_BASE_URL:
        return PUBLIC_BASE_URL + "/"
    return str(request.base_url)

def encode_url(url: str) -> str:
    return base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")

def decode_url(encoded_str: str) -> str:
    padding = "=" * (-len(encoded_str) % 4)
    return base64.urlsafe_b64decode(encoded_str + padding).decode()

def normalize_url(raw_url: str, base_url: str) -> str | None:
    if not raw_url:
        return None
    value = raw_url.strip().replace("\\/", "/").replace("\\u0026", "&")
    value = unquote(value)
    if value.startswith("//"):
        value = "https:" + value
    return urljoin(base_url, value)

def is_hls_url(url: str) -> bool:
    return urlparse(url).path.lower().endswith(".m3u8")

def is_media_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    return path.endswith((".m3u8", ".mp4", ".m4v", ".ts", ".m4s"))

def parse_date_from_title(title: str) -> str | None:
    months = {
        "january": "01", "february": "02", "march": "03", "april": "04",
        "may": "05", "june": "06", "july": "07", "august": "08",
        "september": "09", "october": "10", "november": "11", "december": "12",
        "jan": "01", "feb": "02", "mar": "03", "apr": "04", "jun": "06",
        "jul": "07", "aug": "08", "sep": "09", "oct": "10", "nov": "11", "dec": "12"
    }
    match = re.search(r'(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+)\s+(\d{4})', title, re.IGNORECASE)
    if match:
        day, month_str, year = match.groups()
        month = months.get(month_str.lower(), "01")
        return f"{year}-{month}-{int(day):02d}"
    return None

async def fetch_text_with_retry(client: httpx.AsyncClient, url: str, retries: int = 2) -> str:
    for attempt in range(retries + 1):
        try:
            res = await client.get(url, timeout=12.0)
            res.raise_for_status()
            return res.text
        except (httpx.HTTPError, httpx.TimeoutException):
            if attempt == retries:
                raise
            await asyncio.sleep(0.4 * (attempt + 1))
    return ""

# Media Proxy Endpoints
@app.get("/proxy")
async def proxy_media(
    url: str,
    request: Request,
    range_header: str | None = Header(default=None, alias="Range"),
):
    try:
        target_url = decode_url(url)
    except Exception:
        raise HTTPException(status_code=400, detail="Malformed base64 URL")

    validate_target_url(target_url)

    request_headers = {
        "User-Agent": HEADERS["User-Agent"],
        "Referer": TARGET_SITE,
        "Origin": TARGET_SITE,
        "Accept": "*/*",
    }
    if range_header:
        request_headers["Range"] = range_header

    client: httpx.AsyncClient = request.app.state.http

    try:
        req = client.build_request("GET", target_url, headers=request_headers)
        response = await client.send(req, stream=True)

        try:
            validate_target_url(str(response.url))
        except HTTPException:
            await response.aclose()
            raise

        if response.status_code >= 400:
            await response.aclose()
            raise HTTPException(status_code=response.status_code, detail="Upstream returned error status")

        response_headers = {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "*",
        }
        for h in ("content-type", "content-length", "content-range", "accept-ranges", "cache-control", "etag"):
            if h in response.headers:
                response_headers[h] = response.headers[h]

        async def iterator():
            try:
                async for chunk in response.aiter_bytes():
                    yield chunk
            finally:
                await response.aclose()

        return StreamingResponse(
            iterator(),
            status_code=response.status_code,
            headers=response_headers,
            media_type=response.headers.get("content-type"),
        )
    except HTTPException:
        raise
    except httpx.HTTPError as exc:
        logger.error("Proxy HTTP error for %s: %s", target_url, exc)
        raise HTTPException(status_code=502, detail="Upstream media connection failed")
    except Exception as exc:
        logger.error("Unexpected proxy error for %s: %s", target_url, exc)
        raise HTTPException(status_code=500, detail="Internal proxy failure")

@app.get("/proxy/hls")
async def proxy_hls(url: str, request: Request):
    try:
        playlist_url = decode_url(url)
    except Exception:
        raise HTTPException(status_code=400, detail="Malformed base64 URL")

    validate_target_url(playlist_url)

    client: httpx.AsyncClient = request.app.state.http

    try:
        response = await client.get(playlist_url)
        validate_target_url(str(response.url))

        if response.status_code != 200:
            raise HTTPException(status_code=502, detail=f"Upstream HLS returned HTTP {response.status_code}")

        public_base = get_base_url(request).rstrip("/")
        output_lines = []

        for line in response.text.splitlines():
            line_str = line.strip()
            if not line_str:
                output_lines.append("")
                continue

            if line_str.startswith("#"):
                def replace_uri(match):
                    quote = match.group(1)
                    raw_uri = match.group(2)
                    full_key_url = normalize_url(raw_uri, str(response.url))
                    if not full_key_url or not is_allowed_media_url(full_key_url):
                        return match.group(0)
                    encoded_key = encode_url(full_key_url)
                    return f'URI={quote}{public_base}/proxy?url={encoded_key}{quote}'

                line_str = re.sub(r'URI\s*=\s*(["\'])(.*?)\1', replace_uri, line_str, flags=re.IGNORECASE)
                output_lines.append(line_str)
                continue

            segment_url = normalize_url(line_str, str(response.url))
            if segment_url and is_allowed_media_url(segment_url):
                encoded_segment = encode_url(segment_url)
                if is_hls_url(segment_url):
                    output_lines.append(f"{public_base}/proxy/hls?url={encoded_segment}")
                else:
                    output_lines.append(f"{public_base}/proxy?url={encoded_segment}")
            else:
                output_lines.append(line_str)

        return Response(
            content="\n".join(output_lines),
            media_type="application/vnd.apple.mpegurl",
            headers={"Cache-Control": "no-cache", "Access-Control-Allow-Origin": "*"},
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("HLS Proxy error for %s: %s", playlist_url, exc)
        raise HTTPException(status_code=502, detail="HLS playlist rewriting failed")

# Addon Routes
@app.get("/health")
def health():
    return {"status": "ok", "public_base_url": PUBLIC_BASE_URL or "dynamic"}

@app.get("/")
def home():
    return {"status": "DesiRulez Addon Active!", "manifest": "/manifest.json"}

@app.get("/manifest.json")
def manifest():
    return MANIFEST

@app.get("/catalog/tv/{catalog_id}.json")
@app.get("/catalog/tv/{catalog_id}/search={query}.json")
async def catalog_tv(catalog_id: str, request: Request, query: str = None):
    cache_key = f"catalog:{catalog_id}:{query or 'all'}"
    cached = get_cache(cache_key)
    if cached:
        return cached

    metas = []
    seen_urls = set()

    if catalog_id == "desirulez_popular" and not query:
        for show in POPULAR_SERIALS:
            metas.append({
                "id": f"dr_cat_{encode_url(show['url'])}",
                "type": "tv",
                "name": show["name"],
                "poster": show["poster"],
                "description": f"Episodes for {show['name']}"
            })
        res_payload = {"metas": metas}
        set_cache(cache_key, res_payload, ttl_seconds=600)
        return res_payload

    target_fetch_url = f"{TARGET_SITE}/?s={query.replace(' ', '+')}" if query else TARGET_SITE
    client: httpx.AsyncClient = request.app.state.http

    try:
        html = await fetch_text_with_retry(client, target_fetch_url)
        soup = BeautifulSoup(html, "html.parser")

        for a in soup.find_all("a", href=True):
            full_url = normalize_url(a["href"], TARGET_SITE)
            title = a.text.strip()

            if not full_url or full_url in seen_urls or any(full_url.lower().endswith(ext) for ext in (".jpg", ".png", ".pdf", ".css", ".js")):
                continue

            if any(token in full_url.lower() for token in ("/episode", "/watch-online", "-episode-", "/serial/")) and len(title) > 5:
                seen_urls.add(full_url)
                
                img_tag = a.find("img") or (a.parent.find("img") if a.parent else None)
                poster = DEFAULT_POSTER
                if img_tag:
                    src = img_tag.get("src") or img_tag.get("data-src") or img_tag.get("data-lazy-src")
                    normalized_img = normalize_url(src, TARGET_SITE)
                    if normalized_img:
                        poster = normalized_img

                metas.append({
                    "id": f"dr_ep_{encode_url(full_url)}",
                    "type": "tv",
                    "name": title,
                    "poster": poster,
                    "description": title
                })
    except Exception as e:
        logger.error("Catalog Fetch Error (%s): %s", target_fetch_url, e)

    res_payload = {"metas": metas[:40]}
    set_cache(cache_key, res_payload, ttl_seconds=300)
    return res_payload

@app.get("/meta/tv/{id}.json")
async def meta_tv(id: str, request: Request):
    cache_key = f"meta:{id}"
    cached = get_cache(cache_key)
    if cached:
        return cached

    client: httpx.AsyncClient = request.app.state.http

    if id.startswith("dr_cat_"):
        try:
            category_url = decode_url(id.replace("dr_cat_", ""))
        except Exception:
            category_url = TARGET_SITE

        videos = []
        seen_urls = set()
        show_name = next((s["name"] for s in POPULAR_SERIALS if s["url"] == category_url), "Indian Serial")
        show_poster = next((s["poster"] for s in POPULAR_SERIALS if s["url"] == category_url), DEFAULT_POSTER)

        try:
            html = await fetch_text_with_retry(client, category_url)
            soup = BeautifulSoup(html, "html.parser")
            
            ep_idx = 1
            for a in soup.find_all("a", href=True):
                full_url = normalize_url(a["href"], TARGET_SITE)
                title = a.text.strip()

                if not full_url or full_url in seen_urls:
                    continue

                if any(token in full_url.lower() for token in ("/episode", "/watch-online", "-episode-")) and len(title) > 5:
                    seen_urls.add(full_url)
                    released_date = parse_date_from_title(title)
                    item = {"id": f"dr_ep_{encode_url(full_url)}", "title": title, "season": 1, "episode": ep_idx}
                    if released_date:
                        item["released"] = released_date
                    videos.append(item)
                    ep_idx += 1
        except Exception as e:
            logger.error("Meta Category Error (%s): %s", category_url, e)

        res_payload = {"meta": {"id": id, "type": "tv", "name": show_name, "poster": show_poster, "description": f"Episodes for {show_name}", "videos": videos[:60]}}
        set_cache(cache_key, res_payload, ttl_seconds=300)
        return res_payload

    elif id.startswith("dr_ep_"):
        try:
            ep_url = decode_url(id.replace("dr_ep_", ""))
        except Exception:
            ep_url = TARGET_SITE

        title = "Serial Episode"
        released_date = None

        try:
            html = await fetch_text_with_retry(client, ep_url)
            soup = BeautifulSoup(html, "html.parser")
            h1 = soup.find("h1")
            if h1:
                title = h1.text.strip()
                released_date = parse_date_from_title(title)
        except Exception:
            pass

        item = {"id": id, "title": title, "season": 1, "episode": 1}
        if released_date:
            item["released"] = released_date

        res_payload = {"meta": {"id": id, "type": "tv", "name": title, "poster": DEFAULT_POSTER, "description": title, "videos": [item]}}
        set_cache(cache_key, res_payload, ttl_seconds=300)
        return res_payload

    return {"meta": {"id": id, "type": "tv", "name": "Unknown"}}

def extract_media_urls(html: str, page_url: str) -> list[str]:
    html = html.replace("\\/", "/").replace("\\u0026", "&").replace("\\u003F", "?").replace("\\u003d", "=")
    candidates = set()

    for match in re.findall(r"""["']([^"']+)["']""", html):
        if any(ext in match.lower() for ext in (".m3u8", ".mp4", ".m4v")):
            full_url = normalize_url(match, page_url)
            if full_url and is_media_url(full_url):
                candidates.add(full_url)

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(["video", "source"]):
        for attr in ("src", "data-src", "data-video", "data-file"):
            raw_url = tag.get(attr)
            full_url = normalize_url(raw_url, page_url)
            if full_url and is_media_url(full_url):
                candidates.add(full_url)

    return list(candidates)

async def extract_direct_streams(embed_url: str, client: httpx.AsyncClient, base_url: str, referer: str) -> list[dict]:
    found_streams = []
    headers = {"User-Agent": HEADERS["User-Agent"], "Referer": referer, "Origin": TARGET_SITE}

    try:
        res = await client.get(embed_url, headers=headers, timeout=8.0)
        if res.status_code >= 400:
            return []

        for normalized in extract_media_urls(res.text, str(res.url)):
            if not is_allowed_media_url(normalized):
                continue

            encoded_media = encode_url(normalized)
            if is_hls_url(normalized):
                proxy_link = f"{base_url}proxy/hls?url={encoded_media}"
                title = "HLS Stream"
                hints = {"bingeGroup": "desirulez-hls"}
            else:
                proxy_link = f"{base_url}proxy?url={encoded_media}"
                title = "MP4 Stream"
                hints = {"notWebReady": False}

            if proxy_link not in [s["url"] for s in found_streams]:
                found_streams.append({"name": "DesiRulez", "title": title, "url": proxy_link, "behaviorHints": hints})
    except Exception as e:
        logger.warning("Stream extraction failed for %s: %s", embed_url, e)

    return found_streams

@app.get("/stream/tv/{id}.json")
async def stream_tv(id: str, request: Request):
    if not id.startswith("dr_ep_"):
        return {"streams": []}

    cache_key = f"stream:{id}"
    cached = get_cache(cache_key)
    if cached:
        return cached

    try:
        page_url = decode_url(id.replace("dr_ep_", ""))
    except Exception:
        return {"streams": []}

    base_server_url = get_base_url(request)
    client: httpx.AsyncClient = request.app.state.http
    streams = []

    try:
        html = await fetch_text_with_retry(client, page_url)
        soup = BeautifulSoup(html, "html.parser")

        iframe_urls = []
        for iframe in soup.find_all("iframe", src=True):
            src = normalize_url(iframe["src"].strip(), page_url)
            if src and src not in iframe_urls:
                iframe_urls.append(src)

        semaphore = asyncio.Semaphore(4)
        async def extract_one(src_url: str):
            async with semaphore:
                return await extract_direct_streams(src_url, client, base_server_url, page_url)

        results = await asyncio.gather(*[extract_one(src) for src in iframe_urls[:8]], return_exceptions=True)
        for res in results:
            if isinstance(res, list):
                streams.extend(res)

        direct_streams = await extract_direct_streams(page_url, client, base_server_url, TARGET_SITE)
        streams.extend(direct_streams)
    except Exception as e:
        logger.error("Stream extraction failed for page %s: %s", page_url, e)

    unique_streams = []
    seen = set()
    for s in streams:
        if s["url"] not in seen:
            seen.add(s["url"])
            unique_streams.append(s)

    res_payload = {"streams": unique_streams[:20]}
    if unique_streams:
        set_cache(cache_key, res_payload, ttl_seconds=120)

    return res_payload
