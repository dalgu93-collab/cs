import streamlit as st
import pandas as pd
import json
import os
import re
import time
import requests
from datetime import datetime
from difflib import SequenceMatcher
from google import genai

# --------------------------------------------------
# 0. 페이지 기본 설정 및 로고(아이콘) 적용
# --------------------------------------------------
st.set_page_config(
    page_title="상담 표준 스크립트 시스템",
    page_icon="🎧",
    layout="centered"
)

# --------------------------------------------------
# 1. 기본 환경 설정
# --------------------------------------------------
GEMINI_API_KEY = "AQ.Ab8RN6J063aXfTZzahy3tscKuu3MtvNGhaDnV6CXKCzcoWHLug"
GOOGLE_SHEET_URL = "https://docs.google.com/spreadsheets/d/1Cq55CkPIxgMxeZ6qE6eYaXkNdirbnLX3goWbrEb91x4/edit?usp=sharing"
DEFAULT_WEBAPP_URL = "https://script.google.com/macros/s/AKfycbxlHyQqRhF-8sa-XsNwW-EPD-smaaYX32Y0CT352uXPGp2eyVpK2kTsbe3INtYdy8TfDA/exec"

CONFIG_FILE = "SheetConfig.json"
SHARED_PASSWORDS = ["1234", "9999"]  # 전 직원 공용 비밀번호

def get_webapp_url():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f).get("webapp_url", DEFAULT_WEBAPP_URL)
        except Exception:
            return DEFAULT_WEBAPP_URL
    return DEFAULT_WEBAPP_URL

def save_webapp_url(url):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump({"webapp_url": url.strip()}, f, ensure_ascii=False)

# --------------------------------------------------
# 2. 구글 스프레드시트 실시간 데이터 불러오기
# --------------------------------------------------
def load_data_from_sheet(sheet_url):
    try:
        pattern = r"/d/([a-zA-Z0-9-_]+)"
        match = re.search(pattern, sheet_url)
        if not match:
            return []
        
        sheet_id = match.group(1)
        csv_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&nocache={int(time.time())}"
        
        df = pd.read_csv(csv_url, dtype=str)
        df = df.fillna("")
        
        raw_records = df.to_dict(orient="records")
        valid_scripts = []
        for r in raw_records:
            row_id = str(r.get("id", "")).strip()
            row_title = str(r.get("title", "")).strip()
            if not row_id or row_id.upper() in ["FALSE", "TRUE", "□"]:
                continue
            if not row_title or row_title.upper() in ["FALSE", "TRUE", "□"]:
                continue
            valid_scripts.append(r)
            
        return valid_scripts
    except Exception as e:
        st.error(f"구글 시트를 읽어오지 못했습니다: {e}")
        return []

# --------------------------------------------------
# 3. 한국어 조사 제거 및 AI 키워드/카테고리 자동 분류기
#    (질문 원문은 자르지 않고 100% 그대로 보존)
# --------------------------------------------------
def smart_korean_fallback(question, answer):
    combined = f"{question} {answer}"
    
    if any(k in combined for k in ["계산서", "지출증빙", "영수증", "사업자", "부가세", "세금"]):
        category = "세금계산서/증빙"
    elif any(k in combined for k in ["연동", "배민", "배달의민족", "요기요", "쿠팡", "포스", "체크", "롯데리아", "버거킹", "맥도날드"]):
        category = "배달앱/연동"
    elif any(k in combined for k in ["실행", "작업관리자", "강제종료", "재시작", "설치", "프린트", "출력", "hhd", "오류", "에러"]):
        category = "프로그램/오류"
    elif any(k in combined for k in ["라이더", "기사", "배차", "단말기", "이관", "지사"]):
        category = "라이더/계정관리"
    elif any(k in combined for k in ["정산", "입금", "출금", "캐시", "수수료", "포인트"]):
        category = "정산/결제"
    else:
        category = "일반상담"

    raw_words = re.findall(r"[a-zA-Z0-9가-힣]{2,}", combined)
    suffixes = [
        "이라는데", "이래요", "돼있대요", "되나요", "돼나요", "인가요", "건가요",
        "주시고", "주시거나", "해주시겠어요", "해보시겠어요", "하시고", "하세요",
        "에서", "으로", "에게", "까지", "부터", "이랑", "하고",
        "은", "는", "이", "가", "을", "를", "에", "의", "로", "도", "만", "요"
    ]
    stopwords = {"어떻게", "무엇", "뭐에요", "맞아요", "현재", "경우", "또는", "후에", "다시", "있으면", "없는", "저희", "별도로", "해주실"}

    cleaned_keywords = []
    for w in raw_words:
        orig = w
        for suf in suffixes:
            if len(w) > len(suf) + 1 and w.endswith(suf):
                w = w[:-len(suf)]
                break
        if len(w) >= 2 and w not in stopwords:
            cleaned_keywords.append(w)
        if orig != w and len(orig) >= 2 and orig not in stopwords:
            cleaned_keywords.append(orig)

    if "배민" in combined or "배달의민족" in combined:
        cleaned_keywords.extend(["배민", "배달의민족", "연동"])
    if "실행중" in combined or "작업관리자" in combined:
        cleaned_keywords.extend(["중복실행", "이미실행중", "켜지지않음", "강제종료"])
    if "계산서" in combined or "지출증빙" in combined:
        cleaned_keywords.extend(["세금계산서", "지출증빙", "사업자명", "발행처"])

    unique_kw = list(dict.fromkeys(cleaned_keywords))[:15]

    # 질문 내용을 절대 자르지 않고 원문 전체 사용
    full_question = " ".join(question.strip().splitlines())

    return {
        "title": full_question,
        "category": category,
        "keywords": ", ".join(unique_kw)
    }

