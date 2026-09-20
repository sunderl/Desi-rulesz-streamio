import os
import base64
import re
from urllib.parse import urljoin
from fastapi import FastAPI, Request, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
import httpx
from bs4 import BeautifulSoup

app = FastAPI()

# 1. FIXED CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

TARGET_SITE = "https://desiruleztv.net"
DEFAULT_POSTER = "https://images.unsplash.com/photo-1593784991095-a205069470b6?w=500&q=80"

POPULAR_SERIALS = [
    {"name": "Anupamaa", "url": "https://desiruleztv.net/category/anupama/", "poster": "https://upload.wikimedia.org/wikipedia/en/8/80/Anupamaa_TV_Series.jpg"},
    {"name": "Yeh Rishta Kya Kehlata Hai", "url": "https://desiruleztv.net/category/yeh-rishta-kya-kehlata-hai/", "poster": "https://upload.wikimedia.org/wikipedia/en/b/b8/Yeh_Rishta_Kya_Kehlata_Hai_logo.jpg"},
    {"name": "Taarak Mehta Ka Ooltah Chashmah", "url": "https://desiruleztv.net/category/taarak-mehta-ka-ooltah-chashmah/", "poster": "https://upload.wikimedia.org/wikipedia/en/8/86/Taarak_Mehta_Ka_Ooltah_Chashmah_logo.jpg"},
]

MANIFEST = {
    "id": "org.desiruleztv.proxy.addon",
    "version": "3.2.0",
    "name": "DesiRulez TV (HLS & Range Fixed)",
    "description": "Watch Indian Serials without VPN with proper HLS segment proxying and MP4 seeking support",
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

def get_base_url(request: Request) -> str:
    env_url = os.getenv("PUBLIC_BASE_URL")
    if env_url:
        return env_url.rstrip("/") + "/"
    return str(request.base_url)

def encode_url(url: str) -> str:
    return base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")

def decode_url(encoded_str: str) -> str:
    padding = "=" * (-len(encoded_str) % 4)
    return base64.urlsafe_b64decode(encoded_str + padding).decode()

def parse_date_from_title(title: str) -> str:
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

# 1. RANGE-AWARE MEDIA PROXY (FOR MP4, TS SEGMENTS, KEYS)
@app.get("/proxy")
async def proxy_stream(
    url: str,
    range_header: str | None = Header(default=None, alias="Range"),
):
    try:
        target_url = decode_url(url)

        if not target_url.startswith(("http://", "https://")):
            raise HTTPException(status_code=400, detail="Invalid media URL")

        req_headers = {
            "User-Agent": HEADERS["User-Agent"],
            "Referer": TARGET_SITE,
        }

        if range_header:
            req_headers["Range"] = range_header

        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(30.0, connect=10.0),
        ) as client:
            response = await client.get(target_url, headers=req_headers)

        res_headers = {}
        for h in (
            "content-type",
            "content-length",
            "content-range",
            "accept-ranges",
            "cache-control",
            "etag",
            "last-modified",
        ):
            if h in response.headers:
                res_headers[h] = response.headers[h]

        res_headers["Access-Control-Allow-Origin"] = "*"

        return Response(
            content=response.content,
            status_code=response.status_code,
            headers=res_headers,
            media_type=response.headers.get("content-type"),
        )

    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

# 2. HLS PLAYLIST REWRITER PROXY (.m3u8)
@app.get("/proxy/hls")
async def proxy_hls(url: str, request: Request):
    try:
        playlist_url = decode_url(url)

        async with httpx.AsyncClient(
            headers=HEADERS,
            follow_redirects=True,
            timeout=httpx.Timeout(15.0, connect=8.0),
        ) as client:
            response = await client.get(playlist_url)

        if response.status_code != 200:
            raise HTTPException(
                status_code=response.status_code,
                detail="Unable to load HLS playlist",
            )

        public_base = get_base_url(request).rstrip("/")
        output_lines = []

        for line in response.text.splitlines():
            line_str = line.strip()

            if not line_str:
                output_lines.append("")
                continue

            if line_str.startswith("#"):
                # Handle AES-128 Encryption Key or Map URI rewriting
                if "URI=" in line_str:
                    def replace_uri(match):
                        raw_uri = match.group(1)
                        full_key_url = urljoin(str(response.url), raw_uri)
                        encoded_key = encode_url(full_key_url)
                        return f'URI="{public_base}/proxy?url={encoded_key}"'
                    
                    line_str = re.sub(r'URI="([^"]+)"', replace_uri, line_str)
                output_lines.append(line_str)
                continue

            # Segment or Sub-playlist URL rewriting
            segment_url = urljoin(str(response.url), line_str)
            encoded_segment = encode_url(segment_url)

            if ".m3u8" in line_str.lower() or "m3u8" in segment_url.lower():
                output_lines.append(f"{public_base}/proxy/hls?url={encoded_segment}")
            else:
                output_lines.append(f"{public_base}/proxy?url={encoded_segment}")

        return Response(
            content="\n".join(output_lines),
            media_type="application/vnd.apple.mpegurl",
            headers={
                "Cache-Control": "no-cache",
                "Access-Control-Allow-Origin": "*",
            },
        )

    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

@app.get("/")
def home():
    return {"status": "DesiRulez Addon Active!", "manifest": "/manifest.json"}

@app.get("/manifest.json")
def manifest():
    return MANIFEST

