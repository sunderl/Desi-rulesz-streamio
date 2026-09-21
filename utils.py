import base64
import ipaddress
from urllib.parse import urljoin, urlparse, unquote

from fastapi import HTTPException

from config import settings


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

    full = urljoin(base_url, value)
    if full.startswith(("http://", "https://")):
        return full
    return None


def is_hls_url(url: str) -> bool:
    return urlparse(url).path.lower().endswith(".m3u8")


def is_allowed_media_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return False

    if parsed.scheme not in {"http", "https"}:
        return False

    host = (parsed.hostname or "").lower()
    if not host:
        return False

    if not any(host == allowed_host or host.endswith("." + allowed_host) for allowed_host in settings.allowed_media_hosts):
        return False

    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_reserved or ip.is_link_local:
            return False
    except ValueError:
        pass

    return True


def validate_target_url(target_url: str) -> str:
    if not is_allowed_media_url(target_url):
        raise HTTPException(status_code=403, detail="Host restricted")
    return target_url
