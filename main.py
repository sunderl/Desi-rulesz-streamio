import os
import re
import time
import base64
import logging
import asyncio
from urllib.parse import urljoin, urlparse, unquote
from fastapi import FastAPI, Request, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
import httpx
from bs4 import BeautifulSoup

# Logging Setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("desirulez")

app = FastAPI()

# 1. CORS CONFIGURATION
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
TARGET_SITE = "https://desiruleztv.net"
DEFAULT_POSTER = "https://images.unsplash.com/photo-1593784991095-a205069470b6?w=500&q=80"

# SSRF Protection Host Allowlist
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
    "version": "4.0.0",
    "name": "DesiRulez TV (Production)",
    "description": "Watch Indian Serials via fast parallel extraction, memory-safe streaming, and HLS proxying.",
    "resources": ["catalog", "meta", "stream"],
    "types": ["tv"],
    "catalogs": [
        {
            "type": "tv",
            "id": "desirulez_popular",
            "name": "Top Serials",
            "extra": [{"name": "search", "isRequired": False}]
        },
        {
            "type": "tv",
            "id": "desirulez_latest",
            "name": "Latest Daily Episodes",
            "extra": [{"name": "search", "isRequired": False}]
        }
    ],
    "idPrefixes": ["dr_"]
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": TARGET_SITE
}

# Simple In-Memory Cache TTL Store
CACHE_STORE: dict[str, tuple[float, dict]] = {}

def get_cache(key: str) -> dict | None:
    item = CACHE_STORE.get(key)
    if not item:
        return None
    expires_at, data = item
    if time.time() >= expires_at:
        CACHE_STORE.pop(key, None)
        return None
    return data

def set_cache(key: str, data: dict, ttl_seconds: int = 300) -> None:
    CACHE_STORE[key] = (time.time() + ttl_seconds, data)

def get_base_url(request: Request) -> str:
    if PUBLIC_BASE_URL:
        return PUBLIC_BASE_URL + "/"
    return str(request.base_url)

def encode_url(url: str) -> str:
    return base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")

def decode_url(encoded_str: str) -> str:
    padding = "=" * (-len(encoded_str) % 4)
    return base64.urlsafe_b64decode(encoded_str + padding).decode()

def validate_target_url(target_url: str) -> None:
    parsed = urlparse(target_url)
    if parsed.scheme not in {"http", "https"}:
        raise HTTPException(status_code=400, detail="Invalid URL scheme")
    hostname = (parsed.hostname or "").lower()
    if not any(hostname == host or hostname.endswith("." + host) for host in ALLOWED_MEDIA_HOSTS):
        raise HTTPException(status_code=403, detail="Media host is not in allowed domain list")

def normalize_url(raw_url: str, base_url: str) -> str | None:
    if not raw_url:
        return None
    value = raw_url.strip().replace("\\/", "/").replace("\\u0026", "&")
    value = unquote(value)
    if value.startswith("//"):
        value = "https:" + value
    return urljoin(base_url, value)

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

# 2. MEMORY-SAFE MP4 & SEGMENT STREAMING PROXY (WITH RANGE SUPPORT)
@app.get("/proxy")
async def proxy_stream(
    url: str,
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
    }
    if range_header:
        request_headers["Range"] = range_header

    client = httpx.AsyncClient(
        follow_redirects=True,
        timeout=httpx.Timeout(30.0, connect=10.0),
    )

    try:
        request = client.build_request("GET", target_url, headers=request_headers)
        response = await client.send(request, stream=True)

        if response.status_code >= 400:
            await response.aclose()
            await client.aclose()
            raise HTTPException(
                status_code=502,
                detail=f"Upstream media server returned HTTP {response.status_code}",
            )

        response_headers = {}
        for header in (
            "content-type",
            "content-length",
            "content-range",
            "accept-ranges",
            "cache-control",
            "etag",
            "last-modified",
        ):
            if header in response.headers:
                response_headers[header] = response.headers[header]

        response_headers["Access-Control-Allow-Origin"] = "*"

        async def iterator():
            try:
                async for chunk in response.aiter_bytes():
                    yield chunk
            finally:
                await response.aclose()
                await client.aclose()

        return StreamingResponse(
            iterator(),
            status_code=response.status_code,
            headers=response_headers,
            media_type=response.headers.get("content-type"),
        )
    except Exception as exc:
        await client.aclose()
        logger.error("Proxy failure for %s: %s", target_url, exc)
        raise HTTPException(status_code=502, detail=str(exc))

