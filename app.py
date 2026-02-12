import os
import re
import json
import datetime
from typing import List, Dict, Any, Tuple, Optional

import streamlit as st
import google.generativeai as genai

from basic_setting import BasicSetting

# =========================
# Config
# =========================
DEFAULT_MODEL = "gemini-3-pro-preview"
GEMINI_API_KEY = os.getenv("CX_GEMINI_API_KEY", "")

TABLE_IMP = "cx-avod-am360.200_gam_data.Impressions"
TABLE_VC  = "cx-avod-am360.200_gam_data.VideoConversions"

# Forbidden SQL tokens (case-insensitive)
FORBIDDEN_PATTERNS = [
    r"\bDELETE\b", r"\bUPDATE\b", r"\bMERGE\b", r"\bINSERT\b",
    r"\bCREATE\b", r"\bDROP\b", r"\bALTER\b", r"\bTRUNCATE\b",
    r"\bRENAME\b", r"\bGRANT\b", r"\bREVOKE\b", r"\bCALL\b",
    r"\bEXECUTE\b", r"\bBEGIN\b", r"\bCOMMIT\b", r"\bROLLBACK\b",
]
FORBIDDEN_RE = re.compile("|".join(FORBIDDEN_PATTERNS), re.IGNORECASE)

# Must be SELECT-ish only
ALLOWED_START_RE = re.compile(r"^\s*(WITH\b|SELECT\b)", re.IGNORECASE)

# Require suid filter
REQUIRE_SUID_RE = re.compile(r"\bsuid\s+IS\s+NOT\s+NULL\b", re.IGNORECASE)


# =========================
# Helpers
# =========================
def parse_ids(raw: str) -> List[int]:
    # Accept comma/space/newline separated numeric IDs only
    tokens = re.split(r"[,\s]+", raw.strip())
    ids = []
    for t in tokens:
        if not t:
            continue
        if not re.fullmatch(r"\d+", t):
            raise ValueError(f"IDは数字のみ対応です: {t}")
        ids.append(int(t))
    if not ids:
        raise ValueError("IDが空です。")
    return ids


def safety_check_sql(sql: str) -> Tuple[bool, str]:
    if not ALLOWED_START_RE.search(sql):
        return False, "SQLはWITH/SELECTから始まる必要があります（非SELECT系は禁止）。"

    if FORBIDDEN_RE.search(sql):
        return False, "禁止コマンド（削除/更新/DDL等）が含まれています。"

    if not REQUIRE_SUID_RE.search(sql):
        return False, "必須条件 `suid IS NOT NULL` がSQL内に見つかりません。"

    return True, ""


def build_context(schema_text_imp: str, schema_text_vc: str, codebook: Dict[str, List[Tuple[str, str]]]) -> str:
    # Geminiに渡す「仕様・制約」を強く固定
    codebook_lines = []
    if codebook:
        for field in sorted(codebook.keys()):
            mapping = ", ".join([f"{k}={v}" for k, v in codebook[field]])
            codebook_lines.append(f"- {field}: {mapping}")
    codebook_block = ""
    if codebook_lines:
        codebook_block = "\n# コード値マッピング（抜粋）\n" + "\n".join(codebook_lines)

    return f"""
あなたはBigQuery(Standard SQL)のクエリ作成アシスタントです。目的は「ユーザー要望を満たす安全なSELECTクエリ」を生成することです。

# 絶対ルール（最優先）
- 生成するSQLは必ずSELECTまたはWITHから開始すること（DDL/DML禁止）。
- DELETE/UPDATE/MERGE/INSERT/CREATE/DROP/ALTER/TRUNCATE/GRANT/REVOKE/CALL/EXECUTE/TRANSACTION系は禁止。
- `suid IS NOT NULL` は必須条件。WHERE句に必ず含めること（両テーブルでも同様）。
- BigQuery Standard SQLで書くこと。

# 対象テーブル
1) `{TABLE_IMP}`
{schema_text_imp}

2) `{TABLE_VC}`
{schema_text_vc}

# 社内定義
- UB(reach) = COUNT(DISTINCT suid)  ※cookie/UserId/ifa/ppidではなくsuidが正
- 完再生率 = complete / start （イベントカウント、distinct無し）
  - start = COUNTIF(ActionCode = 2)
  - complete = COUNTIF(ActionCode = 6)
  - completion_rate = SAFE_DIVIDE(complete, start)

# パラメータ方針（必須）
- 日付は必ず `TimeDate BETWEEN @start_date AND @end_date` を使う（DATE型パラメータ）
- IDはユーザーが選んだ種別1つのみ。`<FieldName> IN UNNEST(@ids)` を使う（ARRAY<INT64>）
  - OrderId / LineItemId / CreativeId のいずれか1つだけ

# コード値は可能なら自然言語ラベルも出す
- スキーマ説明にコード値があるカラムは、`CASE` でラベル列（例: `gender_label`）を追加して可読性を上げる
- 例: `CASE gender WHEN 1 THEN '男' WHEN 2 THEN '女' WHEN 9 THEN 'その他' WHEN 0 THEN '回答しない' WHEN -1 THEN 'optout' ELSE NULL END AS gender_label`

# 出力フォーマット（厳守）
あなたの返答は必ずJSONのみ（余計な文章禁止）。次のどちらか：
1) 不明点があり追加質問したい場合:
{{"type":"question","question":"(質問文)"}}
2) SQLが確定した場合:
{{"type":"sql","sql":"(SQL本文)","notes":"(短い補足。なければ空文字)"}}

質問が必要なときは、最小の質問数で要件を確定させること。
{codebook_block}
""".strip()


