from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import httpx
from bs4 import BeautifulSoup
import re

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

TARGET_SITE = "https://desiruleztv.net"

# Dedicated Top Serials Categories
POPULAR_SERIALS = [
    {"id": "dr_cat_anupama", "name": "Anupamaa", "url": "https://desiruleztv.net/category/anupama/"},
    {"id": "dr_cat_yrkkh", "name": "Yeh Rishta Kya Kehlata Hai", "url": "https://desiruleztv.net/category/yeh-rishta-kya-kehlata-hai/"},
    {"id": "dr_cat_tmkoc", "name": "Taarak Mehta Ka Ooltah Chashmah", "url": "https://desiruleztv.net/category/taarak-mehta-ka-ooltah-chashmah/"},
]

MANIFEST = {
    "id": "org.desiruleztv.vk.addon",
    "version": "2.1.0",
    "name": "DesiRulez TV (VK HD Priority)",
    "description": "Watch All Serials with Date-wise Episode Catalog & VK Direct Stream",
    "resources": ["catalog", "meta", "stream"],
    "types": ["tv"],
    "catalogs": [
        {
            "type": "tv",
            "id": "desirulez_popular",
            "name": "Top Serials (Anupama, YRKKH, TMKOC)",
            "extra": [{"name": "search", "isRequired": False}]
        },
        {
            "type": "tv",
            "id": "desirulez_latest",
            "name": "All Latest Daily Episodes",
            "extra": [{"name": "search", "isRequired": False}]
        }
    ],
    "idPrefixes": ["dr_"]
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

@app.get("/manifest.json")
def manifest():
    return MANIFEST

# 1. CATALOGS & SEARCH
@app.get("/catalog/tv/{catalog_id}.json")
@app.get("/catalog/tv/{catalog_id}/search={query}.json")
async def catalog(catalog_id: str, query: str = None):
    metas = []
    
    # Search functionality
    if query:
        search_url = f"{TARGET_SITE}/?s={query.replace(' ', '+')}"
        async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=12.0) as client:
            try:
                res = await client.get(search_url)
                soup = BeautifulSoup(res.text, "html.parser")
                for a in soup.find_all("a", href=True):
                    href = a["href"]
                    text = a.text.strip()
                    if ("episode" in href.lower() or "watch" in href.lower()) and len(text) > 8:
                        clean_id = "dr_ep_" + re.sub(r'[^a-zA-Z0-9]', '_', href)
                        if not any(m["id"] == clean_id for m in metas):
                            metas.append({
                                "id": clean_id,
                                "type": "tv",
                                "name": text,
                                "poster": "https://i.imgur.com/8Q9Z5.png",
                                "description": f"Date-wise Serial: {text}"
                            })
            except Exception as e:
                print("Search Error:", e)
        return {"metas": metas}

    # Popular Top Serials
    if catalog_id == "desirulez_popular":
        for show in POPULAR_SERIALS:
            metas.append({
                "id": show["id"],
                "type": "tv",
                "name": show["name"],
                "poster": "https://i.imgur.com/8Q9Z5.png",
                "description": f"All Date-wise Episodes for {show['name']}"
            })
        return {"metas": metas}

    # All Latest Episodes Home Page
    try:
        async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=12.0) as client:
            res = await client.get(TARGET_SITE)
            soup = BeautifulSoup(res.text, "html.parser")
            for a in soup.find_all("a", href=True):
                href = a["href"]
                text = a.text.strip()
                if "desiruleztv.net" in href and ("episode" in href.lower() or "watch" in href.lower()):
                    clean_id = "dr_ep_" + re.sub(r'[^a-zA-Z0-9]', '_', href)
                    if len(text) > 8 and not any(m["id"] == clean_id for m in metas):
                        metas.append({
                            "id": clean_id,
                            "type": "tv",
                            "name": text,
                            "poster": "https://i.imgur.com/8Q9Z5.png",
                            "description": f"Episode Date: {text}"
                        })
    except Exception as e:
        print("Catalog Error:", e)
        
    return {"metas": metas[:30]}

