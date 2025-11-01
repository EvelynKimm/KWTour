import os
from typing import Dict, List, Tuple

from dotenv import load_dotenv
from google import genai
from google.cloud import firestore as gcfirestore
from google.cloud.firestore_v1.base_vector_query import DistanceMeasure, Vector
from google.genai import types as genai_types
from google.oauth2 import service_account

PROJECT_ID = "kwtourproject"
FIREBASE_CRED_PATH = "./firebase_key.json"
VECTOR_COLLECTION = "rawContents"
EMBED_DIM = 2048
TOP_K = 3


SYSTEM_PROMPT = """
너는 강원도 여행 일정을 계획해주는 플래너야.
- 말투: 친절하고 간결하게 존댓말. 불필요한 수식어 금지.
- 목표: 핵심만 빠르게 전달하는 일정 제안.

[타임라인 출력 규칙]
1) 시간순으로 제시해. (HH:MM 24시간 표기)
2) 하나의 장소, 활동은 하나의 블록으로 간주해.
3) 블록 형식: "[HH:MM] 아이콘 장소/활동 — 소요시간 n분/시간". - 여기서 ‘소요시간’은 ‘활동 체류시간’을 뜻해.
4) 이동은 별도 줄로 표기: "→ 🚎 이동(수단, n분, nkm)". - 이 줄의 시간은 ‘이동 소요시간’이야.
5) 동선 최적화: 역주행, 과도한 장거리 이동 지양, 인접 동선 우선. 같은 권역은 묶어서 배치해.
6) 영업시간/휴무 확인 후 계획에 반영해야해.

[아이콘 가이드(필요 시)]
- 식사: 아침🥣 점심🍜 저녁🍖 카페☕️
- 장소: 바다🏖️ 산⛰️ 호수🏞️ 박물관🏛️ 시장🧺 온천♨️ 소품샵🛍️ 사진📷
""".strip()

INPUT_TEMPLATE = """
[사용자 쿼리]
{user_query}

[검색 결과 Top {k}]
{ranked_chunks}

[지시사항]
- 사용자 일정에 맞춰 아침/점심/저녁, 이동 시간, 예상 소요 시간까지 포함해서 제안해줘.
- 사용자 취향(소품샵 방문 등)을 반영하되, 무리한 동선은 피하고 인접 동선을 우선시 해.
""".strip()


def embed_query_vec(
    client: genai.Client, query: str, dim: int = EMBED_DIM
) -> List[float]:
    cfg = genai_types.EmbedContentConfig(
        task_type="RETRIEVAL_QUERY", output_dimensionality=dim
    )
    resp = client.models.embed_content(
        model="gemini-embedding-001", contents=query, config=cfg
    )
    return resp.embeddings[0].values


def knn_search(
    db: gcfirestore.Client,
    query_vec: List[float],
    k: int = TOP_K,
    collection: str = VECTOR_COLLECTION,
) -> List[Tuple[str, Dict]]:

    docs = (
        db.collection(collection)
        .select(["rawText", "metadata"])
        .find_nearest(
            vector_field="embedding",
            query_vector=Vector(query_vec),
            limit=k,
            distance_measure=DistanceMeasure.COSINE,
            distance_result_field="_distance",
        )
        .get()
    )
    out = []
    for s in docs:
        d = s.to_dict() or {}
        out.append((s.id, d))
    return out


def format_chunks_for_prompt(
    results: List[Tuple[str, Dict]], max_chars_per_chunk: int = 600
) -> str:

    lines = []
    for rank, (doc_id, d) in enumerate(results, 1):
        txt = (d.get("rawText") or "").replace("\u200b", "").strip()
        if len(txt) > max_chars_per_chunk:
            txt = txt[:max_chars_per_chunk].rstrip() + " ..."
        md = d.get("metadata") or {}
        meta_brief = {
            k: md[k]
            for k in ("source", "publishedAt", "updatedAt", "contentId", "chunkIndex")
            if k in md
        }
        lines.append(
            f"[Rank {rank}] id={doc_id}\n" f"- meta: {meta_brief}\n" f"- text: {txt}"
        )
    return "\n\n".join(lines)


def generate_itinerary(
    client: genai.Client,
    system_prompt: str,
    user_query: str,
    ranked_chunks_block: str,
    model_name: str = "gemini-2.5-flash",
) -> str:
    user_input = INPUT_TEMPLATE.format(
        user_query=user_query, k=TOP_K, ranked_chunks=ranked_chunks_block
    )

    resp = client.models.generate_content(
        model=model_name,
        contents=user_input,
        config=genai_types.GenerateContentConfig(
            system_instruction=[system_prompt],
            max_output_tokens=4096,
            temperature=0.3,
        ),
    )
    return resp.text or ""


def main():
    load_dotenv()
    api_key = os.getenv("GOOGLE_API_KEY")

    creds = service_account.Credentials.from_service_account_file(FIREBASE_CRED_PATH)
    db = gcfirestore.Client(project=PROJECT_ID, credentials=creds)
    client = genai.Client(api_key=api_key)

    user_query = "혼자 동해여행을 2일간 가려고 해. 어떤 코스로 가는 게 좋을까? 소품샵도 들러서 예쁜 소품도 사고 싶어."

    qvec = embed_query_vec(client, user_query, EMBED_DIM)
    results = knn_search(db, qvec, TOP_K, VECTOR_COLLECTION)

    ranked_chunks_block = format_chunks_for_prompt(results, max_chars_per_chunk=700)

    plan = generate_itinerary(client, SYSTEM_PROMPT, user_query, ranked_chunks_block)

    print("\n================= 여행 루트 제안 =================\n")
    print(plan)


if __name__ == "__main__":
    main()