def df_to_schema_text(df) -> str:
    # userが渡してきたスキーマCSV（field name, mode, type, description）から
    # Geminiに見せる用に整形
    lines = []
    for _, r in df.iterrows():
        name = str(r["field name"])
        typ = str(r["type"])
        mode = str(r["mode"])
        desc = str(r.get("description", "")).replace("\n", " ")
        lines.append(f"- {name} ({typ}, {mode}) : {desc}")
    return "\n".join(lines)


CODE_PAIR_RE = re.compile(r"(?P<key>null|-?\d+)\s*[:：]\s*(?P<label>[^,、/。]+)")


def extract_codebook(df, max_entries: int = 20) -> Dict[str, List[Tuple[str, str]]]:
    book: Dict[str, List[Tuple[str, str]]] = {}
    for _, r in df.iterrows():
        field = str(r["field name"])
        desc = str(r.get("description", "") or "")
        if not desc or desc == "nan":
            continue
        pairs = []
        for m in CODE_PAIR_RE.finditer(desc):
            key = m.group("key").strip()
            label = m.group("label").strip()
            if not key or not label:
                continue
            pairs.append((key, label))
        if 2 <= len(pairs) <= max_entries:
            seen = set()
            deduped = []
            for k, v in pairs:
                if (k, v) in seen:
                    continue
                seen.add((k, v))
                deduped.append((k, v))
            book[field] = deduped
    return book


def format_gemini_error(err: Exception, model_name: str) -> str:
    msg = str(err)
    retry = ""
    m = re.search(r"retry in ([0-9.]+)s", msg, re.IGNORECASE)
    if m:
        retry = f"（再試行目安: {m.group(1)}秒後）"
    if "429" in msg or "Quota exceeded" in msg:
        return (
            "Gemini API のクォータ上限に達しました。"
            f"モデル: {model_name} {retry}\n"
            "対策: 1) 別モデルに切替 2) 課金/プランを確認 3) 少し待って再試行"
        )
    return f"Gemini呼び出しに失敗: {msg}"


def render_sql_with_params(sql: str, start_date, end_date, ids: List[int]) -> str:
    out = sql
    if start_date:
        out = re.sub(r"@start_date\b", f"DATE '{start_date}'", out, flags=re.IGNORECASE)
    if end_date:
        out = re.sub(r"@end_date\b", f"DATE '{end_date}'", out, flags=re.IGNORECASE)
    if ids:
        arr = "[" + ", ".join(str(i) for i in ids) + "]"
        out = re.sub(r"@ids\b", arr, out, flags=re.IGNORECASE)
    return out


