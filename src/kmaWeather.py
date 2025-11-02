"""
기상청 API 허브에서 동해시의 오늘/미래 날씨 예보를 가져오는 코드
- API 키는 .env 파일의 KMA_SERVICE_KEY 환경변수에서 자동으로 불러옴
- 출력 데이터에는 일최저/일최고 기온, 일강수량 합계, 시간대별 상세 예보가 포함됨
"""

import os, math, sys, requests
from typing import Optional, Dict, Any, List
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv 

# ------------- .env 파일에서 환경변수 로드 -------------
load_dotenv()

# ------------- 위경도(lat, lon) → 기상청 격자 좌표(nx, ny) -------------
def latlon_to_grid(lat: float, lon: float):
    """
    위도/경도를 기상청 동네예보용 격자 좌표(nx, ny)로 변환
    (Lambert 정각원추투영, DFS 1.0 알고리즘)
    """
    RE, GRID = 6371.00877, 5.0
    SLAT1, SLAT2, OLON, OLAT = 30.0, 60.0, 126.0, 38.0
    XO, YO = 43, 136
    DEGRAD = math.pi / 180.0
    re = RE / GRID
    slat1, slat2 = SLAT1 * DEGRAD, SLAT2 * DEGRAD
    olon, olat = OLON * DEGRAD, OLAT * DEGRAD

    sn = math.tan(math.pi*0.25 + slat2*0.5) / math.tan(math.pi*0.25 + slat1*0.5)
    sn = math.log(math.cos(slat1)/math.cos(slat2)) / math.log(sn)
    sf = math.tan(math.pi*0.25 + slat1*0.5)
    sf = (sf**sn) * math.cos(slat1) / sn
    ro = math.tan(math.pi*0.25 + olat*0.5)
    ro = re * sf / (ro**sn)

    ra = math.tan(math.pi*0.25 + (lat)*DEGRAD*0.5)
    ra = re * sf / (ra**sn)
    theta = lon*DEGRAD - olon
    if theta > math.pi: theta -= 2.0*math.pi
    if theta < -math.pi: theta += 2.0*math.pi
    theta *= sn
    nx = int(ra*math.sin(theta) + XO + 0.5)
    ny = int(ro - ra*math.cos(theta) + YO + 0.5)
    return nx, ny

# ------------- 현재 시각 기준으로 가장 최근 예보 발표시각 계산 -------------
def latest_base_datetime_kst(now_kst=None):
    """
    기상청 단기예보 발표 시각: 02,05,08,11,14,17,20,23시
    현재 시각을 기준으로 사용할 수 있는 가장 최근 발표시각을 반환
    """
    if now_kst is None:
        now_kst = datetime.utcnow().replace(tzinfo=timezone.utc).astimezone(timezone(timedelta(hours=9)))
    for h in [23,20,17,14,11,8,5,2]:
        cand = now_kst.replace(hour=h, minute=0, second=0, microsecond=0)
        if now_kst >= cand + timedelta(minutes=10):
            return cand
    return (now_kst - timedelta(days=1)).replace(hour=23, minute=0, second=0, microsecond=0)

# ------------- 강수량 문자열 → 실수(mm) 변환 -------------
def _parse_pcp_to_mm(s: Optional[str]) -> float:
    """
    예보 데이터의 강수량(PCP)은 '강수없음', '1mm 미만', '5.0mm' 등 문자열로 제공
    이 값을 실제 숫자(mm)로 변환해 반환
    """
    if not s:
        return 0.0
    s = s.strip()
    if "강수없음" in s:
        return 0.0
    if "1mm 미만" in s or "1.0mm 미만" in s:
        return 0.5  # 0.5mm로 근사
    num = "".join(ch for ch in s if ch.isdigit() or ch=='.' or ch=='-')
    try:
        return float(num) if num else 0.0
    except:
        return 0.0

# ------------- float 변환 -------------
def _to_float(x: Any) -> Optional[float]:
    try:
        return float(x)
    except:
        return None

