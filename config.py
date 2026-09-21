import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    public_base_url: str = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
    target_site: str = "https://www.desiserials.ru"
    default_poster: str = "https://images.unsplash.com/photo-1593784991095-a205069470b6?w=500&q=80"
    allowed_media_hosts: frozenset[str] = frozenset({
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
    })
    headers: dict[str, str] = field(default_factory=lambda: {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Referer": "https://www.desiserials.ru",
    })


settings = Settings()
