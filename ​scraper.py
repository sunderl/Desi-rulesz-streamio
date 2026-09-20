import os
import re
import json
import base64
import logging
from urllib.parse import urljoin, unquote
import requests
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

TARGET_SITE = "https://www.desiserials.ru"
PUBLIC_DIR = "public"
DEFAULT_POSTER = "https://images.unsplash.com/photo-1593784991095-a205069470b6?w=500&q=80"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer": TARGET_SITE
}

POPULAR_CHANNELS = [
    {"name": "Star Plus", "url": "https://www.desiserials.ru/category/star-plus/", "poster": "https://upload.wikimedia.org/wikipedia/commons/4/43/Star_Plus_logo.png"},
    {"name": "Colors TV", "url": "https://www.desiserials.ru/category/colors-tv/", "poster": "https://upload.wikimedia.org/wikipedia/commons/e/ea/Colors_TV_logo.png"},
    {"name": "Zee TV", "url": "https://www.desiserials.ru/category/zee-tv/", "poster": "https://upload.wikimedia.org/wikipedia/commons/e/eb/Zee_TV_logo.png"},
    {"name": "Sony TV", "url": "https://www.desiserials.ru/category/sony-tv/", "poster": "https://upload.wikimedia.org/wikipedia/commons/5/52/Sony_Entertainment_Television_logo.png"},
    {"name": "SAB TV", "url": "https://www.desiserials.ru/category/sab-tv/", "poster": "https://upload.wikimedia.org/wikipedia/commons/e/e0/Sony_SAB_logo.png"},
    {"name": "&TV", "url": "https://www.desiserials.ru/category/and-tv/", "poster": "https://upload.wikimedia.org/wikipedia/commons/1/1b/And_TV_logo.png"}
]

MANIFEST = {
    "id": "org.desiserials.github.addon",
    "version": "6.0.0",
    "name": "Desi Serials HD (Serverless)",
    "description": "Watch daily Hindi TV Serial episodes split into Parts & Servers directly on Stremio / Novio.",
    "resources": ["catalog", "meta", "stream"],
    "types": ["tv"],
    "catalogs": [
        {"type": "tv", "id": "desiserials_latest", "name": "Latest Daily Episodes"},
        {"type": "tv", "id": "desiserials_channels", "name": "TV Channels"}
    ],
    "idPrefixes": ["ds_"]
}

def encode_id(url: str) -> str:
    return base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")

def decode_id(encoded_str: str) -> str:
    padding = "=" * (-len(encoded_str) % 4)
    return base64.urlsafe_b64decode(encoded_str + padding).decode()

def save_json(filepath: str, data: dict):
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def extract_media_urls(html: str, base_url: str) -> list:
    html = html.replace("\\/", "/").replace("\\u0026", "&")
    found = set()
    for m in re.findall(r"""["']([^"']+\.(?:m3u8|mp4|m4v)[^"']*)["']""", html, re.IGNORECASE):
        full = urljoin(base_url, unquote(m))
        found.add(full)
    
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(["video", "source", "iframe"]):
        src = tag.get("src") or tag.get("data-src")
        if src:
            full = urljoin(base_url, unquote(src))
            if any(ext in full.lower() for ext in [".m3u8", ".mp4", "showdetails.org", "vk.com"]):
                found.add(full)
    return list(found)

def parse_episodes_from_page(page_url: str) -> list:
    episodes = []
    try:
        res = requests.get(page_url, headers=HEADERS, timeout=12)
        if res.status_code != 200:
            return episodes
        soup = BeautifulSoup(res.text, "html.parser")

        seen = set()
        for a in soup.find_all("a", href=True):
            href = urljoin(page_url, a["href"])
            title = a.text.strip()
            
            if href in seen or len(title) < 6:
                continue
            
            if any(k in href.lower() for k in ["episode", "watch-online", "-full-episode"]):
                seen.add(href)
                img = a.find("img") or (a.parent.find("img") if a.parent else None)
                poster = DEFAULT_POSTER
                if img:
                    src = img.get("src") or img.get("data-src") or img.get("data-lazy-src")
                    if src:
                        poster = urljoin(page_url, src)

                episodes.append({
                    "url": href,
                    "title": title,
                    "poster": poster,
                    "id": f"ds_ep_{encode_id(href)}"
                })
    except Exception as e:
        logging.error(f"Error fetching page {page_url}: {e}")
    return episodes