def extract_code_notes(sql: str, codebook: Dict[str, List[Tuple[str, str]]]) -> List[str]:
    notes = []
    for field, pairs in codebook.items():
        if re.search(rf"\\b{re.escape(field)}\\b", sql):
            mapping = ", ".join([f"{k}={v}" for k, v in pairs])
            notes.append(f"{field}: {mapping}")
    return notes


def add_history_entry(
    history: List[Dict[str, object]],
    *,
    request_text: str,
    start_date,
    end_date,
    id_type: str,
    ids: List[int],
    model_name: str,
    sql: str,
    notes: str,
) -> None:
    rendered_sql = render_sql_with_params(sql, start_date, end_date, ids)
    entry = {
        "id": f"hist_{datetime.datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{len(history)+1}",
        "created_at": datetime.datetime.utcnow().isoformat() + "Z",
        "request": request_text,
        "start_date": str(start_date) if start_date else "",
        "end_date": str(end_date) if end_date else "",
        "id_type": id_type,
        "ids": ids,
        "model": model_name,
        "sql": sql,
        "sql_rendered": rendered_sql,
        "notes": notes,
    }
    history.append(entry)


def render_history_panel(history: List[Dict[str, object]]) -> None:
    if not history:
        return
    with st.expander("履歴", expanded=False):
        for entry in reversed(history[-20:]):
            created_at = entry.get("created_at", "")
            request_text = entry.get("request", "")
            id_type = entry.get("id_type", "")
            ids = entry.get("ids", [])
            date_range = f"{entry.get('start_date','')} ~ {entry.get('end_date','')}"
            model_name = entry.get("model", "")
            st.markdown(f"日時: {created_at}")
            if request_text:
                st.markdown(f"要望: {request_text}")
            if id_type and ids:
                st.markdown(f"{id_type}: {', '.join(str(i) for i in ids)}")
            if date_range.strip(" ~"):
                st.markdown(f"期間: {date_range}")
            if model_name:
                st.markdown(f"モデル: {model_name}")
            sql_text = entry.get("sql_rendered") or entry.get("sql") or ""
            if sql_text:
                st.code(sql_text, language="sql")
            notes = entry.get("notes") or ""
            if notes:
                st.markdown(f"補足: {notes}")
            st.divider()


def _try_parse_json(text: str) -> Optional[Dict[str, Any]]:
    t = (text or "").strip()
    if not t:
        return None
    try:
        return json.loads(t)
    except Exception:
        pass
    # Extract first JSON object if extra text exists
    start = t.find("{")
    end = t.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(t[start : end + 1])
        except Exception:
            return None
    return None


def gemini_call(model, system_context: str, messages: List[Dict[str, str]]) -> Dict[str, Any]:
    # messages: [{"role":"user"/"assistant","content":"..."}]
    # google.generativeai は system_instruction を指定できる
    chat = model.start_chat(history=[])
    # systemは最初のuserに含めるより system_instruction を推奨
    # ただし start_chat(history=...) に systemを入れられないので generate_content 形式にする
    # ここでは "system_context + 会話" をまとめて投げる簡易版
    convo_text = []
    convo_text.append("## SYSTEM\n" + system_context)
    for m in messages:
        role = m["role"].upper()
        convo_text.append(f"## {role}\n{m['content']}")
    prompt = "\n\n".join(convo_text)

    resp = model.generate_content(prompt)
    text = getattr(resp, "text", "")

    parsed = _try_parse_json(text)
    if parsed is not None:
        return parsed

    # JSON以外/空が返ったら、強制的にJSON再出力させる
    reprompt = f"""
返答がJSONではありませんでした。必ずJSONのみで返してください。
直前の返答:
{(text or "").strip()}
"""
    resp2 = model.generate_content(prompt + "\n\n" + reprompt)
    text2 = getattr(resp2, "text", "")
    parsed2 = _try_parse_json(text2)
    if parsed2 is not None:
        return parsed2

    raise ValueError(
        "AIの返答が空、またはJSONとして解釈できませんでした。"
        "モデルを変更するか、少し待って再試行してください。"
    )


