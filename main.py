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

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("serials_goat_addon")

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
TARGET_SITE = "https://www.desiserials.ru"
DEFAULT_POSTER = "https://images.unsplash.com/photo-1593784991095-a205069470b6?w=500&q=80"

ALLOWED_MEDIA_HOSTS = {
    "desiserials.ru",
    "desiserials.tv",
    "showdetails.org",
    "strystagflation.showdetails.org",
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

POPULAR_CHANNELS = [
    {"name": "Star Plus", "url": f"{TARGET_SITE}/category/star-plus/", "poster": "https://upload.wikimedia.org/wikipedia/commons/4/43/Star_Plus_logo.png"},
    {"name": "Colors TV", "url": f"{TARGET_SITE}/category/colors-tv/", "poster": "https://upload.wikimedia.org/wikipedia/commons/e/ea/Colors_TV_logo.png"},
    {"name": "Zee TV", "url": f"{TARGET_SITE}/category/zee-tv/", "poster": "https://upload.wikimedia.org/wikipedia/commons/e/eb/Zee_TV_logo.png"},
    {"name": "Sony TV", "url": f"{TARGET_SITE}/category/sony-tv/", "poster": "https://upload.wikimedia.org/wikipedia/commons/5/52/Sony_Entertainment_Television_logo.png"},
    {"name": "SAB TV", "url": f"{TARGET_SITE}/category/sab-tv/", "poster": "https://upload.wikimedia.org/wikipedia/commons/e/e0/Sony_SAB_logo.png"},
    {"name": "&TV", "url": f"{TARGET_SITE}/category/and-tv/", "poster": "https://upload.wikimedia.org/wikipedia/commons/1/1b/And_TV_logo.png"}
]

MANIFEST = {
    "id": "org.desiserials.goat.addon",
    "version": "7.0.0",
    "name": "Desi Serials GOAT HD",
    "description": "Torrentio-style ultra-fast daily Hindi TV Serials streaming addon for Stremio & Novio.",
    "resources": ["catalog", "meta", "stream"],
    "types": ["tv"],
    "catalogs": [
        {"type": "tv", "id": "desiserials_channels", "name": "TV Channels", "extra": [{"name": "search", "isRequired": False}]},
        {"type": "tv", "id": "desiserials_latest", "name": "Latest Daily Episodes", "extra": [{"name": "search", "isRequired": False}]}
    ],
    "idPrefixes": ["ds_"]
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer": TARGET_SITE
}

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

@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http = httpx.AsyncClient(
        headers=HEADERS,
        follow_redirects=True,
        timeout=httpx.Timeout(20.0, connect=8.0),
        limits=httpx.Limits(max_keepalive_connections=25, max_connections=120)
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
        raise HTTPException(status_code=403, detail="Host restricted")

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

async def fetch_text(client: httpx.AsyncClient, url: str, retries: int = 2) -> str:
    for attempt in range(retries + 1):
        try:
            res = await client.get(url, timeout=12.0)
            res.raise_for_status()
            return res.text
        except (httpx.HTTPError, httpx.TimeoutException):
            if attempt == retries:
                raise
            await asyncio.sleep(0.3 * (attempt + 1))
    return ""

@app.get("/")
def home():
    return {
        "status": "online",
        "addon": "Desi Serials GOAT",
        "manifest": "/manifest.json"
    }

@app.get("/proxy")
async def proxy_media(
    url: str,
    request: Request,
    range_header: str | None = Header(default=None, alias="Range"),
):
    try:
        target_url = decode_url(url)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid URL")

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

        if response.status_code >= 400:
            await response.aclose()
            raise HTTPException(status_code=response.status_code, detail="Media fetch error")

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
    except Exception as exc:
        logger.error("Proxy error: %s", exc)
        raise HTTPException(status_code=502, detail="Proxy streaming failed")

@app.get("/proxy/hls")
async def proxy_hls(url: str, request: Request):
    try:
        playlist_url = decode_url(url)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid URL")

    validate_target_url(playlist_url)
    client: httpx.AsyncClient = request.app.state.http

    try:
        response = await client.get(playlist_url)
        if response.status_code != 200:
            raise HTTPException(status_code=502, detail="Playlist fetch failed")

        public_base = get_base_url(request).rstrip("/")
        output_lines = []

        for line in response.text.splitlines():
            line_str = line.strip()
            if not line_str:
                output_lines.append("")
                continue

            if line_str.startswith("#"):
                def replace_uri(match):
                    quote, raw_uri = match.group(1), match.group(2)
                    full_key_url = normalize_url(raw_uri, str(response.url))
                    if not full_key_url or not is_allowed_media_url(full_key_url):
                        return match.group(0)
                    return f'URI={quote}{public_base}/proxy?url={encode_url(full_key_url)}{quote}'

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
        logger.error("HLS Rewrite error: %s", exc)
        raise HTTPException(status_code=502, detail="HLS rewriting failed")

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

    if catalog_id == "desiserials_channels" and not query:
        for ch in POPULAR_CHANNELS:
            metas.append({
                "id": f"ds_cat_{encode_url(ch['url'])}",
                "type": "tv",
                "name": ch["name"],
                "poster": ch["poster"],
                "description": f"Daily Serials on {ch['name']}"
            })
        res_payload = {"metas": metas}
        set_cache(cache_key, res_payload, ttl_seconds=600)
        return res_payload

    target_url = f"{TARGET_SITE}/?s={query.replace(' ', '+')}" if query else TARGET_SITE
    client: httpx.AsyncClient = request.app.state.http

    try:
        html = await fetch_text(client, target_url)
        soup = BeautifulSoup(html, "html.parser")

        for a in soup.find_all("a", href=True):
            full_url = normalize_url(a["href"], TARGET_SITE)
            title = a.text.strip()

            if not full_url or full_url in seen_urls:
                continue

            if any(k in full_url.lower() for k in ["episode", "watch-online", "-full-episode"]) and len(title) > 5:
                seen_urls.add(full_url)
                img = a.find("img") or (a.parent.find("img") if a.parent else None)
                poster = DEFAULT_POSTER
                if img:
                    src = img.get("src") or img.get("data-src") or img.get("data-lazy-src")
                    normalized_img = normalize_url(src, TARGET_SITE)
                    if normalized_img:
                        poster = normalized_img

                metas.append({
                    "id": f"ds_ep_{encode_url(full_url)}",
                    "type": "tv",
                    "name": title,
                    "poster": poster,
                    "description": title
                })
    except Exception as e:
        logger.error("Catalog parse error: %s", e)

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

    if id.startswith("ds_cat_"):
        try:
            category_url = decode_url(id.replace("ds_cat_", ""))
        except Exception:
            category_url = TARGET_SITE

        videos = []
        seen_urls = set()
        ch_info = next((c for c in POPULAR_CHANNELS if c["url"] == category_url), {"name": "Desi Serial Channel", "poster": DEFAULT_POSTER})

        try:
            html = await fetch_text(client, category_url)
            soup = BeautifulSoup(html, "html.parser")

            ep_idx = 1
            for a in soup.find_all("a", href=True):
                full_url = normalize_url(a["href"], TARGET_SITE)
                title = a.text.strip()

                if not full_url or full_url in seen_urls:
                    continue

                if any(k in full_url.lower() for k in ["episode", "watch-online", "-full-episode"]) and len(title) > 5:
                    seen_urls.add(full_url)
                    base_ep_id = encode_url(full_url)

                    for part_num in (1, 2, 3):
                        videos.append({
                            "id": f"ds_ep_{base_ep_id}_p{part_num}",
                            "title": f"{title} [Part {part_num}]",
                            "season": 1,
                            "episode": ep_idx,
                        })
                        ep_idx += 1
        except Exception as e:
            logger.error("Meta category error: %s", e)

        res_payload = {
            "meta": {
                "id": id,
                "type": "tv",
                "name": ch_info["name"],
                "poster": ch_info["poster"],
                "description": f"Daily Full Episodes divided in Parts for {ch_info['name']}",
                "videos": videos[:90]
            }
        }
        set_cache(cache_key, res_payload, ttl_seconds=300)
        return res_payload

    elif id.startswith("ds_ep_"):
        raw_id = id.replace("ds_ep_", "")
        part_requested = 1
        if "_p" in raw_id:
            raw_id, p_str = raw_id.rsplit("_p", 1)
            try:
                part_requested = int(p_str)
            except ValueError:
                part_requested = 1

        try:
            ep_url = decode_url(raw_id)
        except Exception:
            ep_url = TARGET_SITE

        title = "Serial Episode"

        try:
            html = await fetch_text(client, ep_url)
            soup = BeautifulSoup(html, "html.parser")
            h1 = soup.find("h1")
            if h1:
                title = h1.text.strip()
        except Exception:
            pass

        res_payload = {
            "meta": {
                "id": id,
                "type": "tv",
                "name": f"{title} (Part {part_requested})",
                "poster": DEFAULT_POSTER,
                "description": title,
                "videos": [{
                    "id": id,
                    "title": f"{title} [Part {part_requested}]",
                    "season": 1,
                    "episode": part_requested
                }]
            }
        }
        set_cache(cache_key, res_payload, ttl_seconds=300)
        return res_payload

    return {"meta": {"id": id, "type": "tv", "name": "Unknown"}}

@app.get("/stream/tv/{id}.json")
async def stream_tv(id: str, request: Request):
    if not id.startswith("ds_ep_"):
        return {"streams": []}

    cache_key = f"stream:{id}"
    cached = get_cache(cache_key)
    if cached:
        return cached

    raw_id = id.replace("ds_ep_", "")
    target_part = 1
    if "_p" in raw_id:
        raw_id, p_str = raw_id.rsplit("_p", 1)
        try:
            target_part = int(p_str)
        except ValueError:
            target_part = 1

    try:
        page_url = decode_url(raw_id)
    except Exception:
        return {"streams": []}

    base_server_url = get_base_url(request)
    client: httpx.AsyncClient = request.app.state.http
    streams = []

    try:
        html = await fetch_text(client, page_url)
        soup = BeautifulSoup(html, "html.parser")

        server_links = []
        for a in soup.find_all("a", href=True):
            txt = a.text.strip()
            href = normalize_url(a["href"], page_url)
            if not href:
                continue

            if any(p in txt.lower() or p in href.lower() for p in ["jw player", "video.js", "plyr", "shaka", "hls player", "watch now", "showdetails"]):
                s_name = "Server"
                if "jw" in txt.lower(): s_name = "JW Player HD"
                elif "video.js" in txt.lower(): s_name = "Video.js HD"
                elif "plyr" in txt.lower(): s_name = "Plyr HD"
                elif "shaka" in txt.lower(): s_name = "Shaka Player"
                elif "hls" in txt.lower(): s_name = "HLS Stream"
                server_links.append((href, s_name))

        semaphore = asyncio.Semaphore(4)
        async def extract_one(srv_url: str, srv_name: str):
            async with semaphore:
                try:
                    res = await client.get(srv_url, timeout=8.0)
                    if res.status_code >= 400:
                        return []

                    s_soup = BeautifulSoup(res.text, "html.parser")
                    part_urls = []
                    for a_btn in s_soup.find_all(["a", "button"], text=True):
                        if f"part {target_part}" in a_btn.text.lower() or f"part{target_part}" in a_btn.text.lower():
                            h = a_btn.get("href") or a_btn.get("data-url")
                            full_p = normalize_url(h, str(res.url))
                            if full_p:
                                part_urls.append(full_p)

                    target_pages = part_urls if part_urls else [str(res.url)]
                    srv_streams = []

                    for p_url in target_pages:
                        p_res = await client.get(p_url, timeout=8.0) if p_url != str(res.url) else res
                        raw_html = p_res.text.replace("\\/", "/").replace("\\u0026", "&")

                        for m in re.findall(r"""["']([^"']+\.(?:m3u8|mp4|m4v)[^"']*)["']""", raw_html, re.IGNORECASE):
                            full_m = normalize_url(m, str(p_res.url))
                            if full_m and is_allowed_media_url(full_m):
                                enc = encode_url(full_m)
                                is_hls = is_hls_url(full_m)
                                proxy_link = f"{base_server_url}proxy/hls?url={enc}" if is_hls else f"{base_server_url}proxy?url={enc}"
                                srv_streams.append({
                                    "name": "Desi Serials",
                                    "title": f"[{srv_name}] Part {target_part} ({'HLS HD' if is_hls else 'MP4 HD'})",
                                    "url": proxy_link
                                })
                    return srv_streams
                except Exception:
                    return []

        tasks = [extract_one(url, name) for url, name in server_links[:6]]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for r in results:
            if isinstance(r, list):
                streams.extend(r)

    except Exception as e:
        logger.error("Stream extraction error: %s", e)

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

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
