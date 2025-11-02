"""
Google Places API 모듈
---------------------
planner.py에서 확정된 장소 이름들을 받아서,
Google Maps API를 통해 위치 정보(위도·경도)와 영업시간을 가져온다.

예를 들어, 플래너에서 ["묵호항 카페", "동해 소품샵"] 같은 확정 장소 리스트를 넘기면,
이 모듈이 구글맵에서 실제 좌표, 주소, 영업시간 등을 찾아 반환한다.

Input
------
search_with_hours(
    queries: List[str],        # 장소 이름 리스트 (예: ["묵호항 카페", "동해 소품샵"])
    lat: float,                # 기준 위치 위도 (여행 중심점)
    lon: float,                # 기준 위치 경도 (여행 중심점)
    target_date: datetime.date,# 기준 날짜 (오늘이 아닐 수도 있음)
    radius: int = 3000,        # 검색 반경 (미터 단위, 기본값 3000)
    top_k: int = 5             # 반환할 최대 장소 수 (기본값 5)
) -> List[Dict]

Output
-------
반환값: List[Dict]  # 각 장소의 세부 정보
각 Dict(장소 정보) 필드:
    name: str                 # 장소명
    address: str              # 전체 주소
    place_id: str             # Google 고유 ID
    lat: float                # 위도
    lon: float                # 경도
    map_url: str              # Google Maps 링크
    hours_for_date: str|None  # 입력된 날짜(target_date) 기준 영업시간
    open_now: bool|None       # 현재 영업 중 여부 (참고용)
    source: str               # 데이터 출처 ("google_places")
"""

import os, requests, datetime, zoneinfo

PLACES_KEY = os.environ["GOOGLE_PLACES_API_KEY"]

def _place_url(place_id: str) -> str:
    """공식 place_id 기반 지도 URL"""
    return f"https://www.google.com/maps/place/?q=place_id:{place_id}"

def _hours_for_date(opening_hours: dict, target_date: datetime.date) -> str | None:
    """
    주어진 날짜(target_date, KST 기준)의 영업시간을 반환.
    Google weekday_text는 Monday(0)~Sunday(6) 순서.
    """
    if not opening_hours or not opening_hours.get("weekday_text"):
        return None

    # 요일 인덱스 (Monday=0 ~ Sunday=6)
    idx = target_date.weekday()
    line = opening_hours["weekday_text"][idx]  # 예시ㅣ: "Friday: 9:00 AM – 6:00 PM"
    return f"{target_date.strftime('%Y-%m-%d (%a)')} {line.split(':',1)[1].strip()}"


def text_search(query: str, lat: float=None, lon: float=None, radius: int=3000):
    """키워드 기반 장소 검색"""
    url = "https://maps.googleapis.com/maps/api/place/textsearch/json"
    params = {"query": query, "key": PLACES_KEY}
    if lat is not None and lon is not None:
        params.update({"location": f"{lat},{lon}", "radius": radius})
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    return r.json().get("results", [])

def details(place_id: str):
    """상세 정보 요청"""
    url = "https://maps.googleapis.com/maps/api/place/details/json"
    fields = "name,formatted_address,opening_hours,utc_offset_minutes,place_id,geometry"
    r = requests.get(url, params={"place_id": place_id, "fields": fields, "key": PLACES_KEY}, timeout=10)
    r.raise_for_status()
    return r.json().get("result", {})

def search_with_hours(
    queries: list[str],
    lat: float,
    lon: float,
    radius: int = 3000,
    top_k: int = 3,
    target_date: datetime.date | None = None
):
    """
    여러 키워드로 주변 장소를 검색하고,
    특정 날짜(target_date)의 영업시간과 URL을 반환
    """
    import datetime
    if target_date is None:
        target_date = datetime.datetime.now().date()

    seen, results = set(), []
    for q in queries:
        for item in text_search(q, lat, lon, radius):
            pid = item["place_id"]
            if pid in seen:
                continue
            seen.add(pid)
            det = details(pid)
            oh = det.get("opening_hours")
            results.append({
                "name": det.get("name") or item.get("name"),
                "address": det.get("formatted_address"),
                "place_id": pid,
                "map_url": _place_url(pid),
                "hours_for_date": _hours_for_date(oh, target_date),
                "open_now": oh.get("open_now") if oh else None,
                "lat": det["geometry"]["location"]["lat"],
                "lon": det["geometry"]["location"]["lng"],
                "source": "google_places",
            })
            if len(results) >= top_k:
                return results
    return results

