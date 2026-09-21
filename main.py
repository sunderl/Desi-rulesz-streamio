import os
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config import settings
from proxy import router as proxy_router
from proxy import stream_tv
from scraper import catalog_tv, meta_tv


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http = httpx.AsyncClient(
        headers=settings.headers,
        follow_redirects=True,
        timeout=httpx.Timeout(20.0, connect=8.0),
        limits=httpx.Limits(max_keepalive_connections=25, max_connections=120),
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

app.include_router(proxy_router)
app.include_router(catalog_tv.router)
app.include_router(meta_tv.router)
app.include_router(stream_tv.router)


@app.get("/")
def home():
    return {
        "status": "online",
        "addon": "Desi Serials GOAT",
        "manifest": "/manifest.json",
    }


@app.get("/manifest.json")
def manifest():
    return {
        "id": "org.desiserials.goat.addon",
        "version": "7.0.0",
        "name": "Desi Serials GOAT HD",
        "description": "Torrentio-style ultra-fast daily Hindi TV Serials streaming addon for Stremio & Novio.",
        "resources": ["catalog", "meta", "stream"],
        "types": ["tv"],
        "catalogs": [
            {"type": "tv", "id": "desiserials_channels", "name": "TV Channels", "extra": [{"name": "search", "isRequired": False}]},
            {"type": "tv", "id": "desiserials_latest", "name": "Latest Daily Episodes", "extra": [{"name": "search", "isRequired": False}]},
        ],
        "idPrefixes": ["ds_"],
    }


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
