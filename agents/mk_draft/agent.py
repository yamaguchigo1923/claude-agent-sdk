"""
mk_draft Agent - SNS台本作成エージェント

各フェーズの処理関数を提供する。Slack Bot の HITL フローから呼ばれる。

Phases:
  1. load_past_data()    - Google Sheets から過去データを読み込む
  2. propose_topics()   - 題材候補を生成する
  3. generate_outline() - 構成案を生成する
  4. revise_outline()   - 構成案をフィードバックで修正する
  5. generate_draft()   - 台本全文を生成する
  6. revise_draft()     - 台本をフィードバックで修正する
  7. write_to_sheets()  - Google Sheets に書き込む
  8. save_output()      - output/ に MD ファイルを保存する

参照: document/agent-creation-guide.md
"""

import json
import os
import re
from datetime import datetime
from pathlib import Path

import anthropic
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────────
# 設定（agent-creation-guide.md 準拠）
# ─────────────────────────────────────────────

MODEL = "claude-haiku-4-5-20251001"
INPUT_COST_PER_M  = 0.80
OUTPUT_COST_PER_M = 4.00
USD_TO_JPY = 150

# Sonnet（Webリサーチ用）
MODEL_SONNET             = "claude-haiku-4-5-20251001"
SONNET_INPUT_COST_PER_M  = 1.00
SONNET_OUTPUT_COST_PER_M = 5.00

AGENT_NAME = "mk_draft"
OUTPUT_DIR = Path(__file__).parent / "output"


def _make_client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))


def _calc_cost(response: anthropic.types.Message) -> float:
    return (
        response.usage.input_tokens  / 1_000_000 * INPUT_COST_PER_M
        + response.usage.output_tokens / 1_000_000 * OUTPUT_COST_PER_M
    )


def _calc_cost_sonnet(response: anthropic.types.Message) -> float:
    return (
        response.usage.input_tokens  / 1_000_000 * SONNET_INPUT_COST_PER_M
        + response.usage.output_tokens / 1_000_000 * SONNET_OUTPUT_COST_PER_M
    )


def _extract_text_from_response(response: anthropic.types.Message) -> str:
    """tool use を含むレスポンスからテキストブロックを結合して返す"""
    parts = [block.text for block in response.content if hasattr(block, "text") and block.text]
    return "\n".join(parts).strip()


def _parse_json_array(raw: str, expected_count: int) -> list:
    """
    LLM レスポンスから JSON 配列を抽出する。
    raw_decode を使うことで前置き/後置きテキストがあっても正確に抽出できる。
    失敗時はフォールバック辞書リストを返す。
    """
    decoder = json.JSONDecoder()
    text = raw.strip()

    # 1. コードブロック内の配列を優先
    if "```" in text:
        for part in text.split("```")[1:]:
            candidate = part.lstrip("json\n").strip()
            if candidate.startswith("["):
                text = candidate.split("```")[0].strip()
                break

    # 2. 直接パース
    try:
        result = json.loads(text)
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            return [result]
    except json.JSONDecodeError:
        pass

    # 3. raw_decode で最初の [ から配列を抽出（後続テキストを無視）
    idx = text.find("[")
    if idx >= 0:
        try:
            result, _ = decoder.raw_decode(text, idx)
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            pass

    # 4. raw_decode で最初の { から単一オブジェクトを抽出してリスト化
    idx = text.find("{")
    if idx >= 0:
        try:
            result, _ = decoder.raw_decode(text, idx)
            if isinstance(result, dict):
                return [result]
        except json.JSONDecodeError:
            pass

    print(f"⚠️ JSON配列パース失敗 stop_reason 確認推奨 (先頭300字): {raw[:300]!r}")
    return [{"企画概要": f"案{i + 1}", "台本セクション1": "（生成失敗）"} for i in range(expected_count)]


def _estimate_section_lengths(headers: list, rows: list) -> str:
    """過去データから台本セクション列の平均文字数を計測してプロンプト用文字列を返す"""
    results = []
    for h in headers:
        if "台本セクション" not in h:
            continue
        try:
            idx = headers.index(h)
        except ValueError:
            continue
        vals = [row[idx] for row in rows if idx < len(row) and row[idx]]
        if vals:
            avg = int(sum(len(v) for v in vals) / len(vals))
            results.append(f"  {h}: 平均{avg}文字")
    return "\n".join(results) if results else "  （計測データなし）"


