"""
네이버 블로그 데이터 수집 파이프라인 (SE3 전용, 본문 텍스트만)
1) 네이버 검색 API로 URL 후보 여러 건 수집
2) 각 URL의 SE3 본문만 HTML 크롤링
3) Firestore에 업서트(중복 방지) 저장

- 이미지/영상은 수집하지 않음 (텍스트만 저장)
- 같은 URL은 같은 문서 ID로 덮어쓰기 (업서트)
- 모바일(m.blog) 뷰로 우회 -> 파싱 안정성 관련
- Firestore 필드 스키마(8개): id, source, url, title, rawText, author, publishedAt(ts), updatedAt(ts)
- source는 항상 'blog'

"""

import re
import time
import hashlib
from typing import List, Dict, Optional
from urllib.parse import quote, urlparse, urljoin
from datetime import datetime, timezone, timedelta

import requests
from bs4 import BeautifulSoup
import pandas as pd

import firebase_admin
from firebase_admin import credentials


# --------------- 환경 설정 ---------------
FIREBASE_KEY = "/Users/hyolim/Desktop/proj/KWTour/firebase_key.json" 

# 네이버 API 인증 정보
CLIENT_ID = "ZwEIgvBDP2AvxfeQw58f"
CLIENT_SECRET = "tf8IRE_CFQ"

# Firestore 컬렉션명
COLLECTION = "contents"

# API 요청 헤더
UA_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    )
}
# 네이버 검색 API 인증 헤더
API_HEADERS = {
    "X-Naver-Client-Id": CLIENT_ID,
    "X-Naver-Client-Secret": CLIENT_SECRET,
}


# --------------- Firestore 연결 ---------------
cred = credentials.Certificate(FIREBASE_KEY)
if not firebase_admin._apps:
    firebase_admin.initialize_app(cred)
db = firestore.client()
print("[INFO] Firestore 연결 완료")


# --------------- 유틸리티 함수 ---------------
def md5(s: str) -> str: # 문자열을 MD5 해시로 변환 → 문서 ID로 사용: 동일 게시글 = 동일 ID
    return hashlib.md5(s.encode("utf-8")).hexdigest()

def strip_html_tags(s: Optional[str]) -> str: # HTML 태그 없야기
    if not isinstance(s, str):
        return ""
    return re.sub(r"<.*?>", "", s).strip()

def clean_spaces(text: str) -> str: # 공백 등 정리
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

def normalize_date_string(s: str) -> Optional[str]:
    """
    게시일 문자열을 표준 UTC ISO 포맷('YYYY-MM-DDTHH:MM:SSZ')으로 변환.
    - 시간 정보 없으면 00:00:00Z 로 보정.
    - UTC 기반(Z 표기).
    """
    if not s:
        return None
    s = s.strip()
    # YYYYMMDD
    m = re.fullmatch(r"(\d{4})(\d{2})(\d{2})", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}T00:00:00Z"
    # YYYY.MM.DD
    m = re.fullmatch(r"(\d{4})\.(\d{1,2})\.(\d{1,2})\.?", s)
    if m:
        yyyy, mm, dd = m.group(1), m.group(2).zfill(2), m.group(3).zfill(2)
        return f"{yyyy}-{mm}-{dd}T00:00:00Z"
    # ISO 포맷 or 시간 포함된 문자열
    try:
        dt = datetime.fromisoformat(s)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return None


def get_now_kst_iso() -> str:
    """현재 한국시간(KST)을 ISO 포맷(+09:00)으로 반환"""
    kst = timezone(timedelta(hours=9))
    now = datetime.now(kst)
    return now.isoformat(timespec="seconds")  # 예: 2025-10-21T10:04:19+09:00



# --------------- 네이버 검색 API: URL 후보 가져오기 ---------------
def search_naver_blog(query: str, total: int = 30) -> pd.DataFrame:
    """
    네이버 블로그 검색 API로 다건의 URL 수집

    파라미터
    -----------
    query : str // 검색어 (ex: '묵호', '동해', '동해 여행')
    total : int // 총 수집 개수

    * api_author/api_postdate: 본문 파싱 실패 시 보조값으로 사용(저장X)
    """
    base = "https://openapi.naver.com/v1/search/blog.json"
    rows = []
    fetched = 0

    while fetched < total:
        remain = total - fetched
        display = min(100, remain) # API 호출 최대 100
        start = fetched + 1 # start는 1부터 시작

        url = f"{base}?query={quote(query)}&display={display}&start={start}"
        res = requests.get(url, headers=API_HEADERS, timeout=20)
        res.raise_for_status()
        data = res.json()

        items = data.get("items", [])
        if not items:
            break

        for it in items:
            rows.append({
                "title": strip_html_tags(it.get("title", "")),
                "url": it.get("link", ""),
                "api_author": it.get("bloggername", ""),
                "api_postdate": it.get("postdate", ""),  # 보통 'YYYYMMDD'
            })

        fetched += len(items)
        if len(items) == 0:
            break

    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.drop_duplicates(subset=["url"]).reset_index(drop=True)
    return df


# --------------- SE3 전용: 본문 파싱 (이미지, 영상 제외) ---------------
def to_mblog_postview(entry_url: str) -> str:
    """
    PC 프레임셋 주소(https://blog.naver.com/{id}/{logNo}) --> 모바일 PostView로 변환
    모바일 뷰는 DOM이 단순해서 파싱 안정적
    """
    p = urlparse(entry_url)
    if p.netloc in ("blog.naver.com", "m.blog.naver.com"):
        parts = p.path.strip("/").split("/")
        if len(parts) >= 2:
            blog_id, log_no = parts[0], parts[1]
            return f"https://m.blog.naver.com/PostView.naver?blogId={blog_id}&logNo={log_no}"
    # 예외// frameset만 보이면 iframe src 추출로 폴백
    res = requests.get(entry_url, headers=UA_HEADERS, timeout=20)
    res.raise_for_status()
    soup = BeautifulSoup(res.text, "html.parser")
    iframe = soup.find("iframe", id="mainFrame")
    if iframe and iframe.get("src"):
        return urljoin(entry_url, iframe["src"])
    return entry_url  # 최후 폴백