def analyze_and_classify_script(question, answer):
    full_question = " ".join(question.strip().splitlines())
    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        prompt = f"""
        당신은 고객센터 상담 지식 데이터 관리자입니다.
        아래 [질문 내용]과 [답변 내용]을 분석하여 순수 JSON으로만 출력하세요.

        [질문 내용]
        {question}

        [답변 내용]
        {answer}

        [반드시 지켜야 할 출력 형식 (순수 JSON)]
        {{
            "category": "업무 분류 (예: 프로그램/오류, 배달앱/연동, 세금계산서/증빙, 라이더/관리, 정산/결제 등)",
            "keywords": "상담원이 검색창에 칠 만한 핵심 명사, 동의어, 축약어를 조사 없이 쉼표로 12개 나열"
        }}
        """
        res = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
        clean_text = res.text.strip().replace("```json", "").replace("```", "")
        parsed = json.loads(clean_text)
        if parsed.get("keywords"):
            return {
                "title": full_question,  # 요약하지 않고 원본 질문 전체 보존
                "category": parsed.get("category", "일반상담"),
                "keywords": parsed.get("keywords", "")
            }
    except Exception:
        pass

    return smart_korean_fallback(question, answer)

# --------------------------------------------------
# 4. 다중 키워드(띄어쓰기 / 쉼표) 최적화 매칭 엔진
# --------------------------------------------------
def calculate_match_score(query, script):
    raw_tokens = [re.sub(r"[^\w]", "", t).strip().lower() for t in re.split(r"[,\s]+", query) if t.strip()]
    query_tokens = [t for t in raw_tokens if len(t) >= 1]
    
    if not query_tokens:
        return 0, []

    keywords_list = [re.sub(r"[^\w]", "", k).strip().lower() for k in str(script.get("keywords", "")).split(",") if k.strip()]
    title = str(script.get("title", "")).lower()
    category = str(script.get("category", "")).lower()
    text_body = str(script.get("text", "")).lower()

    score = 0
    matched_tokens = []

    for token in query_tokens:
        token_hit = False

        for kw in keywords_list:
            if not kw:
                continue
            if token == kw:
                score += 45
                token_hit = True
            elif len(token) >= 2 and (token in kw or kw in token):
                score += 25
                token_hit = True

        if token in title:
            score += 35
            token_hit = True
        if token in category:
            score += 20
            token_hit = True
        if len(token) >= 2 and token in text_body:
            score += 15
            token_hit = True

        if token_hit:
            matched_tokens.append(token)

    unique_matched = list(dict.fromkeys(matched_tokens))
    match_count = len(unique_matched)
    
    if match_count > 1:
        score += (match_count ** 2) * 30
    
    if len(query_tokens) > 1 and match_count == len(set(query_tokens)):
        score += 100

    clean_query_full = " ".join(query_tokens)
    ratio = SequenceMatcher(None, clean_query_full, title).ratio()
    score += ratio * 20

    return score, unique_matched