# ------------- 메인 함수 -> 지정 날짜의 예보 가져오기 -------------
def get_daily_forecast_from_apihub(
    target_date: str,              # 'YYYYMMDD' 형태 날짜
    apihub_key: str,               # .env에서 불러온 서비스 키
    lat: float = 37.524, lon: float = 129.114  # 기본: 동해시청
) -> Dict[str, Any]:
    """
    apihub.kma.go.kr의 '동네예보 OpenAPI'를 호출해 지정 날짜의 날씨 예보를 가져온다.

    반환 딕셔너리 구조:
    {
      "date": "20251102",           # 예보 대상 날짜
      "grid": {"nx": 95, "ny": 75}, # 기상청 격자 좌표
      "tmin": 10.3,                 # 일 최저기온(°C)
      "tmax": 17.8,                 # 일 최고기온(°C)
      "precip_sum_mm": 2.5,         # 하루 총 강수량(mm)
      "hourly": [                   # 시간대별 상세 예보 목록
        {
          "time": "0900",           # 예보 시각
          "TMP": 15.0,              # 기온(°C, Temperature)
          "POP": 20.0,              # 강수확률(%)
          "PCP": "강수없음",         # 강수량 원문(문자열)
          "REH": 60.0               # 상대습도(%)
        },
        ...
      ]
    }
    """
    nx, ny = latlon_to_grid(lat, lon)
    base_dt = latest_base_datetime_kst()
    base_date, base_time = base_dt.strftime("%Y%m%d"), base_dt.strftime("%H%M")

    url = "https://apihub.kma.go.kr/api/typ02/openApi/VilageFcstInfoService_2.0/getVilageFcst"
    params = {
        "serviceKey": apihub_key,   # 허브 키
        "dataType": "JSON",
        "numOfRows": "1000",
        "pageNo": "1",
        "base_date": base_date,
        "base_time": base_time,
        "nx": str(nx),
        "ny": str(ny),
    }

    # 첫 요청
    r = requests.get(url, params=params, timeout=20)
    # 혹시 serviceKey 파라미터가 안 맞는 경우 authKey로 재시도
    if r.status_code == 401:
        params2 = dict(params)
        params2.pop("serviceKey", None)
        params2["authKey"] = apihub_key
        r = requests.get(url, params=params2, timeout=20)
    r.raise_for_status()

    # JSON 파싱
    j = r.json()
    items: List[Dict[str, Any]] = (j.get("response", {})
                                     .get("body", {})
                                     .get("items", {})
                                     .get("item", []))

    hourly_map: Dict[str, Dict[str, Any]] = {}
    tmin = None
    tmax = None
    pcp_sum = 0.0

    # 항목별(category) 데이터 정리
    for it in items:
        if it.get("fcstDate") != target_date:
            continue
        cat, t, val = it.get("category"), it.get("fcstTime"), it.get("fcstValue")
        slot = hourly_map.setdefault(t, {"time": t})

        if cat == "TMP":       # 기온(°C)
            slot["TMP"] = _to_float(val)
        elif cat == "POP":     # 강수확률(%)
            slot["POP"] = _to_float(val)
        elif cat == "PCP":     # 강수량(mm)
            slot["PCP"] = val
            pcp_sum += _parse_pcp_to_mm(val)
        elif cat == "REH":     # 습도(%)
            slot["REH"] = _to_float(val)
        elif cat == "TMN":     # 일 최저기온(°C)
            tmin = _to_float(val)
        elif cat == "TMX":     # 일 최고기온(°C)
            tmax = _to_float(val)

    # TMN/TMX가 비어있을 경우 시간별 TMP 값으로 보정
    if (tmin is None or tmax is None) and hourly_map:
        tmps = [v.get("TMP") for v in hourly_map.values() if v.get("TMP") is not None]
        if tmps:
            if tmin is None: tmin = min(tmps)
            if tmax is None: tmax = max(tmps)

    hourly = [hourly_map[k] for k in sorted(hourly_map.keys())]

    # 최종 반환
    return {
        "date": target_date,
        "grid": {"nx": nx, "ny": ny},
        "tmin": tmin,
        "tmax": tmax,
        "precip_sum_mm": round(pcp_sum, 1),
        "hourly": hourly
    }


if __name__ == "__main__":
    # .env에서 불러온 서비스 키 읽기
    KEY = os.getenv("KMA_SERVICE_KEY")
    if not KEY:
        raise RuntimeError("KMA_SERVICE_KEY가 정의되어 있지 않습니다")

    # 명령행 인자로 날짜 지정 가능, 없으면 오늘 날짜 사용
    if len(sys.argv) > 1:
        date_str = sys.argv[1]
    else:
        date_str = datetime.utcnow().replace(
            tzinfo=timezone.utc
        ).astimezone(timezone(timedelta(hours=9))).strftime("%Y%m%d")

    from pprint import pprint
    pprint(get_daily_forecast_from_apihub(date_str, KEY))