def parse_se3_text_only(soup: BeautifulSoup) -> Dict[str, str]:
    """
    스마트에디터3 구조만 대상으로 텍스트 본문/메타데이터 추출.
    - 본문 컨테이너: div.se-main-container
    - 문단 블록: div.se-component.se-text
    - 제목: h3.se_textarea (폴백: og:title)
    - 작성일: span.se_publishDate
    - 작성자: span.se_publishAuthor
    """
    # 제목
    title = ""
    node = soup.select_one("h3.se_textarea")
    if node:
        title = node.get_text(strip=True)
    if not title:
        og = soup.select_one('meta[property="og:title"]')
        if og and og.get("content"):
            title = og["content"].strip()

    # 작성일/작성자
    date = ""
    d = soup.select_one("span.se_publishDate")
    if d:
        date = d.get_text(strip=True)

    author = ""
    a = soup.select_one("span.se_publishAuthor")
    if a:
        author = a.get_text(strip=True)

    # 본문(텍스트만)
    content = ""
    main = soup.select_one("div.se-main-container")
    if main:
        texts = []
        for block in main.select("div.se-component.se-text"):
            t = block.get_text(" ", strip=True)
            if t:
                texts.append(t)
        content = "\n".join(texts)

    return {
        "title": title,
        "publishedAt": clean_spaces(date),
        "author": author,
        "rawText": clean_spaces(content),
    }


def fetch_post_text_se3(entry_url: str) -> Dict[str, str]:
    """
    SE3 전용 본문 수집:
    - PC 프레임 주소 → 모바일 PostView로 변환
    - HTML 요청 → 파싱 → (제목/작성일/작성자/본문) 반환
    """
    real_url = to_mblog_postview(entry_url)
    res = requests.get(real_url, headers=UA_HEADERS, timeout=20)
    res.raise_for_status()
    soup = BeautifulSoup(res.text, "html.parser")
    parsed = parse_se3_text_only(soup)
    return parsed


# --------------- Firestore 업서트 저장 ---------------
def upsert_doc(url: str, doc: Dict, collection: str = COLLECTION) -> None:
    doc_id = md5(url)
    ref = db.collection(collection).document(doc_id)
    doc_with_id = {"id": doc_id, **doc}
    ref.set(doc_with_id, merge=True)


# --------------- 메인 파이프라인 실행 함수 ---------------
def run_pipeline_text_only(
    queries: List[str],
    total_per_query: int = 30,
    delay_sec: float = 0.7,
) -> None:
    """
    queries: 여러 검색어를 한 번에 처리 (예: ['묵호', '묵호 여행'])
    total_per_query: 검색어당 수집할 최대 개수
    delay_sec: 요청 간 간격(차단 방지/예의)

    저장 스키마(8개 필드):
      - id(문서ID md5(url)), source("blog"), url, title, rawText, author,
        publishedAt(timestamp, 가능할 때만), updatedAt(timestamp)
    """
    for q in queries:
        print(f"\n[SEARCH] '{q}' 상위 {total_per_query}건 수집 중 ...")
        df = search_naver_blog(query=q, total=total_per_query)
        if df.empty:
            print(f"  - 결과 없음 (query='{q}')")
            continue

        ok = fail = 0
        for _, row in df.iterrows():
            url = row["url"]

            try:
                # 1) SE3 본문 크롤링
                body = fetch_post_text_se3(url)

                # 2) 게시일(timestamp) 파싱: body > api_postdate 순으로 시도
                date_norm = normalize_date_string(body.get("publishedAt")) or normalize_date_string(
                    row.get("api_postdate", "")
                )

                # 3) Firestore 저장 문서 (스키마 8개 필드만)
                doc = {
                    "source": "blog",                                # 고정값
                    "url": url,                                      # 원문 주소
                    "title": body.get("title") or row.get("title", ""),
                    "rawText": body.get("rawText") or "",            # 본문 실패 시 빈 문자열
                    "author": body.get("author") or row.get("api_author", ""),
                    "updatedAt": get_now_kst_iso(),  # 수집 시각
                }
                # publishedAt은 가능한 경우에만 넣음(없으면 필드 자체 생략)
                if date_norm:
                    doc["publishedAt"] = date_norm

                upsert_doc(url, doc)
                ok += 1
                print(f"  [OK] {url}")

            except Exception as e:
                # 실패해도 스키마는 유지하되 rawText는 빈 문자열로(요약 대체 금지)
                date_norm = normalize_date_string(row.get("api_postdate", ""))

                doc = {
                    "source": "blog",
                    "url": url,
                    "title": row.get("title", ""),
                    "rawText": "",                                   # 실패 시 빈 문자열
                    "author": row.get("api_author", ""),
                    "updatedAt": get_now_kst_iso(),
                }
                if date_norm:
                    doc["publishedAt"] = date_norm

                upsert_doc(url, doc)
                fail += 1
                print(f"  [FAIL] {url} -> {e}")

            time.sleep(delay_sec)

        print(f"[DONE] '{q}' → 성공 {ok} / 실패 {fail}")



# --------------- 실행 ---------------
if __name__ == "__main__":
    queries = ["묵호 여행"]
    run_pipeline_text_only(queries=queries, total_per_query=100, delay_sec=0.8)
