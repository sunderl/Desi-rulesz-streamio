import re

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from config import settings
from utils import (
    decode_url,
    encode_url,
    is_allowed_media_url,
    is_hls_url,
    normalize_url,
    validate_target_url,
)

router = APIRouter()


@router.get("/proxy")
async def proxy_media(url: str, request: Request, range_header: str | None = None):
    try:
        target_url = decode_url(url)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid URL")

    target_url = validate_target_url(target_url)

    range_header = range_header or request.headers.get("range")
    request_headers = {
        "User-Agent": settings.headers["User-Agent"],
        "Referer": settings.target_site,
        "Origin": settings.target_site,
        "Accept": "*/*",
    }
    if range_header:
        request_headers["Range"] = range_header

    client = request.app.state.http

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
        for key in ("content-type", "content-length", "content-range", "accept-ranges", "cache-control", "etag"):
            if key in response.headers:
                response_headers[key] = response.headers[key]

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
        raise HTTPException(status_code=502, detail=f"Proxy streaming failed: {exc}")


@router.get("/proxy/hls")
async def proxy_hls(url: str, request: Request):
    try:
        playlist_url = decode_url(url)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid URL")

    playlist_url = validate_target_url(playlist_url)
    client = request.app.state.http

    try:
        response = await client.get(playlist_url)
        if response.status_code != 200:
            raise HTTPException(status_code=502, detail="Playlist fetch failed")

        public_base = (settings.public_base_url or str(request.base_url)).rstrip("/")
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
            headers={
                "Cache-Control": "no-cache",
                "Access-Control-Allow-Origin": "*",
            },
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"HLS rewriting failed: {exc}")