# CATALOGS & SEARCH
@app.get("/catalog/tv/{catalog_id}.json")
@app.get("/catalog/tv/{catalog_id}/search={query}.json")
async def catalog(catalog_id: str, query: str = None):
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
        return {"metas": metas}

    target_fetch_url = f"{TARGET_SITE}/?s={query.replace(' ', '+')}" if query else TARGET_SITE

    try:
        async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=12.0) as client:
            res = await client.get(target_fetch_url)
            soup = BeautifulSoup(res.text, "html.parser")
            
            for a in soup.find_all("a", href=True):
                full_url = urljoin(TARGET_SITE, a["href"]).strip()
                title = a.text.strip()
                
                if full_url in seen_urls:
                    continue
                
                if any(kw in full_url.lower() for kw in ["/episode", "/watch-online", "-episode-"]) and len(title) > 8:
                    seen_urls.add(full_url)
                    ep_id = f"dr_ep_{encode_url(full_url)}"
                    
                    img_tag = a.find("img") or (a.parent.find("img") if a.parent else None)
                    poster = DEFAULT_POSTER
                    if img_tag:
                        src = img_tag.get("src") or img_tag.get("data-src")
                        if src:
                            poster = urljoin(TARGET_SITE, src)
                    
                    metas.append({
                        "id": ep_id,
                        "type": "tv",
                        "name": title,
                        "poster": poster,
                        "description": f"Episode: {title}"
                    })
    except Exception as e:
        print(f"Catalog Error: {e}")

    return {"metas": metas[:40]}

# METADATA
@app.get("/meta/tv/{id}.json")
async def meta(id: str):
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
                    full_url = urljoin(TARGET_SITE, a["href"]).strip()
                    title = a.text.strip()
                    
                    if full_url in seen_urls:
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
            print(f"Meta Category Fetch Error: {e}")

        return {
            "meta": {
                "id": id,
                "type": "tv",
                "name": show_name,
                "poster": show_poster,
                "description": f"All date-wise episodes for {show_name}",
                "videos": videos[:60]
            }
        }

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

        return {
            "meta": {
                "id": id,
                "type": "tv",
                "name": title,
                "poster": DEFAULT_POSTER,
                "description": f"Watch {title} in HD Direct Stream",
                "videos": [video_item]
            }
        }

    return {"meta": {"id": id, "type": "tv", "name": "Unknown"}}

# DEEP MEDIA EXTRACTION
async def extract_direct_media(embed_url: str, client: httpx.AsyncClient, base_url: str) -> list:
    found_streams = []
    try:
        res = await client.get(embed_url, timeout=8.0)
        html = res.text

        # 1. Direct .m3u8 URLs -> Route via HLS Proxy
        m3u8_links = re.findall(r'["\'](https?://[^"\']+\.m3u8[^"\']*)["\']', html)
        for link in m3u8_links:
            clean_link = link.replace("\\/", "/")
            proxy_link = f"{base_url}proxy/hls?url={encode_url(clean_link)}"
            if proxy_link not in [s["url"] for s in found_streams]:
                found_streams.append({
                    "name": "⚡ No-VPN HLS Proxy",
                    "title": "Proxied Direct HLS Stream (.m3u8)",
                    "url": proxy_link
                })

        # 2. Direct .mp4 URLs -> Route via Range Proxy
        mp4_links = re.findall(r'["\'](https?://[^"\']+\.mp4[^"\']*)["\']', html)
        for link in mp4_links:
            clean_link = link.replace("\\/", "/")
            proxy_link = f"{base_url}proxy?url={encode_url(clean_link)}"
            if proxy_link not in [s["url"] for s in found_streams]:
                found_streams.append({
                    "name": "⚡ No-VPN MP4 Proxy",
                    "title": "Proxied Direct MP4 Stream",
                    "url": proxy_link
                })

        # 3. VK Stream Links -> Route via Range Proxy
        vk_urls = re.findall(r'"url(?:720|1080|480|360)"\s*:\s*"([^"]+)"', html)
        for v_url in vk_urls:
            clean_link = v_url.replace("\\/", "/")
            proxy_link = f"{base_url}proxy?url={encode_url(clean_link)}"
            if proxy_link not in [s["url"] for s in found_streams]:
                found_streams.append({
                    "name": "⚡ No-VPN VK Proxy",
                    "title": "Proxied VK HD Stream (720p)",
                    "url": proxy_link
                })

    except Exception as e:
        print(f"Deep extract error for {embed_url}: {e}")

    return found_streams

@app.get("/stream/tv/{id}.json")
async def stream(id: str, request: Request):
    streams = []
    
    if not id.startswith("dr_ep_"):
        return {"streams": []}

    encoded_url = id.replace("dr_ep_", "")
    try:
        page_url = decode_url(encoded_url)
    except Exception as e:
        print(f"URL Decode error: {e}")
        return {"streams": []}

    base_server_url = get_base_url(request)

    try:
        async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=12.0) as client:
            res = await client.get(page_url)
            soup = BeautifulSoup(res.text, "html.parser")

            # Search IFrames
            iframes = soup.find_all("iframe", src=True)
            for iframe in iframes:
                src = urljoin(page_url, iframe["src"].strip())

                if any(srv in src for srv in ["vk.com", "vkprime", "streamwish", "filelions", "dood", "vidoza", "streamtape"]):
                    direct_streams = await extract_direct_media(src, client, base_server_url)
                    streams.extend(direct_streams)

            # Search Page Scripts
            page_direct_streams = await extract_direct_media(page_url, client, base_server_url)
            for ds in page_direct_streams:
                if ds["url"] not in [s["url"] for s in streams]:
                    streams.append(ds)

    except Exception as e:
        print(f"Stream Scraping Error: {e}")

    # Deduplicate streams
    unique_streams = []
    seen_stream_urls = set()
    for s in streams:
        u = s.get("url")
        if u and u not in seen_stream_urls:
            seen_stream_urls.add(u)
            unique_streams.append(s)

    return {"streams": unique_streams}