# --------------------------------------------------
# 5. 로그인 화면 (공용 비밀번호 + 담당자 이름 입력)
# --------------------------------------------------
if "logged_in" not in st.session_state:
    st.session_state.logged_in = False
if "user_name" not in st.session_state:
    st.session_state.user_name = ""

if not st.session_state.logged_in:
    st.title("🎧 상담 표준 스크립트 시스템")
    st.caption("등록자·수정자 자동 기록을 위해 본인 이름(또는 닉네임)과 공용 비밀번호를 입력하세요.")
    with st.form("login_box"):
        u = st.text_input("담당자 이름 (등록자·수정자로 자동 기록됩니다)", placeholder="예: 강성구")
        p = st.text_input("공용 비밀번호", type="password", placeholder="1234")
        if st.form_submit_button("로그인", type="primary", use_container_width=True):
            if not u.strip():
                st.warning("담당자 이름을 입력해 주세요.")
            elif p in SHARED_PASSWORDS:
                st.session_state.logged_in = True
                st.session_state.user_name = u.strip()
                st.rerun()
            else:
                st.error("공용 비밀번호가 일치하지 않습니다.")
    st.stop()

# --------------------------------------------------
# 6. 메인 상담 화면
# --------------------------------------------------
current_webapp_url = get_webapp_url()

col_top, col_btn = st.columns([8, 2])
with col_top:
    st.title("🎧 CS 프로그램")
    st.caption(f"현재 접속자: **{st.session_state.user_name}** 님 (등록·수정 시 이름이 자동 기록됩니다)")
with col_btn:
    if st.button("로그아웃", use_container_width=True):
        st.session_state.logged_in = False
        st.rerun()

scripts = load_data_from_sheet(GOOGLE_SHEET_URL)

c_stat, c_refresh = st.columns([8, 2])
with c_stat:
    st.info(f"📊 현재 구글 시트에 등록된 스크립트: 총 **{len(scripts)}**건")
with c_refresh:
    if st.button("🔄 시트 새로고침", use_container_width=True):
        st.rerun()

tab_search, tab_add, tab_edit = st.tabs([
    "🔍 다중 키워드 검색", 
    "➕ 새 스크립트 AI 등록", 
    "✏️ 기존 스크립트 수정/관리"
])

