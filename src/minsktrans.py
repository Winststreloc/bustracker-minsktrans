import asyncio
import enum
import re
import ssl
import sys
import time

import aiohttp
import bs4



# Enums

class Place(enum.Enum):
    Minsk = "minsk"
    Region = "region"


class TransportType(enum.Enum):
    Bus = "bus"
    Trolleybus = "trolleybus"
    Tram = "tram"


class _ArithmeticOp(enum.Enum):
    Xor = "^"
    Add = "+"


# Internal helpers

class _RateLimiter:
    """Token-bucket rate limiter: позволяет не более *rps* запросов в секунду."""

    def __init__(self, rps: float) -> None:
        self._interval = 1.0 / rps
        self._next_allowed_at: float = 0.0

    async def __aenter__(self) -> "_RateLimiter":
        now = time.monotonic()
        wait_until = self._next_allowed_at
        self._next_allowed_at = max(now, wait_until) + self._interval
        if now < wait_until:
            await asyncio.sleep(wait_until - now)
        return self

    async def __aexit__(self, *_) -> None:
        pass


class _AntiScrapeTransform:
    """
    Сайт minsktrans применяет простую арифметическую операцию к числовым
    параметрам перед отправкой, чтобы отфильтровать наивных ботов.
    Этот класс воспроизводит логику их JS-защиты.
    """

    _PATTERN = re.compile(r"'v': function \(a\) { return (\d+) (.) a; }")

    def __init__(self, operand: int, op: _ArithmeticOp) -> None:
        self._operand = operand
        self._op = op

    @classmethod
    def from_html(cls, html: str) -> "_AntiScrapeTransform":
        soup = bs4.BeautifulSoup(html, "html.parser")
        for script in soup.find_all("script"):
            if not script.string:
                continue
            match = cls._PATTERN.search(script.string)
            if not match:
                continue
            operand = int(match[1])
            op = _ArithmeticOp(match[2])
            return cls(operand, op)
        raise RuntimeError("Anti-scrape transform not found in page JS.")

    def apply(self, value: int | str) -> int:
        if isinstance(value, str):
            # Берём только ведущие цифры (как в оригинальном JS)
            numeric = ""
            for ch in value:
                if not ch.isdigit():
                    break
                numeric += ch
            value = int(numeric) if numeric else 0

        match self._op:
            case _ArithmeticOp.Xor:
                return self._operand ^ value
            case _ArithmeticOp.Add:
                return self._operand + value
            case _:
                raise RuntimeError(f"Unknown op: {self._op}")


# Public client

class MinsktransClient:
    """
    Асинхронный клиент для неофициального API minsktrans.by.

    Использование:
        async with MinsktransClient() as client:
            data = await client.scoreboard(stop_id="3087838")
    """

    _BASE_URL = "https://www.minsktrans.by/lookout_yard"
    _FRONT_URL = f"{_BASE_URL}/Home/Index/minsk"
    _API_URL = f"{_BASE_URL}/Data/"
    _RPS = 3

    # --- Lifecycle ---

    async def __aenter__(self) -> "MinsktransClient":
        self._ssl = self._make_ssl_context()
        self._session = await aiohttp.ClientSession().__aenter__()
        self._limiter = _RateLimiter(self._RPS)

        try:
            self._token, self._transform = await self._bootstrap()
        except BaseException:
            await self._session.__aexit__(*sys.exc_info())
            raise

        return self

    async def __aexit__(self, *exc_info) -> None:
        await self._session.__aexit__(*exc_info)

    # --- Public API ---

    async def route_list(
        self,
        transport_type: TransportType = TransportType.Trolleybus,
        place: Place = Place.Minsk,
    ) -> dict:
        return await self._post("RouteList", p=place.value, tt=transport_type.value)

    async def track(
        self,
        route: str,
        transport_type: TransportType = TransportType.Trolleybus,
        place: Place = Place.Minsk,
    ) -> dict:
        return await self._post("Track", r=route, p=place.value, tt=transport_type.value)

    async def route(
        self,
        route: str,
        transport_type: TransportType = TransportType.Trolleybus,
        place: Place = Place.Minsk,
    ) -> dict:
        return await self._post("Route", r=route, p=place.value, tt=transport_type.value)

    async def vehicles(
        self,
        route: str,
        transport_type: TransportType = TransportType.Trolleybus,
        place: Place = Place.Minsk,
    ) -> dict:
        return await self._post(
            "Vehicles",
            r=route,
            p=place.value,
            tt=transport_type.value,
            v=self._transform.apply(route),
        )

    async def scoreboard(self, stop_id: str, place: Place = Place.Minsk) -> dict:
        return await self._post(
            "Scoreboard",
            s=stop_id,
            p=place.value,
            v=self._transform.apply(stop_id),
        )

    # --- Private ---

    @staticmethod
    def _make_ssl_context() -> ssl.SSLContext:
        # minsktrans использует самоподписанный / битый сертификат
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    async def _bootstrap(self) -> tuple[str, _AntiScrapeTransform]:
        """Получает CSRF-токен и параметры анти-скрейп защиты с фронтенда."""
        async with self._session.get(self._FRONT_URL, ssl=self._ssl) as resp:
            html = await resp.text()

        soup = bs4.BeautifulSoup(html, "html.parser")
        token_input = soup.find("input", {"name": "__RequestVerificationToken"})
        if token_input is None:
            raise RuntimeError("CSRF token not found.")
        token: str = token_input["value"]  # type: ignore[assignment]

        transform = _AntiScrapeTransform.from_html(html)
        return token, transform

    async def _post(self, endpoint: str, **params) -> dict:
        payload = {**params, "__RequestVerificationToken": self._token}
        async with self._limiter:
            async with self._session.post(
                self._API_URL + endpoint,
                ssl=self._ssl,
                data=payload,
                headers={"Referer": self._FRONT_URL},
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise RuntimeError(
                        f"API error [{resp.status}] on {endpoint!r}: {body}"
                    )
                return await resp.json()