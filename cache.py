import time

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
