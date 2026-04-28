import math
import asyncio
from aiohttp import web
from minsktrans import MinsktransClient, TransportType

# --- Конфиг ---
PROXY_PORT    = 8080
STOP_ID       = "3087838"  # Брестская
TARGET_ROUTE  = "56"
AVG_SPEED_MPS = 5.55       # ~20 км/ч

# --- Транслитерация ---
_TRANSLIT = {
    'а':'a','б':'b','в':'v','г':'g','д':'d','е':'e','ё':'yo',
    'ж':'zh','з':'z','и':'i','й':'y','к':'k','л':'l','м':'m',
    'н':'n','о':'o','п':'p','р':'r','с':'s','т':'t','у':'u',
    'ф':'f','х':'kh','ц':'ts','ч':'ch','ш':'sh','щ':'sch',
    'ъ':'','ы':'y','ь':'','э':'e','ю':'yu','я':'ya',
}

def translit(s: str) -> str:
    result = ""
    for ch in s:
        lo = ch.lower()
        t = _TRANSLIT.get(lo, ch)
        result += t.capitalize() if ch.isupper() and t else t
    return result

# --- Парсинг official (может быть "<1", "D", числа) ---
def parse_official(info: list) -> str:
    """
    Info[] содержит минуты до прибытия.
    Значения могут быть: числа, "<1" (меньше минуты), буквы (на конечной).
    Возвращаем только числовые значения, "<1" → "1".
    """
    result = []
    for x in info:
        s = str(x).strip()
        if s == "<1":
            result.append("1")
        else:
            try:
                result.append(str(int(float(s))))
            except (ValueError, TypeError):
                pass  # пропускаем нечисловые значения ("D", "A" и т.п.)
    return ", ".join(result)

# --- Геодезия ---

def haversine(lat1, lon1, lat2, lon2):
    R = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))

def nearest_idx(points, lat, lon):
    return min(range(len(points)),
               key=lambda i: haversine(lat, lon, points[i]["Latitude"], points[i]["Longitude"]))

def route_dist(points, bus_lat, bus_lon, stop_lat, stop_lon):
    if not points:
        return -1
    bi = nearest_idx(points, bus_lat, bus_lon)
    si = nearest_idx(points, stop_lat, stop_lon)
    if bi >= si:
        return -1
    return sum(
        haversine(points[i]["Latitude"], points[i]["Longitude"],
                  points[i+1]["Latitude"], points[i+1]["Longitude"])
        for i in range(bi, si)
    )

# --- Кэш клиента (переиспользуем между запросами) ---
_client: MinsktransClient | None = None

async def get_client() -> MinsktransClient:
    global _client
    if _client is None:
        _client = MinsktransClient()
        await _client.__aenter__()
    return _client

async def reset_client():
    global _client
    if _client is not None:
        try:
            await _client.__aexit__(None, None, None)
        except Exception:
            pass
        _client = None

# --- Handler ---

async def handle_buses(request: web.Request) -> web.Response:
    global _client
    for attempt in range(2):
        try:
            client = await get_client()

            sb       = await client.scoreboard(stop_id=STOP_ID)
            stop_lat = sb["Latitude"]
            stop_lon = sb["Longitude"]
            stop_name = translit(sb.get("StopName", ""))

            official = ""
            for route in sb.get("Routes", []):
                if route.get("Number") == TARGET_ROUTE:
                    official = parse_official(route.get("Info", []))
                    break

            track    = await client.track(route=TARGET_ROUTE, transport_type=TransportType.Bus)
            points   = track.get("PointsB", [])

            veh_data = await client.vehicles(route=TARGET_ROUTE, transport_type=TransportType.Bus)
            vehicles = veh_data.get("Vehicles", [])

            buses = []
            for v in vehicles:
                d = route_dist(points, v["Latitude"], v["Longitude"], stop_lat, stop_lon)
                if d > 0:
                    buses.append({
                        "id":   str(v["Id"]),
                        "dist": int(d),
                        "eta":  max(1, round(d / AVG_SPEED_MPS / 60)),
                    })
            buses.sort(key=lambda x: x["dist"])

            result = {
                "stop":     stop_name,
                "buses":    buses[:4],
                "official": official,
            }
            print(f"OK | stop: {stop_name} | buses: {len(buses)} | official: {official!r}")
            return web.json_response(result)

        except Exception as e:
            print(f"Error (attempt {attempt+1}): {e}")
            await reset_client()  # сбросить клиент — пересоздаст с новым bootstrap
            if attempt == 1:
                return web.json_response({"error": str(e)}, status=500)
            await asyncio.sleep(1)


app = web.Application()
app.router.add_get("/buses", handle_buses)

if __name__ == "__main__":
    print(f"Proxy starting on http://0.0.0.0:{PROXY_PORT}")
    print(f"ESP should request: http://<your-pc-ip>:{PROXY_PORT}/buses")
    web.run_app(app, port=PROXY_PORT)