# ==================================================
# [탭 1: 다중 키워드 검색 & 즉시 수정]
# ==================================================
with tab_search:
    user_query = st.text_input(
        "키워드를 띄어쓰기나 쉼표(,)로 구분해 여러 개 입력하세요",
        placeholder="예: 롯데리아, 배민, 요기요  또는  프로그램 실행중 작업관리자",
        key="search_input_box"
    )

    if st.button("가장 알맞은 답변 찾기", type="primary", use_container_width=True) or user_query.strip():
        if not user_query.strip():
            st.warning("검색어를 입력해 주세요.")
        elif not scripts:
            st.warning("현재 구글 시트에 등록된 스크립트가 없습니다. [새 스크립트 AI 등록] 탭에서 먼저 등록해 주세요.")
        else:
            scored_list = []
            for item in scripts:
                pts, matched_kws = calculate_match_score(user_query, item)
                if pts > 5:
                    scored_list.append((pts, matched_kws, item))
            
            scored_list.sort(key=lambda x: x[0], reverse=True)

            st.divider()

            if not scored_list:
                st.warning("일치하는 스크립트를 찾지 못했습니다. 다른 키워드로 검색해 보세요.")
            else:
                top_score, top_matched, top_item = scored_list[0]
                author_info = top_item.get("author", "") or "미기재"
                modifier_info = top_item.get("modifier", "") or "-"

                # 질문 내용 전체가 한눈에 보이도록 출력
                st.success(f"🎯 **[1순위 최적 매칭]** (분류: {top_item.get('category', '일반')})\n\n**❓ 대리점 질문 내용:** {top_item.get('title')}")
                st.caption(
                    f"✅ **일치한 검색 키워드({len(top_matched)}개):** `{', '.join(top_matched)}` | "
                    f"✍️ **등록자:** {author_info} | 🛠️ **최종 수정자:** {modifier_info}"
                )
                st.text_area(
                    "💬 대리점 전달용 답변 본문 (복사해서 사용하세요)", 
                    value=top_item.get("text", ""), 
                    height=110, 
                    key=f"top_res_{top_item.get('id')}"
                )

                # 검색 결과 화면에서 바로 수정하는 기능
                with st.expander("✏️ 이 질문/답변이 잘렸거나 수정이 필요한가요? (여기서 바로 수정 후 시트 반영)"):
                    with st.form(f"quick_edit_{top_item.get('id')}"):
                        edit_title = st.text_area("대리점 질문 내용 (전체 문장)", value=top_item.get("title", ""), height=70)
                        edit_cat = st.text_input("카테고리", value=top_item.get("category", ""))
                        edit_kw = st.text_input("검색 키워드 (쉼표로 구분)", value=top_item.get("keywords", ""))
                        edit_text = st.text_area("답변 본문 수정", value=top_item.get("text", ""), height=100)
                        
                        if st.form_submit_button("💾 수정 내용 구글 시트에 즉시 덮어쓰기", type="primary", use_container_width=True):
                            now_str = datetime.now().strftime("%m/%d %H:%M")
                            modifier_stamp = f"{st.session_state.user_name} ({now_str})"
                            payload = {
                                "action": "edit",
                                "id": top_item.get("id"),
                                "category": edit_cat.strip(),
                                "title": " ".join(edit_title.strip().splitlines()),
                                "keywords": edit_kw.strip(),
                                "text": edit_text.strip(),
                                "modifier": modifier_stamp
                            }
                            with st.spinner("구글 시트의 기존 내용을 수정하는 중..."):
                                resp = requests.get(current_webapp_url, params=payload, allow_redirects=True, timeout=15)
                                if resp.status_code == 200 and '"result":"success"' in resp.text.replace(" ", ""):
                                    st.success(f"✅ 수정 완료! (수정자: {modifier_stamp}) 시트에 즉시 반영되었습니다.")
                                    time.sleep(1.2)
                                    st.rerun()
                                else:
                                    st.error("시트 반영 실패! Apps Script 웹 앱 URL을 확인해 주세요.")

                # 2순위, 3순위 관련 대안 표시
                if len(scored_list) > 1:
                    st.markdown("---")
                    st.caption("💡 **함께 참고할 만한 후보 질문/답변**")
                    for idx, (s_pts, s_matched, s_item) in enumerate(scored_list[1:4], start=2):
                        with st.expander(f"[{idx}순위] ❓ 질문: {s_item.get('title')} (일치 키워드: {', '.join(s_matched)})"):
                            st.caption(f"분류: {s_item.get('category')} | 등록자: {s_item.get('author', '-')} | 수정자: {s_item.get('modifier', '-')}")
                            st.text_area("답변 내용", value=s_item.get("text", ""), height=80, key=f"sub_{idx}_{s_item.get('id')}")