# 2. DATE-WISE EPISODE CATALOG METADATA
@app.get("/meta/tv/{id}.json")
async def meta(id: str):
    videos = []
    show_name = "Indian Serial Episode"
    
    # Matching Serial Category for Episode List
    target_url = TARGET_SITE
    for show in POPULAR_SERIALS:
        if show["id"] == id:
            target_url = show["url"]
            show_name = show["name"]
            break
            
    if id.startswith("dr_ep_"):
        # Single Episode Detail
        return {
            "meta": {
                "id": id,
                "type": "tv",
                "name": "Selected Serial Episode",
                "poster": "https://i.imgur.com/8Q9Z5.png",
                "description": "HD Stream with VK Server"
            }
        }

    # Scraping Category Page for Date-wise Episode List
    try:
        async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=12.0) as client:
            res = await client.get(target_url)
            soup = BeautifulSoup(res.text, "html.parser")
            
            ep_idx = 1
            for a in soup.find_all("a", href=True):
                href = a["href"]
                text = a.text.strip()
                if "desiruleztv.net" in href and ("episode" in href.lower() or "watch" in href.lower()):
                    clean_ep_id = "dr_ep_" + re.sub(r'[^a-zA-Z0-9]', '_', href)
                    # Cleaning title to get proper Date-wise Episode Name
                    date_match = re.search(r'(\d{1,2}(st|nd|rd|th)?\s+[A-Za-z]+\s+\d{4})', text)
                    clean_title = text if not date_match else f"Episode - {date_match.group(0)}"
                    
                    if not any(v["id"] == clean_ep_id for v in videos):
                        videos.append({
                            "id": clean_ep_id,
                            "title": clean_title,
                            "season": 1,
                            "episode": ep_idx,
                            "released": "2026-01-01"
                        })
                        ep_idx += 1
    except Exception as e:
        print("Meta Fetch Error:", e)

    return {
        "meta": {
            "id": id,
            "type": "tv",
            "name": show_name,
            "poster": "https://i.imgur.com/8Q9Z5.png",
            "description": f"Date-wise episode list for {show_name}",
            "videos": videos[:50]
        }
    }

# 3. STREAM EXTRACTION (VK SERVER PRIORITY + FALLBACKS)
@app.get("/stream/tv/{id}.json")
async def stream(id: str):
    streams = []
    
    # Original Page Link Extract
    raw_path = id.replace("dr_ep_", "").replace("_", "/")
    if not raw_path.startswith("http"):
        page_url = f"{TARGET_SITE}/{raw_path.strip('/')}/"
    else:
        page_url = raw_path

    try:
        async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=12.0) as client:
            res = await client.get(page_url)
            soup = BeautifulSoup(res.text, "html.parser")
            
            # Searching for VK Server Embeds First
            iframes = soup.find_all("iframe", src=True)
            for iframe in iframes:
                src = iframe["src"]
                
                # Priority 1: VK / VKPrime Embed
                if "vk.com" in src or "vkprime" in src or "vkembed" in src:
                    if src.startswith("//"):
                        src = "https:" + src
                    streams.append({
                        "name": "VK Server [HD 720p]",
                        "title": "Direct VK Play (Fast Load)",
                        "url": src
                    })
                # Priority 2: Other Working Players (StreamWish, FileLions, Dood)
                elif any(server in src for server in ["streamwish", "filelions", "dood", "vidoza"]):
                    if src.startswith("//"):
                        src = "https:" + src
                    streams.append({
                        "name": "Backup Server [HD]",
                        "title": "HD Backup Stream",
                        "url": src
                    })

    except Exception as e:
        print("Stream Extraction Error:", e)

    # Fallback to External Browser/1DM if no direct iframe scraped
    if not streams:
        streams.append({
            "name": "DesiRulez Direct Page",
            "title": "Open Page in 1DM / Browser",
            "externalUrl": page_url
        })

    return {"streams": streams}
