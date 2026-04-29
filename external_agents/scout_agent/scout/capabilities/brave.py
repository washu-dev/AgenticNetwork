import httpx

from scout.capabilities.base import Capability, CapabilityResult

WEB_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
NEWS_ENDPOINT = "https://api.search.brave.com/res/v1/news/search"

FRESHNESS_DESC = (
    "Optional recency filter. One of: pd (past day), pw (past week), "
    "pm (past month), py (past year). Use for trend/news queries."
)


def _common_schema(include_freshness: bool) -> dict:
    props = {
        "q": {
            "type": "string",
            "description": "Search query. Be specific; vary phrasing across calls.",
        },
        "count": {
            "type": "integer",
            "description": "Number of results to return (1-20).",
            "minimum": 1,
            "maximum": 20,
            "default": 10,
        },
    }
    if include_freshness:
        props["freshness"] = {
            "type": "string",
            "description": FRESHNESS_DESC,
            "enum": ["pd", "pw", "pm", "py"],
        }
    return {
        "type": "object",
        "properties": props,
        "required": ["q"],
        "additionalProperties": False,
    }


class _BraveBase(Capability):
    endpoint: str

    def __init__(self, api_key: str, *, timeout: float = 15.0) -> None:
        self._api_key = api_key
        self._timeout = timeout

    async def _call(self, args: dict) -> CapabilityResult:
        if not self._api_key:
            return CapabilityResult(ok=False, error="BRAVE_API_KEY not set")
        params = {"q": args["q"], "count": min(int(args.get("count", 10)), 20)}
        if args.get("freshness"):
            params["freshness"] = args["freshness"]
        headers = {
            "X-Subscription-Token": self._api_key,
            "Accept": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(self.endpoint, params=params, headers=headers)
        except httpx.TimeoutException:
            return CapabilityResult(ok=False, error="brave request timed out")
        except httpx.HTTPError as e:
            return CapabilityResult(ok=False, error=f"brave network error: {e}")

        if resp.status_code == 429:
            retry = resp.headers.get("Retry-After", "?")
            return CapabilityResult(ok=False, error=f"rate_limited; retry_after={retry}")
        if resp.status_code >= 400:
            return CapabilityResult(
                ok=False, error=f"brave http {resp.status_code}: {resp.text[:200]}"
            )

        data = resp.json()
        return CapabilityResult(
            ok=True,
            content={"query": params["q"], "results": self._trim(data)},
            meta={"status": resp.status_code},
        )

    def _trim(self, data: dict) -> list[dict]:  # pragma: no cover - subclass overrides
        raise NotImplementedError


class BraveWebSearch(_BraveBase):
    name = "brave_web_search"
    description = (
        "General web search via Brave. Use for background context, established facts, "
        "documents, and reference material. Prefer this over news for non-time-sensitive queries."
    )
    input_schema = _common_schema(include_freshness=True)
    endpoint = WEB_ENDPOINT

    async def invoke(self, args: dict) -> CapabilityResult:
        return await self._call(args)

    def _trim(self, data: dict) -> list[dict]:
        items = (data.get("web") or {}).get("results") or []
        out = []
        for r in items:
            out.append(
                {
                    "title": r.get("title"),
                    "url": r.get("url"),
                    "description": r.get("description"),
                    "age": r.get("age") or r.get("page_age"),
                }
            )
        return out


class BraveNewsSearch(_BraveBase):
    name = "brave_news_search"
    description = (
        "News search via Brave. Use for breaking events, recent developments, and "
        "trend questions where freshness matters. Prefer freshness=pw or pm for trend windows."
    )
    input_schema = _common_schema(include_freshness=True)
    endpoint = NEWS_ENDPOINT

    async def invoke(self, args: dict) -> CapabilityResult:
        return await self._call(args)

    def _trim(self, data: dict) -> list[dict]:
        items = data.get("results") or []
        out = []
        for r in items:
            out.append(
                {
                    "title": r.get("title"),
                    "url": r.get("url"),
                    "description": r.get("description"),
                    "age": r.get("age") or r.get("page_age"),
                    "breaking": r.get("breaking"),
                }
            )
        return out
