# DesiSerials TV Streamio addon

A FastAPI Stremio addon for cataloguing DesiSerials pages and resolving the site's
multi-stage player flow (episode page -> gateway/player -> media host -> HLS/MP4).

## Run locally

```bash
python -m pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```

Install `http://localhost:8000/manifest.json` in Stremio.

## Configuration

- `TARGET_SITE`: catalog site, default `https://www.desiserials.ru`
- `PUBLIC_BASE_URL`: public HTTPS URL when deployed behind a proxy
- `MEDIA_HOSTS_EXTRA`: comma-separated additional media hostnames
- `LOG_LEVEL` and `USER_AGENT`: optional request settings

The resolver intentionally uses an allowlist and does not attempt to bypass
CAPTCHA, Cloudflare, or other access controls. If a provider requires a browser
challenge, it is skipped while other providers continue to be tried.

## Deployment notes

Set `PUBLIC_BASE_URL` to the externally reachable addon URL when using Vercel,
a reverse proxy, or another serverless platform. HLS playlists and media
segments are rewritten through the addon so relative segment URLs continue to
work in Stremio.