# 3. RECURSIVE HLS PLAYLIST REWRITER PROXY
@app.get("/proxy/hls")
async def proxy_hls(url: str, request: Request):
    try:
        playlist_url = decode_url(url)
    except Exception:
        raise HTTPException(status_code=400, detail="Malformed base64 URL")

    validate_target_url(playlist_url)

    try:
        async with httpx.AsyncClient(
            headers=HEADERS,
            follow_redirects=True,
            timeout=httpx.Timeout(15.0, connect=8.0),
        ) as client:
            response = await client.get(playlist_url)

        if response.status_code != 200:
            raise HTTPException(
                status_code=502,
                detail=f"Unable to load upstream HLS playlist: HTTP {response.status_code}",
            )

        public_base = get_base_url(request).rstrip("/")
        output_lines = []

        for line in response.text.splitlines():
            line_str = line.strip()

            if not line_str:
                output_lines.append("")
                continue

            if line_str.startswith("#"):
                # Rewrite URI attributes (AES-128 keys, init maps) supporting single & double quotes
                def replace_uri(match):
                    quote = match.group(1)
                    raw_uri = match.group(2)
                    full_key_url = normalize_url(raw_uri, str(response.url))
                    if not full_key_url:
                        return match.group(0)
                    encoded_key = encode_url(full_key_url)
                    return f'URI={quote}{public_base}/proxy?url={encoded_key}{quote}'

                line_str = re.sub(
                    r'URI\s*=\s*(["\'])(.*?)\1',
                    replace_uri,
                    line_str,
                    flags=re.IGNORECASE,
                )
                output_lines.append(line_str)
                continue

            # Segment or sub-playlist URI rewriting
            segment_url = normalize_url(line_str, str(response.url))
            if segment_url:
                encoded_segment = encode_url(segment_url)
                if ".m3u8" in line_str.lower() or "m3u8" in segment_url.lower():
                    output_lines.append(f"{public_base}/proxy/hls?url={encoded_segment}")
                else:
                    output_lines.append(f"{public_base}/proxy?url={encoded_segment}")
            else:
                output_lines.append(line_str)

        return Response(
            content="\n".join(output_lines),
            media_type="application/vnd.apple.mpegurl",
            headers={
                "Cache-Control": "no-cache",
                "Access-Control-Allow-Origin": "*",
            },
        )

    except httpx.HTTPError as exc:
        logger.error("HLS Playlist Proxy error: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc))

@app.get("/")
def home():
    return {"status": "DesiRulez Addon Active!", "manifest": "/manifest.json"}

@app.get("/manifest.json")
def manifest():
    return MANIFEST

# 4. CATALOGS & SEARCH
@app.get("/catalog/tv/{catalog_id}.json")
@app.get("/catalog/tv/{catalog_id}/search={query}.json")
async def catalog(catalog_id: str, query: str = None):
    cache_key = f"catalog:{catalog_id}:{query or 'all'}"
    cached = get_cache(cache_key)
    if cached:
        return cached

    metas = []
    seen_urls = set()

    if catalog_id == "desirulez_popular" and not query:
        for show in POPULAR_SERIALS:
            cat_id = f"dr_cat_{encode_url(show['url'])}"
            metas.append({
                "id": cat_id,
                "type": "tv",
                "name": show["name"],
                "poster": show["poster"],
                "description": f"Date-wise episodes for {show['name']}"
            })
        res_payload = {"metas": metas}
        set_cache(cache_key, res_payload, ttl_seconds=600)
        return res_payload

    target_fetch_url = f"{TARGET_SITE}/?s={query.replace(' ', '+')}" if query else TARGET_SITE

    try:
        async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=12.0) as client:
            res = await client.get(target_fetch_url)
            soup = BeautifulSoup(res.text, "html.parser")
            
            for a in soup.find_all("a", href=True):
                full_url = normalize_url(a["href"], TARGET_SITE)
                title = a.text.strip()
                
                if not full_url or full_url in seen_urls:
                    continue
                
                if any(kw in full_url.lower() for kw in ["/episode", "/watch-online", "-episode-"]) and len(title) > 8:
                    seen_urls.add(full_url)
                    ep_id = f"dr_ep_{encode_url(full_url)}"
                    
                    img_tag = a.find("img") or (a.parent.find("img") if a.parent else None)
                    poster = DEFAULT_POSTER
                    if img_tag:
                        src = img_tag.get("src") or img_tag.get("data-src")
                        normalized_img = normalize_url(src, TARGET_SITE)
                        if normalized_img:
                            poster = normalized_img
                    
                    metas.append({
                        "id": ep_id,
                        "type": "tv",
                        "name": title,
                        "poster": poster,
                        "description": f"Episode: {title}"
                    })
    except Exception as e:
        logger.error("Catalog Error: %s", e)

    res_payload = {"metas": metas[:40]}
    set_cache(cache_key, res_payload, ttl_seconds=300)
    return res_payload