# ─────────────────────────────────────────────
# Phase 1: Google Sheets から過去データ読み込み
# ─────────────────────────────────────────────

def load_past_data(spreadsheet_id: str) -> dict:
    """
    Google Sheets から過去の規格・台本データを読み込む。

    Returns:
        成功: {"headers": [...], "rows": [[...]], "summary": "...", "total_count": int}
        失敗: {"error": "...", "headers": [], "rows": [], "summary": "データなし"}
    """
    if not spreadsheet_id:
        return {"error": None, "headers": [], "rows": [], "summary": "データなし（スプレッドシートID未設定）"}

    try:
        import gspread
        from google.oauth2.service_account import Credentials

        creds_path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
        if not creds_path:
            return {
                "error": "GOOGLE_SERVICE_ACCOUNT_JSON が未設定です",
                "headers": [], "rows": [], "summary": "データなし（認証情報未設定）",
            }

        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive.readonly",
        ]
        creds = Credentials.from_service_account_file(creds_path, scopes=scopes)
        client = gspread.authorize(creds)

        sheet = client.open_by_key(spreadsheet_id).sheet1
        all_values = sheet.get_all_values()

        if not all_values:
            return {"error": None, "headers": [], "rows": [], "summary": "データなし（シートが空）"}

        headers = all_values[0]
        rows = all_values[1:]

        # 直近50件を保持（列はすべて保持 — write_to_sheets でヘッダーマッピングに使う）
        recent = rows[-50:] if len(rows) > 50 else rows

        # summary はトークン節約のため先頭12列のみ
        key_cols = min(12, len(headers))
        summary = "\t".join(headers[:key_cols]) + "\n" + "\n".join(
            "\t".join(r[:key_cols]) for r in recent
        )

        return {
            "error": None,
            "headers": headers,   # 全列ヘッダー
            "rows": recent,       # 全列データ（直近20件）
            "summary": summary,
            "total_count": len(rows),
        }

    except ImportError:
        return {
            "error": "gspread が未インストールです。uv sync を実行してください",
            "headers": [], "rows": [], "summary": "データなし",
        }
    except FileNotFoundError:
        creds_path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
        return {
            "error": f"認証ファイルが見つかりません: {creds_path}",
            "headers": [], "rows": [], "summary": "データなし",
        }
    except Exception as e:
        return {
            "error": f"スプレッドシート読み込みエラー: {e}",
            "headers": [], "rows": [], "summary": "データなし",
        }


# ─────────────────────────────────────────────
# Phase 1b: スプレッドシートデータのサマリー生成
# ─────────────────────────────────────────────

