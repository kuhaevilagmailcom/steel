import aiohttp

async def search_music(query: str, limit: int = 8):
    url = "https://itunes.apple.com/search"
    params = {"term": query, "entity": "song", "limit": limit, "country": "US"}
    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params, timeout=15) as r:
            r.raise_for_status()
            data = await r.json()
            return data.get("results", [])