# ==================================================
# [탭 2: 새 스크립트 AI 등록 (등록자 자동 기입)]
# ==================================================
with tab_add:
    st.subheader("⚡ 새 스크립트 AI 자동 분류 및 시트 저장")
    st.caption(f"입력하신 질문 원문 전체가 그대로 저장되며, 작성자(**{st.session_state.user_name}**)가 자동 기록됩니다.")

    with st.expander("⚙️ Apps Script 웹 앱 URL 설정 확인 / 변경"):
        new_url_input = st.text_input("웹 앱 URL (`https://script.google.com/macros/s/.../exec`)", value=current_webapp_url)
        if st.button("웹 앱 URL 저장"):
            save_webapp_url(new_url_input)
            st.success("새 웹 앱 URL이 저장되었습니다!")
            st.rerun()

    with st.form("auto_add_form"):
        input_q = st.text_area("1) 대리점이 자주 묻는 질문 내용 (적으신 문장 전체가 그대로 표시됩니다)", placeholder="예: 롯데리아 배민 요기요 신규로 되는데 포스 연동 따로 해야 하나요?", height=70)
        input_a = st.text_area("2) 대리점에 안내할 최종 확정 답변 본문", placeholder="예: 기존 롯데리아 포스가 저희 프로그램과 연동되어있다면 별도로 해주실 작업은 없습니다.", height=90)
        custom_kw = st.text_input("3) [선택] 추가 키워드 (비워두면 AI가 알아서 추출합니다)", placeholder="예: 롯데리아, 배민, 요기요, 신규연동")
        
        save_btn = st.form_submit_button("🚀 AI 자동 분류 후 구글 시트에 등록하기", type="primary", use_container_width=True)
        
        if save_btn:
            if not input_q.strip() or not input_a.strip():
                st.warning("질문 내용과 답변 본문을 모두 입력해 주세요.")
            else:
                with st.spinner("AI 분류 및 구글 시트에 저장하는 중..."):
                    ai_res = analyze_and_classify_script(input_q, input_a)
                    
                    max_num = len(scripts)
                    for s in scripts:
                        m = re.search(r"(\d+)", str(s.get("id", "")))
                        if m and int(m.group(1)) > max_num:
                            max_num = int(m.group(1))
                    new_id = f"SCR_{max_num + 1:02d}"
                    
                    final_keywords = ai_res.get("keywords", "")
                    if custom_kw.strip():
                        final_keywords = f"{custom_kw.strip()}, {final_keywords}"

                    now_str = datetime.now().strftime("%m/%d %H:%M")
                    author_stamp = f"{st.session_state.user_name} ({now_str})"

                    payload = {
                        "action": "add",
                        "id": new_id,
                        "category": ai_res.get("category", "일반"),
                        "title": ai_res.get("title", ""),  # 질문 원문 전체
                        "keywords": final_keywords,
                        "text": input_a.strip(),
                        "author": author_stamp,
                        "modifier": "-"
                    }
                    
                    try:
                        resp = requests.get(current_webapp_url, params=payload, allow_redirects=True, timeout=15)
                        if resp.status_code == 200 and '"result":"success"' in resp.text.replace(" ", ""):
                            st.success(f"✅ 구글 시트 등록 완료! [{new_id}] {payload['title']} (등록자: {author_stamp})")
                            time.sleep(1.2)
                            st.rerun()
                        else:
                            st.error("시트 저장이 거부되었습니다. 웹 앱 URL 설정을 확인해 주세요.")
                    except Exception as e:
                        st.error(f"전송 중 오류가 발생했습니다: {e}")

# ==================================================
# [탭 3: 전체 스크립트 목록 조회 및 선택 수정]
# ==================================================
with tab_edit:
    st.subheader("✏️ 등록된 스크립트 선택 수정 및 전체 목록")
    if not scripts:
        st.info("등록된 스크립트가 없습니다.")
    else:
        script_options = {f"[{s.get('id')}] ({s.get('category')}) {s.get('title')}": s for s in scripts}
        selected_label = st.selectbox("수정할 스크립트를 선택하세요", options=list(script_options.keys()))
        selected_item = script_options[selected_label]

        with st.form("tab_edit_form"):
            st.caption(f"최초 등록자: **{selected_item.get('author', '미기재')}** | 직전 수정자: **{selected_item.get('modifier', '-')}**")
            e_title = st.text_area("대리점 질문 내용 (전체 문장)", value=selected_item.get("title", ""), height=70)
            e_cat = st.text_input("카테고리", value=selected_item.get("category", ""))
            e_kw = st.text_input("키워드 (쉼표 또는 띄어쓰기로 구분)", value=selected_item.get("keywords", ""))
            e_text = st.text_area("답변 본문", value=selected_item.get("text", ""), height=110)

            if st.form_submit_button("💾 선택한 스크립트 수정 완료 (구글 시트 반영)", type="primary", use_container_width=True):
                now_str = datetime.now().strftime("%m/%d %H:%M")
                modifier_stamp = f"{st.session_state.user_name} ({now_str})"
                payload = {
                    "action": "edit",
                    "id": selected_item.get("id"),
                    "category": e_cat.strip(),
                    "title": " ".join(e_title.strip().splitlines()),
                    "keywords": e_kw.strip(),
                    "text": e_text.strip(),
                    "modifier": modifier_stamp
                }
                with st.spinner("구글 시트에 수정사항을 반영하는 중..."):
                    resp = requests.get(current_webapp_url, params=payload, allow_redirects=True, timeout=15)
                    if resp.status_code == 200 and '"result":"success"' in resp.text.replace(" ", ""):
                        st.success(f"✅ [{selected_item.get('id')}] 수정 사항이 구글 시트에 반영되었습니다! (수정자: {modifier_stamp})")
                        time.sleep(1.2)
                        st.rerun()
                    else:
                        st.error("수정에 실패했습니다.")

        st.divider()
        st.caption("📋 **구글 시트 전체 데이터 표**")
        st.dataframe(pd.DataFrame(scripts), use_container_width=True)