def summarize_sheet_data(past_data: dict) -> tuple[str, float]:
    """
    スプレッドシートのデータを人間が読みやすい形にまとめる。

    Returns: (summary_text, cost_usd)
    """
    if not past_data.get("rows"):
        return "過去データなし（初回実行）", 0.0

    client = _make_client()
    count  = past_data.get("total_count", len(past_data.get("rows", [])))

    prompt = (
        f"以下のスプレッドシートデータを分析して日本語で簡潔にまとめてください。\n"
        f"列ヘッダー: {past_data.get('headers', [])}\n"
        f"データ（全{count}件中直近{len(past_data['rows'])}件）:\n"
        f"{past_data['summary']}\n\n"
        f"以下を含めてください:\n"
        f"- 総件数\n"
        f"- 最近の投稿テーマ（3〜5件）\n"
        f"- 全体的な傾向・パターン（1〜2文）"
    )

    response = client.messages.create(
        model=MODEL,
        max_tokens=350,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip(), _calc_cost(response)


# ─────────────────────────────────────────────
# Phase 2: 題材・テーマ候補の生成
# ─────────────────────────────────────────────

_TOPIC_SYSTEM = """あなたはSNS運用代行の専門家です。
過去の投稿データを分析して、次回の投稿テーマの候補を提案してください。
日本語で回答してください。"""


def propose_topics(past_data: dict, user_hint: str = "") -> tuple[str, float]:
    """
    過去データを元に題材候補（3〜5件）を生成する。

    Returns: (番号付きテーマリストのテキスト, cost_usd)
    """
    client = _make_client()

    data_section = ""
    if past_data.get("summary") and past_data["summary"] != "データなし":
        count = past_data.get("total_count", len(past_data.get("rows", [])))
        data_section = f"【過去データ（直近{min(count, 20)}件）】\n{past_data['summary']}\n\n"
    else:
        data_section = "【過去データ】なし（初回または未取得）\n\n"

    hint_section = f"【追加指示】{user_hint}\n\n" if user_hint else ""

    prompt = (
        f"{data_section}"
        f"{hint_section}"
        "上記をもとに、次回のSNS投稿テーマの候補を3〜5件提案してください。\n"
        "各候補について以下を含めてください:\n"
        "- テーマ名（簡潔に）\n"
        "- 提案理由（1〜2行）\n"
        "- 期待できる反応・効果（1行）\n\n"
        "番号付きリストで返してください。"
    )

    response = client.messages.create(
        model=MODEL,
        max_tokens=800,
        system=_TOPIC_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )

    return response.content[0].text.strip(), _calc_cost(response)


# ─────────────────────────────────────────────
# Phase 3: 構成・アウトラインの生成と修正
# ─────────────────────────────────────────────

_OUTLINE_SYSTEM = """あなたはSNS運用代行の専門家です。
選択されたテーマで、過去の投稿フォーマットに合わせた構成案を作成してください。
日本語で回答してください。"""


def generate_outline(topic: str, past_data: dict) -> tuple[str, float]:
    """
    テーマを元に構成案（アウトライン）を生成する。

    Returns: (構成案テキスト, cost_usd)
    """
    client = _make_client()

    data_section = ""
    if past_data.get("summary") and past_data["summary"] != "データなし":
        data_section = f"【参考：過去の投稿データ（フォーマット・スタイル参考）】\n{past_data['summary']}\n\n"

    prompt = (
        f"選択テーマ: *{topic}*\n\n"
        f"{data_section}"
        "このテーマでSNS投稿の構成案（アウトライン）を作成してください。\n"
        "以下の形式で返してください:\n\n"
        "【構成案】\n"
        "1. [セクション名]（目安の長さ・秒数）: 内容の概要\n"
        "2. ...\n\n"
        "全体で5〜8セクション程度にまとめてください。"
    )

    response = client.messages.create(
        model=MODEL,
        max_tokens=600,
        system=_OUTLINE_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )

    return response.content[0].text.strip(), _calc_cost(response)


def revise_outline(topic: str, outline: str, feedback: str) -> tuple[str, float]:
    """
    フィードバックをもとに構成案を修正する。

    Returns: (修正後の構成案テキスト, cost_usd)
    """
    client = _make_client()

    prompt = (
        f"テーマ: *{topic}*\n\n"
        f"【現在の構成案】\n{outline}\n\n"
        f"【修正依頼】\n{feedback}\n\n"
        "上記の修正依頼に基づいて構成案を改善してください。\n"
        "改善後を「【構成案（修正版）】」という見出しで返してください。"
    )

    response = client.messages.create(
        model=MODEL,
        max_tokens=600,
        system=_OUTLINE_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )

    return response.content[0].text.strip(), _calc_cost(response)


# ─────────────────────────────────────────────
# Phase 4: 台本の生成と修正（スプシ列構造対応）
# ─────────────────────────────────────────────

_STRUCTURED_DRAFT_SYSTEM = """あなたはSNS運用代行の専門家です。
スプレッドシートの各列に入力するデータをJSONで生成してください。
過去データのスタイル・分量・フォーマットに完全に合わせてください。
JSONのみを返してください（コードブロック・説明不要）。日本語で回答してください。"""

# 表示用テキスト変換（Slack レビュー・save_output 用）
_META_KEYS    = ["媒体", "企画FMT", "企画概要"]
_HOOK_KEYS    = ["視聴開始の仕掛け", "視聴維持の仕掛け", "コメント誘発の仕掛け"]
_AUTO_KEYS    = {"台本No.", "投稿日", "参考動画URL"}


def _format_structured_for_display(structured: dict) -> str:
    """構造化辞書を Slack / MD 表示用テキストに変換する"""
    lines = []
    for key in _META_KEYS:
        if structured.get(key):
            lines.append(f"*{key}*: {structured[key]}")

    for key in _HOOK_KEYS:
        if structured.get(key):
            lines.append(f"\n*{key}*: {structured[key]}")

    skip = set(_META_KEYS) | set(_HOOK_KEYS) | _AUTO_KEYS
    section_keys = sorted(
        [k for k in structured if "セクション" in k],
        key=lambda x: int("".join(filter(str.isdigit, x)) or "0"),
    )
    skip |= set(section_keys)

    for key, val in structured.items():
        if key not in skip and val:
            lines.append(f"\n*{key}*: {val}")

    if section_keys:
        lines.append("\n---")
        for key in section_keys:
            if structured.get(key):
                lines.append(f"\n*【{key}】*\n{structured[key]}")

    return "\n".join(lines)


def _parse_json_response(raw: str, fallback_topic: str) -> dict:
    """LLM レスポンスから JSON を抽出する。raw_decode で前後テキストを無視して抽出。"""
    decoder = json.JSONDecoder()
    text = raw.strip()

    # コードブロック内の JSON を優先
    if "```" in text:
        for part in text.split("```")[1:]:
            candidate = part.lstrip("json\n").strip()
            if candidate.startswith("{"):
                text = candidate.split("```")[0].strip()
                break

    # 直接パース
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # raw_decode で最初の { からオブジェクトを抽出
    idx = text.find("{")
    if idx >= 0:
        try:
            result, _ = decoder.raw_decode(text, idx)
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass

    return {"企画概要": fallback_topic, "台本セクション1": raw}


def generate_draft(topic: str, outline: str, past_data: dict) -> tuple[dict, str, float]:
    """
    構成案をもとにスプシ列構造に合わせた台本を生成する。

    Returns: (structured_dict, display_text, cost_usd)
    """
    client   = _make_client()
    headers  = past_data.get("headers", [])
    rows     = past_data.get("rows", [])

    # 過去サンプル（直近2件）をヘッダー付きで整形
    sample_parts = []
    for i, row in enumerate(rows[-2:], 1):
        parts = []
        for j, h in enumerate(headers):
            val = row[j] if j < len(row) else ""
            if val:
                parts.append(f"  {h}: {val[:250]}{'...' if len(val) > 250 else ''}")
        if parts:
            sample_parts.append(f"--- 過去サンプル{i} ---\n" + "\n".join(parts))
    sample_str = "\n\n".join(sample_parts) if sample_parts else "（なし）"

    # 自動入力列を除いた生成対象ヘッダー
    gen_cols = [h for h in headers if h and h not in _AUTO_KEYS]
    section_cols = [h for h in headers if "台本セクション" in h]

    prompt = (
        f"テーマ: {topic}\n\n"
        f"構成案:\n{outline}\n\n"
        f"生成対象の列（このキーでJSONを作成）:\n{gen_cols}\n\n"
        f"台本セクション列: {section_cols}（構成案の各セクションを対応する列に入力）\n\n"
        f"過去データのサンプル（この分量・スタイルに合わせること）:\n{sample_str}\n\n"
        "【注意】\n"
        "- 各「台本セクション」には完全な台本テキストを入力（省略・要約禁止）\n"
        "- 過去サンプルと同等の詳細度・セリフ量で作成\n"
        "- 返答はJSONのみ（```や説明不要）"
    )

    response = client.messages.create(
        model=MODEL,
        max_tokens=4000,
        system=_STRUCTURED_DRAFT_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )

    structured   = _parse_json_response(response.content[0].text, topic)
    display_text = _format_structured_for_display(structured)
    return structured, display_text, _calc_cost(response)


def revise_draft(topic: str, structured_draft: dict, feedback: str) -> tuple[dict, str, float]:
    """
    フィードバックをもとに構造化台本を修正する。

    Returns: (revised_dict, display_text, cost_usd)
    """
    client = _make_client()

    prompt = (
        f"テーマ: {topic}\n\n"
        f"【現在の台本（JSON）】\n{json.dumps(structured_draft, ensure_ascii=False, indent=2)}\n\n"
        f"【修正依頼】\n{feedback}\n\n"
        "修正依頼に基づいて改善し、同じJSON形式で返してください。\n"
        "修正が必要な列のみ変更し、他は維持してください。\n"
        "返答はJSONのみ（```や説明不要）。"
    )

    response = client.messages.create(
        model=MODEL,
        max_tokens=4000,
        system=_STRUCTURED_DRAFT_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )

    revised      = _parse_json_response(response.content[0].text, topic)
    # フォールバック時も元データを維持
    if not revised.get("台本セクション1") and structured_draft.get("台本セクション1"):
        revised = {**structured_draft, **revised}
    display_text = _format_structured_for_display(revised)
    return revised, display_text, _calc_cost(response)


# ─────────────────────────────────────────────
# 自動パイプライン: Webリサーチ + 複数案一括生成
# ─────────────────────────────────────────────

_RESEARCH_PROMPT_TEMPLATE = """\
以下のクライアントの既存SNS投稿一覧を参考に、次回投稿で使える新しいトレンドフォーマットをリサーチしてください。

【既存コンテンツ（重複禁止）】
{existing}

{hint_section}
リサーチ内容:
1. TikTok・Instagram Reels・YouTube Shortsで現在（2025〜2026年）バズっているフォーマット・仕掛け
2. 食系・日常系・ライフスタイル系で伸びているコンテンツパターン
3. 上記既存コンテンツと被らない新しい切り口・アングル
4. 視聴者参加型・コメント誘発につながるトレンド手法

具体的な情報（数値・事例・プラットフォーム名）を含めて報告してください。"""


def research_sns_trends(past_data: dict, hint: str = "") -> tuple[str, float]:
    """
    Sonnet + Web検索で最新SNSトレンドをリサーチする。
    Web検索が使えない場合は内部知識のみで実行（フォールバック）。

    Returns: (research_text, cost_usd)
    """
    client = _make_client()
    headers = past_data.get("headers", [])
    rows    = past_data.get("rows", [])

    # 既存コンテンツ一覧（重複回避用）
    existing_topics: list[str] = []
    for h in ["企画概要", "テーマ", "タイトル"]:
        if h in headers:
            idx = headers.index(h)
            for row in rows[-30:]:
                val = row[idx] if idx < len(row) else ""
                if val:
                    existing_topics.append(val[:80])
            break
    existing_str = "\n".join(f"- {t}" for t in existing_topics[-20:]) if existing_topics else "なし"
    hint_section = f"【追加指示】\n{hint}\n" if hint else ""

    prompt = _RESEARCH_PROMPT_TEMPLATE.format(
        existing=existing_str,
        hint_section=hint_section,
    )

    tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}]
    try:
        response = client.messages.create(
            model=MODEL_SONNET,
            max_tokens=2000,
            tools=tools,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception:
        # Web検索不可の場合は内部知識のみで実行
        response = client.messages.create(
            model=MODEL_SONNET,
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )

    return _extract_text_from_response(response), _calc_cost_sonnet(response)


_ALL_PROPOSALS_SYSTEM = """あなたはSNS運用代行の専門家です。
スプレッドシートの列ヘッダーに合わせた企画サマリーをJSON配列で生成してください。
過去データのスタイル・フォーマットに完全に合わせてください。
JSON配列のみを返してください（コードブロック・説明不要）。日本語で回答してください。"""

_EXPAND_DRAFT_SYSTEM = """あなたはSNS運用代行の専門家です。
選択された企画サマリーをもとに完全な台本（スプシ全列）をJSONで生成してください。
過去データのスタイル・分量・フォーマットに完全に合わせてください。
JSONのみを返してください（コードブロック・説明不要）。日本語で回答してください。"""


def generate_all_proposals(
    past_data: dict,
    research: str,
    hint: str = "",
    n: int = 4,
) -> tuple[list, float]:
    """
    Webリサーチ結果と過去データをもとに n 案の「企画サマリー」を一括生成する。
    台本セクション（本文）は含まない。expand_proposal_to_draft() で別途生成する。

    Returns: (list[summary_dict], cost_usd)
    """
    client  = _make_client()
    headers = past_data.get("headers", [])
    rows    = past_data.get("rows", [])

    # サマリー列のみ（台本セクションは除外）
    summary_cols = [h for h in headers if h and h not in _AUTO_KEYS and "台本セクション" not in h]

    # 過去サンプル（直近2件のサマリー列のみ）
    sample_parts = []
    for i, row in enumerate(rows[-2:], 1):
        parts = []
        for h in summary_cols:
            j = headers.index(h) if h in headers else -1
            if j >= 0 and j < len(row) and row[j]:
                parts.append(f"  {h}: {row[j][:150]}")
        if parts:
            sample_parts.append(f"--- 過去サンプル{i} ---\n" + "\n".join(parts))
    sample_str = "\n\n".join(sample_parts) if sample_parts else "（なし）"

    hint_section = f"【追加指示】\n{hint}\n\n" if hint else ""
    # リサーチが長い場合は先頭2000字に絞る（入力コスト削減）
    research_excerpt = research[:2000] if len(research) > 2000 else research

    prompt = (
        f"【リサーチ結果（最新トレンド・新フォーマット）】\n{research_excerpt}\n\n"
        f"{hint_section}"
        f"【生成対象の列（このキーでJSONを作成）】\n{summary_cols}\n"
        "※ 台本セクション列は含めない\n\n"
        f"【過去データのサンプル（スタイル・出演者名・口調を参照）】\n{sample_str}\n\n"
        f"上記をもとに、異なるテーマ・フォーマットで{n}案の企画サマリーを作成してください。\n\n"
        "【絶対ルール】\n"
        "- 返答はJSON配列のみ（前置き・説明・コードブロック一切不要）\n"
        f"- 必ず{n}要素のJSON配列で返す\n"
        "- 台本テキスト（台本セクション）は含めない\n"
        "- 案ごとに異なるリサーチ結果のトレンドを活用\n"
        "- 既存コンテンツと被るテーマ・フォーマットを避ける"
    )

    response = client.messages.create(
        model=MODEL,
        max_tokens=4000,
        system=_ALL_PROPOSALS_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )

    if response.stop_reason == "max_tokens":
        print(f"⚠️ generate_all_proposals: max_tokens ({response.usage.output_tokens}) で打ち切られました")
    proposals = _parse_json_array(response.content[0].text, n)
    return proposals, _calc_cost(response)


def expand_proposal_to_draft(
    proposal_summary: dict,
    past_data: dict,
) -> tuple[dict, str, float]:
    """
    選択された企画サマリーから完全な台本（台本セクション含む全列）を生成する。

    Returns: (structured_dict, display_text, cost_usd)
    """
    client  = _make_client()
    headers = past_data.get("headers", [])
    rows    = past_data.get("rows", [])

    # 過去サンプル（直近2件・全列）
    sample_parts = []
    for i, row in enumerate(rows[-2:], 1):
        parts = []
        for j, h in enumerate(headers):
            val = row[j] if j < len(row) else ""
            if val:
                parts.append(f"  {h}: {val[:200]}{'...' if len(val) > 200 else ''}")
        if parts:
            sample_parts.append(f"--- 過去サンプル{i} ---\n" + "\n".join(parts))
    sample_str = "\n\n".join(sample_parts) if sample_parts else "（なし）"

    section_len_str = _estimate_section_lengths(headers, rows)
    section_cols    = [h for h in headers if "台本セクション" in h]
    all_cols        = [h for h in headers if h and h not in _AUTO_KEYS]

    # サマリーをテキスト化してプロンプトに含める
    summary_text = "\n".join(
        f"  {k}: {v}" for k, v in proposal_summary.items() if v and "セクション" not in k
    )

    prompt = (
        f"【選択された企画サマリー（このフィールドをそのまま引き継ぐ）】\n{summary_text}\n\n"
        f"【全列ヘッダー（このキーでJSONを作成）】\n{all_cols}\n\n"
        f"【台本セクション列】\n{section_cols}（各セクションに完全な台本テキストを入力）\n\n"
        f"【各セクションの目標文字数（過去データ平均）】\n{section_len_str}\n"
        "この文字数に近い分量で各セクションを記述すること。\n\n"
        f"【過去データのサンプル（スタイル・分量・出演者名・口調を完全に踏襲）】\n{sample_str}\n\n"
        "上記の企画サマリーを基に、完全な台本を生成してください。\n\n"
        "【注意】\n"
        "- 企画サマリーのフィールド値は変更せず JSON に含める\n"
        "- 各「台本セクション」は省略なし・完全なセリフで記述\n"
        "- 過去サンプルと同等の文字数・詳細度\n"
        "- 返答はJSONのみ（```や説明不要）"
    )

    response = client.messages.create(
        model=MODEL,
        max_tokens=6000,
        system=_EXPAND_DRAFT_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )

    topic      = str(proposal_summary.get("企画概要", ""))
    structured = _parse_json_response(response.content[0].text, topic)
    # サマリーのフィールドを確実に保持（LLMが変更した場合のフォールバック）
    merged       = {**proposal_summary, **structured}
    display_text = _format_structured_for_display(merged)
    return merged, display_text, _calc_cost(response)


# ─────────────────────────────────────────────
# Phase 5: Google Sheets への書き込み
# ─────────────────────────────────────────────

def write_to_sheets(
    spreadsheet_id: str,
    structured_data: dict,
) -> tuple[str, str | None]:
    """
    スプレッドシートの列ヘッダーに合わせてデータを追記する。

    structured_data のキーはシートの列ヘッダーと一致させること。
    台本No. / 投稿日 は自動設定。参考動画URL は空白。

    Returns: (spreadsheet_url, error_or_None)
    """
    if not spreadsheet_id:
        return "", "DRAFT_SPREADSHEET_ID が未設定です"

    try:
        import gspread
        from google.oauth2.service_account import Credentials

        creds_path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
        if not creds_path:
            return "", "GOOGLE_SERVICE_ACCOUNT_JSON が未設定です"

        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = Credentials.from_service_account_file(creds_path, scopes=scopes)
        gc = gspread.authorize(creds)

        spreadsheet = gc.open_by_key(spreadsheet_id)
        sheet = spreadsheet.sheet1

        all_values = sheet.get_all_values()
        if not all_values:
            return "", "シートが空です（ヘッダー行が見つかりません）"

        headers = all_values[0]
        next_no = len(all_values)         # ヘッダー含む行数 = 新しい台本No.
        today   = datetime.now().strftime("%Y-%m-%d %H:%M")

        # 各ヘッダーに対応する値を組み立て
        row = []
        for h in headers:
            if h == "台本No.":
                row.append(str(next_no))
            elif h == "投稿日":
                row.append(today)
            elif h == "参考動画URL":
                row.append("")
            else:
                row.append(str(structured_data.get(h, "")))

        sheet.append_row(row)

        url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}"
        return url, None

    except ImportError:
        return "", "gspread が未インストールです。uv sync を実行してください"
    except Exception as e:
        return "", f"Sheets 書き込みエラー: {e}"


# ─────────────────────────────────────────────
# output/ への保存（agent-creation-guide.md 準拠）
# ─────────────────────────────────────────────

def save_output(
    topic: str,
    outline: str,
    draft: str,
    elapsed_str: str,
    cost_usd: float,
    cost_jpy: float,
    spreadsheet_url: str = "",
) -> Path:
    """
    output/ ディレクトリに MD ファイルとして保存する。

    Returns: 保存したファイルの Path
    """
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file = OUTPUT_DIR / f"{AGENT_NAME}_{timestamp}.md"

    today = datetime.now().strftime("%Y年%m月%d日 %H:%M")
    sheets_line = f"- 📊 スプレッドシート: {spreadsheet_url}" if spreadsheet_url else ""

    content = f"""# SNS台本: {topic}
作成日: {today}

{outline}

---

{draft}

---

## 実行サマリー
- ⏱ 実行時間: {elapsed_str}
- 💰 推定コスト: ${cost_usd:.4f} USD (約 {cost_jpy:.1f} 円)
{sheets_line}
"""

    output_file.write_text(content, encoding="utf-8")
    print(f"📄 出力ファイル: {output_file}")
    return output_file