# 5. METADATA ENDPOINTS
@app.get("/meta/tv/{id}.json")
async def meta(id: str):
    cache_key = f"meta:{id}"
    cached = get_cache(cache_key)
    if cached:
        return cached

    if id.startswith("dr_cat_"):
        encoded_url = id.replace("dr_cat_", "")
        try:
            category_url = decode_url(encoded_url)
        except Exception:
            category_url = TARGET_SITE

        videos = []
        seen_urls = set()
        
        show_name = "Indian Serial"
        show_poster = DEFAULT_POSTER
        for show in POPULAR_SERIALS:
            if show["url"] == category_url:
                show_name = show["name"]
                show_poster = show["poster"]
                break

        try:
            async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=12.0) as client:
                res = await client.get(category_url)
                soup = BeautifulSoup(res.text, "html.parser")
                
                ep_idx = 1
                for a in soup.find_all("a", href=True):
                    full_url = normalize_url(a["href"], TARGET_SITE)
                    title = a.text.strip()
                    
                    if not full_url or full_url in seen_urls:
                        continue
                        
                    if any(kw in full_url.lower() for kw in ["/episode", "/watch-online", "-episode-"]) and len(title) > 8:
                        seen_urls.add(full_url)
                        ep_id = f"dr_ep_{encode_url(full_url)}"
                        
                        released_date = parse_date_from_title(title)
                        video_obj = {
                            "id": ep_id,
                            "title": title,
                            "season": 1,
                            "episode": ep_idx,
                        }
                        if released_date:
                            video_obj["released"] = released_date
                            
                        videos.append(video_obj)
                        ep_idx += 1
        except Exception as e:
            logger.error("Meta Category Fetch Error: %s", e)

        res_payload = {
            "meta": {
                "id": id,
                "type": "tv",
                "name": show_name,
                "poster": show_poster,
                "description": f"All date-wise episodes for {show_name}",
                "videos": videos[:60]
            }
        }
        set_cache(cache_key, res_payload, ttl_seconds=300)
        return res_payload

    elif id.startswith("dr_ep_"):
        encoded_url = id.replace("dr_ep_", "")
        try:
            ep_url = decode_url(encoded_url)
        except Exception:
            ep_url = TARGET_SITE

        title = "Serial Episode"
        released_date = None

        try:
            async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=10.0) as client:
                res = await client.get(ep_url)
                soup = BeautifulSoup(res.text, "html.parser")
                h1 = soup.find("h1")
                if h1:
                    title = h1.text.strip()
                    released_date = parse_date_from_title(title)
        except Exception:
            pass

        video_item = {
            "id": id,
            "title": title,
            "season": 1,
            "episode": 1
        }
        if released_date:
            video_item["released"] = released_date

        res_payload = {
            "meta": {
                "id": id,
                "type": "tv",
                "name": title,
                "poster": DEFAULT_POSTER,
                "description": f"Watch {title} in HD Direct Stream",
                "videos": [video_item]
            }
        }
        set_cache(cache_key, res_payload, ttl_seconds=300)
        return res_payload

    return {"meta": {"id": id, "type": "tv", "name": "Unknown"}}