def process_episode_streams(ep_url: str, ep_id: str):
    try:
        res = requests.get(ep_url, headers=HEADERS, timeout=12)
        if res.status_code != 200:
            return
        soup = BeautifulSoup(res.text, "html.parser")

        # Find all player server links on page (JW Player, Video.js, Plyr, Shaka, HLS Player)
        server_links = []
        for a in soup.find_all("a", href=True):
            txt = a.text.strip()
            href = urljoin(ep_url, a["href"])
            if any(p in txt.lower() or p in href.lower() for p in ["jw player", "video.js", "plyr", "shaka", "hls player", "showdetails"]):
                s_name = "Server"
                if "jw" in txt.lower(): s_name = "JW Player"
                elif "video.js" in txt.lower(): s_name = "Video.js"
                elif "plyr" in txt.lower(): s_name = "Plyr"
                elif "shaka" in txt.lower(): s_name = "Shaka"
                elif "hls" in txt.lower(): s_name = "HLS Player"
                server_links.append((href, s_name))

        # Build part-wise streams (Part 1, Part 2, Part 3)
        for part_num in (1, 2, 3):
            part_ep_id = f"{ep_id}_p{part_num}"
            streams = []

            for srv_url, srv_name in server_links:
                try:
                    s_res = requests.get(srv_url, headers=HEADERS, timeout=10)
                    if s_res.status_code != 200:
                        continue
                    
                    s_soup = BeautifulSoup(s_res.text, "html.parser")
                    part_urls = []
                    for p_btn in s_soup.find_all(["a", "button"], text=True):
                        if f"part {part_num}" in p_btn.text.lower() or f"part{part_num}" in p_btn.text.lower():
                            p_href = p_btn.get("href") or p_btn.get("data-url")
                            if p_href:
                                part_urls.append(urljoin(str(s_res.url), p_href))

                    targets = part_urls if part_urls else [str(s_res.url)]
                    for t_url in targets:
                        t_res = requests.get(t_url, headers=HEADERS, timeout=8) if t_url != str(s_res.url) else s_res
                        media_links = extract_media_urls(t_res.text, str(t_res.url))
                        
                        for med in media_links:
                            stream_type = "HLS HD" if ".m3u8" in med.lower() else "MP4 HD"
                            streams.append({
                                "name": "Desi Serials",
                                "title": f"[{srv_name}] Part {part_num} ({stream_type})",
                                "url": med
                            })
                except Exception as ex:
                    logging.warning(f"Error scraping server {srv_url}: {ex}")

            # Save stream endpoint static JSON
            save_json(
                os.path.join(PUBLIC_DIR, "stream", "tv", f"{part_ep_id}.json"),
                {"streams": streams}
            )

    except Exception as e:
        logging.error(f"Error processing episode streams {ep_url}: {e}")

def build_addon():
    logging.info("Starting Stremio Static Addon Generation...")
    
    # 1. Manifest JSON
    save_json(os.path.join(PUBLIC_DIR, "manifest.json"), MANIFEST)

    # 2. Channels Catalog
    channel_metas = []
    for ch in POPULAR_CHANNELS:
        ch_id = f"ds_cat_{encode_id(ch['url'])}"
        channel_metas.append({
            "id": ch_id,
            "type": "tv",
            "name": ch["name"],
            "poster": ch["poster"],
            "description": f"Daily Serials on {ch['name']}"
        })
    save_json(
        os.path.join(PUBLIC_DIR, "catalog", "tv", "desiserials_channels.json"),
        {"metas": channel_metas}
    )

    # 3. Latest Episodes Catalog
    latest_episodes = parse_episodes_from_page(TARGET_SITE)
    latest_metas = []
    for ep in latest_episodes[:40]:
        latest_metas.append({
            "id": ep["id"],
            "type": "tv",
            "name": ep["title"],
            "poster": ep["poster"],
            "description": ep["title"]
        })
    save_json(
        os.path.join(PUBLIC_DIR, "catalog", "tv", "desiserials_latest.json"),
        {"metas": latest_metas}
    )

    # 4. Generate Channel Meta & Episode Lists
    for ch in POPULAR_CHANNELS:
        ch_id = f"ds_cat_{encode_id(ch['url'])}"
        episodes = parse_episodes_from_page(ch["url"])
        videos = []
        
        ep_idx = 1
        for ep in episodes[:15]:
            # Generate Metadata and Streams for Episode Parts
            process_episode_streams(ep["url"], ep["id"])

            for p_num in (1, 2, 3):
                videos.append({
                    "id": f"{ep['id']}_p{p_num}",
                    "title": f"{ep['title']} [Part {p_num}]",
                    "season": 1,
                    "episode": ep_idx,
                })
                ep_idx += 1

        save_json(
            os.path.join(PUBLIC_DIR, "meta", "tv", f"{ch_id}.json"),
            {
                "meta": {
                    "id": ch_id,
                    "type": "tv",
                    "name": ch["name"],
                    "poster": ch["poster"],
                    "description": f"Daily Full Episodes divided into Parts for {ch['name']}",
                    "videos": videos
                }
            }
        )

    # 5. Build Individual Episode Meta Files
    for ep in latest_episodes[:20]:
        process_episode_streams(ep["url"], ep["id"])
        for p_num in (1, 2, 3):
            part_id = f"{ep['id']}_p{p_num}"
            save_json(
                os.path.join(PUBLIC_DIR, "meta", "tv", f"{part_id}.json"),
                {
                    "meta": {
                        "id": part_id,
                        "type": "tv",
                        "name": f"{ep['title']} (Part {p_num})",
                        "poster": ep["poster"],
                        "description": ep["title"],
                        "videos": [{
                            "id": part_id,
                            "title": f"{ep['title']} [Part {p_num}]",
                            "season": 1,
                            "episode": p_num
                        }]
                    }
                }
            )

    logging.info("Build completed successfully!")

if __name__ == "__main__":
    build_addon()