# =========================
# UI
# =========================
st.set_page_config(
    page_title="BQ SQL Generator",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# Hide sidebar UI completely
st.markdown(
    """
<style>
[data-testid="stSidebar"] {display: none;}
[data-testid="stSidebarNav"] {display: none;}
</style>
""",
    unsafe_allow_html=True,
)

basic = BasicSetting(history_state_key="history", history_format="raw", login_title="BQ SQL Generator Login")
basic.sync_cookie_controller()
basic.require_login()
basic.init_history()

st.title("BigQuery SQL Generator")

api_key = GEMINI_API_KEY
model_name = DEFAULT_MODEL

if st.button("ログアウト"):
    basic.logout()

# Load schema from CSVs (local or /mnt/data)
def resolve_schema_path(filename: str) -> str:
    candidates = []
    schema_dir = os.getenv("SCHEMA_DIR", "").strip()
    if schema_dir:
        candidates.append(os.path.join(schema_dir, filename))
    candidates.append(os.path.join(os.getcwd(), filename))
    candidates.append(os.path.join("/mnt/data", filename))
    for p in candidates:
        if os.path.exists(p):
            return p
    raise FileNotFoundError(
        f"スキーマCSVが見つかりません: {filename} (SCHEMA_DIR or CWD or /mnt/data を確認)"
    )

IMP_SCHEMA_CSV = resolve_schema_path("imp.csv")
VC_SCHEMA_CSV  = resolve_schema_path("vc.csv")

import pandas as pd
imp_schema_df = pd.read_csv(IMP_SCHEMA_CSV)
vc_schema_df  = pd.read_csv(VC_SCHEMA_CSV)

schema_text_imp = df_to_schema_text(imp_schema_df)
schema_text_vc  = df_to_schema_text(vc_schema_df)

codebook = extract_codebook(imp_schema_df)
codebook.update(extract_codebook(vc_schema_df))
system_context = build_context(schema_text_imp, schema_text_vc, codebook)

# Inputs
col1, col2 = st.columns(2)

with col1:
    st.subheader("条件")
    date_range = st.date_input("日付範囲 (TimeDate)", [])
    if isinstance(date_range, (list, tuple)) and len(date_range) == 2:
        start_date, end_date = date_range
    else:
        start_date, end_date = None, None

    id_type = st.selectbox(
        "ID種別（どれか1つ）",
        ["OrderId", "LineItemId", "CreativeId"],
        index=0
    )
    ids_raw = st.text_area("ID（単一/複数OK、カンマ or 空白区切り）", placeholder="例: 3897550230, 3897550231")

with col2:
    st.subheader("やりたいこと（自然言語）")
    nl_request = st.text_area(
        "どんなクエリが欲しい？",
        height=180,
        placeholder="例: 日別のimpとUBとfrequencyを出したい。device_codeでも分けたい。完再生率もほしい。"
    )
    st.caption("不明点があればAIが質問します。回答して続けてください。")

if "chat_messages" not in st.session_state:
    st.session_state.chat_messages = []  # [{"role":"user"/"assistant","content": "..."}]

if "final_sql" not in st.session_state:
    st.session_state.final_sql = ""

if "final_notes" not in st.session_state:
    st.session_state.final_notes = ""

if "pending_question" not in st.session_state:
    st.session_state.pending_question = ""


def init_model():
    if not api_key:
        st.error("CX_GEMINI_API_KEY が未設定です（環境変数で設定してください）。")
        st.stop()
    genai.configure(api_key=api_key)
    return genai.GenerativeModel(model_name)


def build_user_packet() -> str:
    # Geminiに「必須の前提」を明示し、パラメータ・フィールド名も固定
    return f"""
ユーザー要望:
{nl_request}

必須パラメータ:
- @start_date (DATE) = {start_date}
- @end_date   (DATE) = {end_date}
- @ids (ARRAY<INT64>) = {ids_raw}

ID種別:
- {id_type}

必須条件:
- TimeDate BETWEEN @start_date AND @end_date
- {id_type} IN UNNEST(@ids)
- suid IS NOT NULL
""".strip()


st.divider()

# Show conversation
for m in st.session_state.chat_messages:
    with st.chat_message(m["role"]):
        st.markdown(m["content"])

# First generate / continue rally
colA, colB = st.columns([1, 2])

with colA:
    generate_btn = st.button("SQL生成/続行", type="primary")

with colB:
    user_answer = st.text_input("（AIの質問に回答）", value="")

if generate_btn:
    # Validate base inputs
    if not (start_date and end_date):
        st.error("日付範囲を2つ指定してください。")
        st.stop()
    if not nl_request.strip():
        st.error("自然言語の要望を入力してください。")
        st.stop()
    try:
        ids = parse_ids(ids_raw)
    except Exception as e:
        st.error(str(e))
        st.stop()

    model = init_model()

    # If we have a pending question, add the user's answer
    if st.session_state.pending_question:
        if not user_answer.strip():
            st.error("質問への回答を入力してください（または質問が不要なら最初からやり直し）。")
            st.stop()
        st.session_state.chat_messages.append({"role": "user", "content": f"質問への回答: {user_answer.strip()}"})
        st.session_state.pending_question = ""

    # If no messages yet, seed with full packet
    if not st.session_state.chat_messages:
        st.session_state.chat_messages.append({"role": "user", "content": build_user_packet()})
    else:
        # Add a short "continue" hint with current constraints to reduce drift
        st.session_state.chat_messages.append({
            "role": "user",
            "content": f"続けて。忘れずに: TimeDate BETWEEN @start_date AND @end_date / {id_type} IN UNNEST(@ids) / suid IS NOT NULL / SELECTのみ。"
        })

    # Call Gemini
    try:
        out = gemini_call(model, system_context, st.session_state.chat_messages)
    except Exception as e:
        st.error(format_gemini_error(e, model_name))
        st.stop()

    if out.get("type") == "question":
        q = out.get("question", "").strip()
        st.session_state.chat_messages.append({"role": "assistant", "content": q})
        st.session_state.pending_question = q
        st.rerun()

    if out.get("type") == "sql":
        sql = (out.get("sql") or "").strip()
        notes = (out.get("notes") or "").strip()

        ok, reason = safety_check_sql(sql)
        if not ok:
            st.session_state.chat_messages.append(
                {"role": "assistant", "content": f"生成SQLが安全要件を満たしませんでした: {reason}\n\nGeminiに修正を要求します。"}
            )
            # ask Gemini to fix
            st.session_state.chat_messages.append(
                {"role": "user", "content": f"修正して。理由: {reason}。安全要件を満たすSELECTクエリにして、JSON(type=sql)で返して。"}
            )
            st.rerun()

        st.session_state.final_sql = sql
        st.session_state.final_notes = notes
        if "history" not in st.session_state or not isinstance(st.session_state.history, list):
            st.session_state.history = []
        add_history_entry(
            st.session_state.history,
            request_text=nl_request.strip(),
            start_date=start_date,
            end_date=end_date,
            id_type=id_type,
            ids=ids,
            model_name=model_name,
            sql=sql,
            notes=notes,
        )
        basic.persist_history_to_storage()
        st.session_state.chat_messages.append({"role": "assistant", "content": "SQLが確定しました。下に出力します。"})
        st.rerun()

    st.error("不明な応答形式です（typeがquestion/sqlではない）。JSON形式での返答を強制してください。")
    st.stop()


if st.session_state.final_sql:
    st.subheader("出力SQL（入力値で埋め込み）")
    rendered_sql = ""
    try:
        ids_for_render = parse_ids(ids_raw)
        if start_date and end_date and ids_for_render:
            rendered_sql = render_sql_with_params(
                st.session_state.final_sql, start_date, end_date, ids_for_render
            )
            st.code(rendered_sql, language="sql")
        else:
            st.error("埋め込みに必要な入力値が不足しています。")
    except Exception as e:
        st.error(f"埋め込みに失敗しました: {e}")

    if rendered_sql:
        code_notes = extract_code_notes(rendered_sql, codebook)
        if code_notes:
            st.subheader("コード値（自動補足）")
            st.markdown("\n".join([f"- {n}" for n in code_notes]))

    if st.session_state.final_notes:
        st.subheader("補足")
        st.write(st.session_state.final_notes)

    st.info(
        "※このアプリはSQLを“実行しません”。実行する場合も別途、読み取り専用の権限/ジョブ実行制御を推奨します。"
    )

render_history_panel(st.session_state.get("history", []))