# 6. PARALLEL & SPECIFIC MEDIA EXTRACTION
async def extract_direct_media(
    embed_url: str,
    client: httpx.AsyncClient,
    base_url: str,
    referer: str,
) -> list[dict]:
    found_streams = []
    request_headers = {
        "User-Agent": HEADERS["User-Agent"],
        "Referer": referer,
        "Origin": TARGET_SITE,
    }

    try:
        res = await client.get(embed_url, headers=request_headers, timeout=8.0)
        if res.status_code >= 400:
            return []

        html = res.text.replace("\\/", "/").replace("\\u0026", "&")

        # Robust regex matching for .m3u8 playlists
        m3u8_matches = re.findall(
            r'https?://[^"\'\s<>]+?\.m3u8(?:\?[^"\'\s<>]*)?',
            html,
            flags=re.IGNORECASE,
        )
        for link in m3u8_matches:
            normalized = normalize_url(link, str(res.url))
            if normalized:
                proxy_link = f"{base_url}proxy/hls?url={encode_url(normalized)}"
                if proxy_link not in [s["url"] for s in found_streams]:
                    found_streams.append({
                        "name": "DesiRulez",
                        "title": "HLS Stream",
                        "url": proxy_link,
                        "behaviorHints": {
                            "bingeGroup": "desirulez-hls"
                        }
                    })

        # Robust regex matching for .mp4 media
        mp4_matches = re.findall(
            r'https?://[^"\'\s<>]+?\.mp4(?:\?[^"\'\s<>]*)?',
            html,
            flags=re.IGNORECASE,
        )
        for link in mp4_matches:
            normalized = normalize_url(link, str(res.url))
            if normalized:
                proxy_link = f"{base_url}proxy?url={encode_url(normalized)}"
                if proxy_link not in [s["url"] for s in found_streams]:
                    found_streams.append({
                        "name": "DesiRulez",
                        "title": "MP4 Stream",
                        "url": proxy_link,
                        "behaviorHints": {
                            "notWebReady": False
                        }
                    })

    except Exception as e:
        logger.warning("Deep extract error for %s: %s", embed_url, e)

    return found_streams

# 7. PARALLEL BOUNDED STREAM DISCOVERY
@app.get("/stream/tv/{id}.json")
async def stream(id: str, request: Request):
    if not id.startswith("dr_ep_"):
        return {"streams": []}

    cached = get_cache(f"stream:{id}")
    if cached:
        return cached

    encoded_url = id.replace("dr_ep_", "")
    try:
        page_url = decode_url(encoded_url)
    except Exception as e:
        logger.error("URL Decode error: %s", e)
        return {"streams": []}

    base_server_url = get_base_url(request)
    streams = []

    try:
        async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=12.0) as client:
            res = await client.get(page_url)
            soup = BeautifulSoup(res.text, "html.parser")

            # Extract iframe sources without restrictive hardcoded domain filters
            iframe_urls = []
            for iframe in soup.find_all("iframe", src=True):
                src = normalize_url(iframe["src"].strip(), page_url)
                if src and src not in iframe_urls:
                    iframe_urls.append(src)

            # Bounded async concurrency (max 4 parallel provider requests)
            semaphore = asyncio.Semaphore(4)

            async def extract_one(src_url: str):
                async with semaphore:
                    return await extract_direct_media(
                        embed_url=src_url,
                        client=client,
                        base_url=base_server_url,
                        referer=page_url,
                    )

            tasks = [extract_one(src) for src in iframe_urls]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for result in results:
                if isinstance(result, Exception):
                    logger.error("Provider extraction task failed: %s", result)
                    continue
                streams.extend(result)

            # Inspect primary page scripts directly
            page_direct_streams = await extract_direct_media(
                embed_url=page_url,
                client=client,
                base_url=base_server_url,
                referer=TARGET_SITE,
            )
            streams.extend(page_direct_streams)

    except Exception as e:
        logger.error("Stream Scraping Error for %s: %s", page_url, e)

    # Deduplicate streams
    unique_streams = []
    seen_urls = set()
    for s in streams:
        u = s.get("url")
        if u and u not in seen_urls:
            seen_urls.add(u)
            unique_streams.append(s)

    res_payload = {"streams": unique_streams}
    if unique_streams:
        set_cache(f"stream:{id}", res_payload, ttl_seconds=120)

    return res_payload
