"""
Slack Bot - Socket Mode フロントエージェント

動作:
- 明確なタスク → エージェントを選定 → 時間・費用を提示して確認 → 実行 → スレッドに返答
- 不明確 / 相談 → 質問して明確化
- mk_draft: 台本作成を HITL (Human-in-the-loop) で進行

新しいエージェント追加時は document/agent-creation-guide.md を参照すること。

起動方法:
    cd C:/Users/yamag/project/claude-agent-sdk
    uv run python slack_bot/main.py
"""

import json
import os
import sys
import threading
import time
import asyncio
from datetime import datetime
from pathlib import Path

import anthropic
from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from slack_sdk.socket_mode import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from agents.research import run_research
from agents.mk_draft import (
    load_past_data,
    summarize_sheet_data,
    generate_draft,
    revise_draft,
    write_to_sheets,
    save_output,
    research_sns_trends,
    generate_all_proposals,
    expand_proposal_to_draft,
    format_structured_for_display,
)

load_dotenv(PROJECT_ROOT / ".env")

# ─────────────────────────────────────────────
# 設定
# ─────────────────────────────────────────────

SLACK_BOT_TOKEN  = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_APP_TOKEN  = os.environ.get("SLACK_APP_TOKEN", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
DRAFT_SPREADSHEET_ID = os.environ.get("DRAFT_SPREADSHEET_ID", "")

if not SLACK_BOT_TOKEN or not SLACK_APP_TOKEN:
    print("❌ SLACK_BOT_TOKEN と SLACK_APP_TOKEN を .env に設定してください")
    sys.exit(1)

# フロントエージェントは意図判断のみ
FRONT_AGENT_MAX_TOKENS = 200

web_client      = WebClient(token=SLACK_BOT_TOKEN)
anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# 状態管理 { thread_ts: { "action": ..., ... } }
pending_tasks: dict = {}
pending_tasks_lock = threading.Lock()


# ─────────────────────────────────────────────
# ターミナルモニター（並列エージェントをインプレース表示）
# ─────────────────────────────────────────────

class TerminalMonitor:
    """並列エージェントの実行状態をターミナルにインプレース表示する"""

    def __init__(self) -> None:
        self._agents: dict[str, dict] = {}
        self._lock   = threading.Lock()
        self._lines  = 0
        self._tty    = sys.stdout.isatty()

    # ── 公開 API ─────────────────────────────────────

    def log(self, msg: str) -> None:
        """通常ログ（モニター表示を保ちながら出力）"""
        with self._lock:
            self._erase()
            print(msg, flush=True)
            self._draw()

    def update(self, tid: str, name: str, step: str, cost: float = 0.0) -> None:
        """エージェント状態を更新して再描画"""
        with self._lock:
            prev = self._agents.get(tid, {})
            self._agents[tid] = {
                "name":  name,
                "step":  step,
                "start": prev.get("start", time.time()),
                "cost":  cost,
            }
            self._erase()
            self._draw()

    def done(self, tid: str) -> None:
        """エージェント完了（エントリを削除して再描画）"""
        with self._lock:
            self._agents.pop(tid, None)
            self._erase()
            self._draw()

    # ── 内部描画 ─────────────────────────────────────

    def _erase(self) -> None:
        if self._tty and self._lines > 0:
            sys.stdout.write(f"\033[{self._lines}A\033[J")
            self._lines = 0

    def _draw(self) -> None:
        if not self._agents or not self._tty:
            return
        hdr  = f"\033[2m── agents {datetime.now().strftime('%H:%M:%S')} ──\033[0m"
        rows = []
        for info in self._agents.values():
            sec = int(time.time() - info["start"])
            c   = f"  ${info['cost']:.4f}" if info["cost"] > 0 else ""
            rows.append(f"  {info['name']}  {info['step']}  {sec}s{c}")
        output = "\n".join([hdr] + rows) + "\n"
        sys.stdout.write(output)
        sys.stdout.flush()
        self._lines = 1 + len(rows)


monitor = TerminalMonitor()

# ─────────────────────────────────────────────
# 履歴管理（実績ベースの見積もり）
# agent-creation-guide.md §4 参照
# ─────────────────────────────────────────────

HISTORY_FILES = {
    "research": PROJECT_ROOT / "agents" / "research" / "history.json",
    "mk_draft": PROJECT_ROOT / "agents" / "mk_draft"  / "history.json",
}


def load_history(agent: str) -> list:
    path = HISTORY_FILES.get(agent)
    if path and path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def save_history(agent: str, topic: str, result: dict) -> None:
    path = HISTORY_FILES.get(agent)
    if not path:
        return
    history = load_history(agent)
    history.append({
        "timestamp":       datetime.now().isoformat(),
        "topic":           topic,
        "elapsed_seconds": result.get("elapsed_seconds", 0),
        "cost_usd":        result.get("cost_usd", 0),
        "cost_jpy":        result.get("cost_jpy", 0),
    })
    path.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
    monitor.log(f"📝 履歴を保存: {path.name} ({len(history)}件)")


def get_estimate(agent: str) -> dict:
    """履歴から平均時間・費用を算出して返す"""
    history = load_history(agent)
    n = len(history)

    if n >= 2:
        avg_sec = sum(h.get("elapsed_seconds", 600) for h in history) / n
        avg_jpy = sum(h.get("cost_jpy", 50)         for h in history) / n
        lo_min  = max(1, int(avg_sec // 60) - 2)
        hi_min  = int(avg_sec // 60) + 3
        lo_jpy  = max(1, int(avg_jpy * 0.7))
        hi_jpy  = int(avg_jpy * 1.4)
        return {"time": f"{lo_min}〜{hi_min}分", "cost": f"約{lo_jpy}〜{hi_jpy}円", "note": f"過去{n}件の実績より"}

    if n == 1:
        h = history[0]
        return {
            "time": f"約{int(h.get('elapsed_seconds', 600) // 60)}分",
            "cost": f"約{int(h.get('cost_jpy', 50))}円",
            "note": "前回実績より",
        }

    return AGENT_INFO.get(agent, {}).get("_default_estimate",
        {"time": "不明", "cost": "不明", "note": "初回のため概算"})


# ─────────────────────────────────────────────
# エージェント情報（agent-creation-guide.md §6 参照）
# ─────────────────────────────────────────────

AGENT_INFO = {
    "research": {
        "name":  "research-agent",
        "label": "SNS・SEOリサーチ",
        "_default_estimate": {"time": "5〜15分", "cost": "約30〜70円", "note": "初回のため概算"},
    },
    "mk_draft": {
        "name":  "mk_draft-agent",
        "label": "SNS台本作成",
        "_default_estimate": {"time": "5〜10分", "cost": "約10〜30円", "note": "初回のため概算"},
    },
}

# ─────────────────────────────────────────────
# Slack ユーティリティ
# ─────────────────────────────────────────────

def post_message(channel: str, text: str, thread_ts: str | None = None) -> None:
    try:
        web_client.chat_postMessage(channel=channel, text=text, thread_ts=thread_ts)
    except SlackApiError as e:
        monitor.log(f"⚠️  Slack 送信エラー: {e.response['error']}")


def upload_file(channel: str, file_path: Path, comment: str, thread_ts: str | None = None) -> None:
    try:
        web_client.files_upload_v2(
            channel=channel,
            file=str(file_path),
            filename=file_path.name,
            initial_comment=comment,
            thread_ts=thread_ts,
        )
    except SlackApiError as e:
        post_message(channel, f"{comment}\n⚠️ ファイル添付エラー: {e.response['error']}", thread_ts)


# ─────────────────────────────────────────────
# フロントエージェント（意図判断）
# ─────────────────────────────────────────────

ROUTING_SYSTEM = """あなたはSlack Botのフロントエージェントです。
ユーザーのメッセージを分析して、以下のJSONのみを返してください（コードブロック不要）。

利用可能なエージェント:
- research: SNS・SEO・市場のリサーチ・調査・分析
- mk_draft: SNS台本・投稿コンテンツ・スクリプト作成

【ルーティング方針】
このBotはエージェント案内専用です。ユーザーは必ずどちらかのエージェントを使います。

- 「調べて」「リサーチ」「分析」「調査」「トレンド確認」など情報収集 → research
- それ以外はすべて → mk_draft
  （「台本」「投稿」「作って」「エージェント」「draft」「コンテンツ」「規格」
   「作成」「出して」「スクリプト」「動画」など何でも mk_draft）
- 純粋な挨拶のみ → chat（ただし次の依頼を促す）
- ask は絶対に使わない（不明な場合も mk_draft を選ぶ）

【返答形式】
{"action": "mk_draft",  "hint": "ユーザーの追加指示があれば記載、なければ空文字"}
{"action": "research",  "topic": "リサーチトピック"}
{"action": "chat",      "reply": "挨拶への返答（次の依頼を促す一言を添える）"}

【例】
「台本作って」→ {"action": "mk_draft", "hint": ""}
「draft出して」→ {"action": "mk_draft", "hint": ""}
「エージェントを出して」→ {"action": "mk_draft", "hint": ""}
「台本作成エージェント」→ {"action": "mk_draft", "hint": ""}
「食べ物系でお願い」→ {"action": "mk_draft", "hint": "食べ物系"}
「Instagramのリールのトレンド調べて」→ {"action": "research", "topic": "Instagram リール トレンド"}
「こんにちは」→ {"action": "chat", "reply": "こんにちは！台本作成やリサーチなど、お気軽にどうぞ。"}"""

CONFIRM_YES      = {"はい", "yes", "ok", "OK", "実行", "よろしく", "お願い", "go", "GO", "y", "Y", "👍", "おねがい"}
CONFIRM_NO       = {"いいえ", "no", "No", "NO", "キャンセル", "やめて", "n", "N", "🙅", "やめる"}
CONFIRM_FINALIZE = {"確定", "OK", "ok", "はい", "yes", "承認", "よし", "いいよ", "👍", "決定"}
CANCEL_WORDS     = {"やめる", "キャンセル", "cancel", "やめて", "中止", "stop", "終了"}
# mk_draft_review から proposals への手戻りキーワード
GOTO_PROPOSALS   = {"他の案", "やり直し", "別のテーマ", "最初から", "別の案",
                    "案変えて", "テーマ変えて", "案一覧", "案を見直す", "他の選択肢"}

HELP_TEXT = """何でも依頼してください。対応できるエージェントがあれば起動します。

現在使えるエージェント:
📊 *research-agent* — SNS・SEOリサーチ
　例:「Instagram リール 2025 のトレンドを調べて」
　⏱ 約5〜15分 | 💰 約30〜70円

📝 *mk_draft-agent* — SNS台本作成
　例:「次の台本を作って」「投稿スクリプトを作成して」
　⏱ 約5〜10分 | 💰 約10〜30円

依頼内容を送ると、実行前に時間と費用を確認します。
不明な場合は質問しますので気軽に話しかけてください。"""


def classify_intent(text: str) -> dict:
    try:
        resp = anthropic_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=FRONT_AGENT_MAX_TOKENS,
            system=ROUTING_SYSTEM,
            messages=[{"role": "user", "content": text}],
        )
        raw = resp.content[0].text.strip()
        if "```" in raw:
            raw = raw.split("```")[1].lstrip("json").strip()
        return json.loads(raw)
    except Exception as e:
        monitor.log(f"⚠️  意図分類エラー: {e}")
        return {"action": "ask", "question": "すみません、もう少し詳しく教えていただけますか？"}


# ─────────────────────────────────────────────
# research エージェント実行
# ─────────────────────────────────────────────

def handle_research(channel: str, topic: str, thread_ts: str) -> None:
    try:
        monitor.update(thread_ts, "research", "🔍 リサーチ中")
        result = asyncio.run(run_research(topic))

        elapsed_str = result.get("elapsed_str", "不明")
        cost_jpy    = result.get("cost_jpy", 0)
        cost_usd    = result.get("cost_usd", cost_jpy / 150)
        output_file = result.get("output_file")
        error       = result.get("error")

        if not error or output_file:
            save_history("research", topic, result)

        monitor.done(thread_ts)

        if error and not output_file:
            post_message(channel, f"❌ リサーチ失敗\nエラー: {error}", thread_ts)
            return

        summary = f"✅ リサーチ完了: *{topic}*\n⏱ {elapsed_str} | 💰 約{cost_jpy:.1f}円"
        if error:
            summary += f"\n⚠️ {error}（部分的なレポートがあります）"

        if output_file and Path(output_file).exists():
            upload_file(channel, Path(output_file), summary, thread_ts)
        else:
            post_message(channel, f"{summary}\n⚠️ レポートファイルが見つかりませんでした。", thread_ts)

    except Exception as e:
        monitor.done(thread_ts)
        post_message(channel, f"❌ エラーが発生しました: {e}", thread_ts)


# ─────────────────────────────────────────────
# mk_draft エージェント - フェーズハンドラー
# ─────────────────────────────────────────────

def _mk_draft_elapsed(task: dict) -> tuple[int, str]:
    """開始時刻からの経過時間を返す"""
    start = task.get("start_time")
    if isinstance(start, datetime):
        elapsed = int((datetime.now() - start).total_seconds())
    else:
        elapsed = 0
    return elapsed, f"{elapsed // 60}分{elapsed % 60}秒"


def _show_proposals(channel: str, proposals: list, thread_ts: str) -> None:
    """proposals リストをサマリー付きで Slack に投稿する"""
    lines = ["📋 *台本案ができました*\n"]
    for i, p in enumerate(proposals, 1):
        summary = str(p.get("企画概要", f"案{i}"))[:70]
        fmt     = str(p.get("企画FMT", ""))[:50]
        hook    = str(p.get("視聴開始の仕掛け", ""))[:60]
        lines.append(f"*案{i}: {summary}*")
        if fmt:
            lines.append(f"　フォーマット: {fmt}")
        if hook:
            lines.append(f"　開始の仕掛け: {hook}...")
        lines.append("")
    lines.append("番号で選択してください（例: 「2」）")
    lines.append("修正指示がある場合はテキストで入力してください。")
    lines.append("（やめる場合は「キャンセル」）")
    post_message(channel, "\n".join(lines), thread_ts)


def start_mk_draft(channel: str, hint: str, thread_ts: str) -> None:
    """全自動パイプライン: スプシ読込 → Webリサーチ → 4案生成 → Slack提示"""

    # Step 1: スプシ読込
    monitor.update(thread_ts, "mk_draft", "📂 スプシ読込中")
    post_message(channel, "📂 スプレッドシートを読み込んでいます...", thread_ts)
    past_data = load_past_data(DRAFT_SPREADSHEET_ID)
    if past_data.get("error"):
        post_message(channel, f"⚠️ スプシ読込エラー: {past_data['error']}\n過去データなしで続行します。", thread_ts)

    # Step 2: Webリサーチ（Sonnet + web_search）
    monitor.update(thread_ts, "mk_draft", "🔍 Webリサーチ中")
    post_message(channel, "🔍 最新SNSトレンドをリサーチ中...\n（Sonnet + Web検索）", thread_ts)
    try:
        research_text, r_cost = research_sns_trends(past_data, hint)
        monitor.update(thread_ts, "mk_draft", "🔍 リサーチ完了", r_cost)
    except Exception as e:
        research_text = ""
        r_cost = 0.0
        monitor.update(thread_ts, "mk_draft", "⚠️ リサーチ失敗")
        post_message(channel, f"⚠️ リサーチエラー（スキップして続行）: {e}", thread_ts)

    # Step 3: 4案生成（Haiku）
    monitor.update(thread_ts, "mk_draft", "✍️ 案生成中", r_cost)
    post_message(channel, "✍️ 台本案を4つ生成中...\n（Haiku）", thread_ts)
    try:
        proposals, p_cost = generate_all_proposals(past_data, research_text, hint, n=4)
    except Exception as e:
        monitor.done(thread_ts)
        post_message(channel, f"❌ 台本案の生成に失敗しました: {e}", thread_ts)
        return

    total_cost = r_cost + p_cost
    step_costs = {"research": r_cost, "proposals": p_cost}
    monitor.update(thread_ts, "mk_draft", "⏸ 案選択待ち", total_cost)
    _show_proposals(channel, proposals, thread_ts)

    with pending_tasks_lock:
        pending_tasks[thread_ts] = {
            "action":         "mk_draft_proposals",
            "channel":        channel,
            "past_data":      past_data,
            "proposals":      proposals,
            "research":       research_text,
            "hint":           hint,
            "start_time":     datetime.now(),
            "total_cost_usd": total_cost,
            "step_costs":     step_costs,
        }


def handle_mk_draft_proposals(channel: str, text: str, task: dict, thread_ts: str) -> None:
    """案一覧 → 番号選択: mk_draft_review へ / テキスト: ヒント付き再生成"""
    proposals = task.get("proposals", [])
    text_stripped = text.strip()

    if text_stripped.isdigit():
        idx = int(text_stripped) - 1
        if 0 <= idx < len(proposals):
            selected = proposals[idx]
            # サマリーから完全な台本を生成（台本セクションを含む）
            monitor.update(thread_ts, "mk_draft", "📝 台本生成中", task.get("total_cost_usd", 0))
            post_message(channel, f"📝 *案{idx + 1}* の台本を生成中...", thread_ts)
            try:
                full_draft, display_text, e_cost = expand_proposal_to_draft(
                    selected, task.get("past_data", {})
                )
            except Exception as e:
                monitor.update(thread_ts, "mk_draft", "⏸ 案選択待ち", task.get("total_cost_usd", 0))
                post_message(channel, f"❌ 台本生成に失敗しました: {e}", thread_ts)
                with pending_tasks_lock:
                    pending_tasks[thread_ts] = task
                return

            new_total  = task.get("total_cost_usd", 0) + e_cost
            prev_sc    = task.get("step_costs", {})
            new_sc     = {**prev_sc, "expand": prev_sc.get("expand", 0) + e_cost}

            preview = display_text[:600] + "\n\n...（以下省略）" if len(display_text) > 600 else display_text
            msg = (
                f"✅ *案{idx + 1}を選択しました*\n\n"
                f"{preview}\n\n"
                f"確定する場合は「*確定*」と送信してください。\n"
                f"修正がある場合は内容を教えてください。\n"
                f"他の案に戻る場合は「*他の案*」と送信してください。\n"
                f"（やめる場合は「キャンセル」）"
            )
            post_message(channel, msg, thread_ts)
            monitor.update(thread_ts, "mk_draft", "⏸ レビュー待ち", new_total)
            with pending_tasks_lock:
                pending_tasks[thread_ts] = {
                    "action":           "mk_draft_review",
                    "channel":          channel,
                    "past_data":        task.get("past_data"),
                    "proposals":        proposals,   # 手戻り用に保持
                    "research":         task.get("research", ""),
                    "topic":            str(selected.get("企画概要", f"案{idx + 1}")),
                    "outline":          "",
                    "structured_draft": full_draft,
                    "draft_display":    display_text,
                    "start_time":       task.get("start_time", datetime.now()),
                    "total_cost_usd":   new_total,
                    "step_costs":       new_sc,
                }
        else:
            post_message(channel, f"1〜{len(proposals)}の番号で選択してください。", thread_ts)
            with pending_tasks_lock:
                pending_tasks[thread_ts] = task  # 状態を維持
        return

    # テキスト入力 → ヒント付き再生成（research は使い回す）
    monitor.update(thread_ts, "mk_draft", "🔄 再生成中", task.get("total_cost_usd", 0))
    post_message(channel, "🔄 指示を踏まえて案を再生成中...", thread_ts)
    new_hint = " / ".join(filter(None, [task.get("hint", ""), text_stripped]))
    try:
        proposals, cost = generate_all_proposals(
            task.get("past_data", {}),
            task.get("research", ""),
            new_hint,
            n=4,
        )
    except Exception as e:
        monitor.update(thread_ts, "mk_draft", "⏸ 案選択待ち", task.get("total_cost_usd", 0))
        post_message(channel, f"❌ 再生成に失敗しました: {e}", thread_ts)
        with pending_tasks_lock:
            pending_tasks[thread_ts] = task
        return

    new_total = task.get("total_cost_usd", 0) + cost
    prev_step_costs = task.get("step_costs", {})
    new_step_costs  = {**prev_step_costs, "proposals": prev_step_costs.get("proposals", 0) + cost}
    monitor.update(thread_ts, "mk_draft", "⏸ 案選択待ち", new_total)
    _show_proposals(channel, proposals, thread_ts)
    with pending_tasks_lock:
        pending_tasks[thread_ts] = {
            **task,
            "action":         "mk_draft_proposals",
            "proposals":      proposals,
            "hint":           new_hint,
            "total_cost_usd": new_total,
            "step_costs":     new_step_costs,
        }


def handle_mk_draft_outline(channel: str, text: str, task: dict, thread_ts: str) -> None:
    """Phase 3: 構成確認 → はい: Phase 4へ / 修正: 再生成"""

    topic   = task.get("topic", "")
    outline = task.get("outline", "")
    past_data = task.get("past_data", {})

    if text in CONFIRM_YES:
        # 構成確定 → 台本生成（スプシ列構造対応）
        post_message(channel, "✅ 構成確定！台本を作成中...", thread_ts)
        try:
            draft_dict, display_text, cost = generate_draft(topic, outline, past_data)
        except Exception as e:
            post_message(channel, f"❌ 台本の生成に失敗しました: {e}", thread_ts)
            return

        total_cost = task.get("total_cost_usd", 0) + cost

        # 長い場合は先頭をプレビューして表示
        if len(display_text) > 2500:
            preview = display_text[:500] + "\n\n...（続きは確定後のファイルをご確認ください）"
        else:
            preview = display_text

        msg = (
            f"📝 *台本を作成しました（スプシ列構造対応）*\n\n"
            f"{preview}\n\n"
            f"確定する場合は「*確定*」と送信してください。\n"
            f"修正がある場合は内容を教えてください。\n"
            f"（やめる場合は「キャンセル」）"
        )
        post_message(channel, msg, thread_ts)

        with pending_tasks_lock:
            pending_tasks[thread_ts] = {
                "action":           "mk_draft_review",
                "channel":          channel,
                "past_data":        past_data,
                "topic":            topic,
                "outline":          outline,
                "structured_draft": draft_dict,
                "draft_display":    display_text,
                "start_time":       task.get("start_time", datetime.now()),
                "total_cost_usd":   total_cost,
            }

    else:
        # 修正依頼 → 構成を再生成
        post_message(channel, "🔄 構成案を修正中...", thread_ts)
        try:
            revised_outline, cost = revise_outline(topic, outline, text)
        except Exception as e:
            post_message(channel, f"❌ 構成案の修正に失敗しました: {e}", thread_ts)
            return

        total_cost = task.get("total_cost_usd", 0) + cost

        msg = (
            f"📐 *構成案（修正版）*\n\n"
            f"{revised_outline}\n\n"
            f"このアウトラインで進めますか？\n"
            f"→ *はい* で台本作成へ / 修正点があれば入力"
        )
        post_message(channel, msg, thread_ts)

        with pending_tasks_lock:
            pending_tasks[thread_ts] = {
                **task,
                "outline":        revised_outline,
                "total_cost_usd": total_cost,
            }


def handle_mk_draft_review(channel: str, text: str, task: dict, thread_ts: str) -> None:
    """Phase 4: 台本レビュー → 確定: Phase 5へ / 修正: 再生成 / 手戻り: proposals へ"""

    # 手戻りチェック（proposals に戻る）
    if any(kw in text for kw in GOTO_PROPOSALS):
        proposals = task.get("proposals", [])
        if proposals:
            monitor.update(thread_ts, "mk_draft", "⏸ 案選択待ち", task.get("total_cost_usd", 0))
            _show_proposals(channel, proposals, thread_ts)
            with pending_tasks_lock:
                pending_tasks[thread_ts] = {**task, "action": "mk_draft_proposals"}
        else:
            post_message(channel, "案一覧がありません。新たに「台本作って」とお送りください。", thread_ts)
            monitor.done(thread_ts)
            with pending_tasks_lock:
                pending_tasks.pop(thread_ts, None)
        return

    topic           = task.get("topic", "")
    outline         = task.get("outline", "")
    structured_draft = task.get("structured_draft", {})

    if text in CONFIRM_FINALIZE:
        # 確定 → スプシ書き込み + output保存
        monitor.update(thread_ts, "mk_draft", "📊 スプシ書込中", task.get("total_cost_usd", 0))
        post_message(channel, "📊 スプレッドシートに書き込み中...", thread_ts)

        spreadsheet_url, sheets_err = write_to_sheets(
            DRAFT_SPREADSHEET_ID, structured_draft
        )

        elapsed, elapsed_str = _mk_draft_elapsed(task)
        cost_usd = task.get("total_cost_usd", 0)
        cost_jpy = cost_usd * 150

        # コスト内訳を組み立て
        step_costs = task.get("step_costs", {})
        breakdown_lines = []
        if step_costs.get("research", 0):
            c = step_costs["research"]
            breakdown_lines.append(f"  🔍 Webリサーチ (Sonnet): ${c:.4f} (約{c*150:.0f}円)")
        if step_costs.get("proposals", 0):
            c = step_costs["proposals"]
            breakdown_lines.append(f"  ✍️ 案サマリー生成 (Haiku): ${c:.4f} (約{c*150:.0f}円)")
        if step_costs.get("expand", 0):
            c = step_costs["expand"]
            breakdown_lines.append(f"  📝 台本生成 (Haiku): ${c:.4f} (約{c*150:.0f}円)")
        if step_costs.get("revise", 0):
            c = step_costs["revise"]
            breakdown_lines.append(f"  🔄 修正 (Haiku): ${c:.4f} (約{c*150:.0f}円)")
        breakdown = "\n💸 *コスト内訳:*\n" + "\n".join(breakdown_lines) if breakdown_lines else ""

        # output/ に保存（display_text を台本テキストとして使用）
        draft_display = task.get("draft_display", str(structured_draft))
        try:
            output_file = save_output(
                topic, outline, draft_display,
                elapsed_str, cost_usd, cost_jpy,
                spreadsheet_url,
            )
        except Exception as e:
            output_file = None
            monitor.log(f"⚠️ output保存エラー: {e}")

        # 履歴に保存
        save_history("mk_draft", topic, {
            "elapsed_seconds": elapsed,
            "cost_usd": cost_usd,
            "cost_jpy": cost_jpy,
        })

        monitor.done(thread_ts)
        with pending_tasks_lock:
            pending_tasks.pop(thread_ts, None)

        if sheets_err:
            summary = (
                f"✅ *台本が完成しました！*\n"
                f"⏱ {elapsed_str} | 💰 約{cost_jpy:.1f}円"
                f"{breakdown}\n"
                f"⚠️ スプシ書き込みエラー: {sheets_err}"
            )
        else:
            summary = (
                f"✅ *台本が完成しました！*\n"
                f"⏱ {elapsed_str} | 💰 約{cost_jpy:.1f}円"
                f"{breakdown}\n"
                f"📊 スプレッドシート: {spreadsheet_url}"
            )

        if output_file and output_file.exists():
            upload_file(channel, output_file, summary, thread_ts)
        else:
            post_message(channel, summary, thread_ts)

    else:
        # 修正依頼 → 台本を再生成
        monitor.update(thread_ts, "mk_draft", "🔄 台本修正中", task.get("total_cost_usd", 0))
        post_message(channel, "🔄 台本を修正中...", thread_ts)
        try:
            revised_dict, display_text, cost = revise_draft(topic, structured_draft, text)
        except Exception as e:
            monitor.update(thread_ts, "mk_draft", "⏸ レビュー待ち", task.get("total_cost_usd", 0))
            post_message(channel, f"❌ 台本の修正に失敗しました: {e}", thread_ts)
            return

        total_cost  = task.get("total_cost_usd", 0) + cost
        prev_sc     = task.get("step_costs", {})
        new_sc      = {**prev_sc, "revise": prev_sc.get("revise", 0) + cost}
        monitor.update(thread_ts, "mk_draft", "⏸ レビュー待ち", total_cost)

        if len(display_text) > 2500:
            content_block = display_text[:500] + "\n\n...（続きは確定後のファイルをご確認ください）"
        else:
            content_block = display_text

        msg = (
            f"📝 *台本（修正版）*\n\n"
            f"{content_block}\n\n"
            f"確定する場合は「*確定*」と送信してください。\n"
            f"修正がある場合は内容を教えてください。"
        )
        post_message(channel, msg, thread_ts)

        with pending_tasks_lock:
            pending_tasks[thread_ts] = {
                **task,
                "structured_draft": revised_dict,
                "draft_display":    display_text,
                "total_cost_usd":   total_cost,
                "step_costs":       new_sc,
            }


# ─────────────────────────────────────────────
# メッセージ処理（ステートマシン）
# agent-creation-guide.md §7 参照
# ─────────────────────────────────────────────

def process_message(channel: str, text: str, thread_ts: str) -> None:
    with pending_tasks_lock:
        task = pending_tasks.get(thread_ts)

    # ── キャンセル（全フェーズ共通）──
    if task and text.lower() in CANCEL_WORDS:
        monitor.done(thread_ts)
        with pending_tasks_lock:
            pending_tasks.pop(thread_ts, None)
        post_message(channel, "キャンセルしました。他に何かあれば声をかけてください。", thread_ts)
        return

    # ── トピック確認待ち (research) ──
    if task and task["action"] == "ask_research_topic":
        with pending_tasks_lock:
            pending_tasks.pop(thread_ts, None)
        topic = text.strip()
        _show_research_confirm(channel, topic, thread_ts)
        return

    # ── 確認待ち (research) ──
    if task and task["action"] == "confirm_research":
        if text in CONFIRM_YES:
            with pending_tasks_lock:
                pending_tasks.pop(thread_ts, None)
            topic = task["topic"]
            post_message(channel, "▶️ リサーチを開始します。完了したらここに返します。", thread_ts)
            t = threading.Thread(target=handle_research, args=(channel, topic, thread_ts), daemon=True)
            t.start()
            return
        if text in CONFIRM_NO:
            with pending_tasks_lock:
                pending_tasks.pop(thread_ts, None)
            post_message(channel, "キャンセルしました。", thread_ts)
            return
        # YES でも NO でもない → トピック更新
        topic = text.strip()
        _show_research_confirm(channel, topic, thread_ts)
        return

    # ── 確認待ち (mk_draft) ──
    if task and task["action"] == "confirm_mk_draft":
        if text in CONFIRM_NO:
            with pending_tasks_lock:
                pending_tasks.pop(thread_ts, None)
            post_message(channel, "キャンセルしました。", thread_ts)
            return
        # YES + 追加テキスト を検出（例: 「はい、食べ物系で」）
        matched_yes = text in CONFIRM_YES
        extra_hint  = ""
        if not matched_yes:
            for kw in sorted(CONFIRM_YES, key=len, reverse=True):
                if text.lower().startswith(kw.lower()) and len(text) > len(kw):
                    matched_yes = True
                    extra_hint  = text[len(kw):].strip().lstrip("、,， ")
                    break
        if matched_yes:
            with pending_tasks_lock:
                pending_tasks.pop(thread_ts, None)
            combined_hint = " / ".join(filter(None, [task.get("hint", ""), extra_hint]))
            t = threading.Thread(target=start_mk_draft, args=(channel, combined_hint, thread_ts), daemon=True)
            t.start()
            return

    # ── mk_draft フェーズハンドラー ──
    if task and task["action"] == "mk_draft_proposals":
        with pending_tasks_lock:
            pending_tasks.pop(thread_ts, None)
        t = threading.Thread(target=handle_mk_draft_proposals, args=(channel, text, task, thread_ts), daemon=True)
        t.start()
        return

    if task and task["action"] == "mk_draft_review":
        with pending_tasks_lock:
            pending_tasks.pop(thread_ts, None)
        t = threading.Thread(target=handle_mk_draft_review, args=(channel, text, task, thread_ts), daemon=True)
        t.start()
        return

    # ── 質問待ち (awaiting_clarification) ──
    if task and task["action"] == "awaiting_clarification":
        with pending_tasks_lock:
            pending_tasks.pop(thread_ts, None)
        original = task.get("original_message", "")
        combined = f"依頼: {original}\n追加情報: {text}"
        intent = classify_intent(combined)
        _dispatch_intent(channel, intent, text, thread_ts, original_message=original)
        return

    # ── ヘルプ ──
    if text in ("help", "ヘルプ", "使い方", "何ができる", "?", "？"):
        post_message(channel, HELP_TEXT, thread_ts)
        return

    # ── 新規メッセージ → 意図分類 ──
    intent = classify_intent(text)
    _dispatch_intent(channel, intent, text, thread_ts, original_message=text)


def _show_research_confirm(channel: str, topic: str, thread_ts: str) -> None:
    """リサーチ確認メッセージを表示してステートをセット"""
    estimate = get_estimate("research")
    info     = AGENT_INFO["research"]
    confirm_msg = (
        f"📋 *タスクを受け付けました*\n\n"
        f"🤖 *{info['name']}* で対応できます\n"
        f"📝 リサーチ対象: _{topic}_\n"
        f"⏱ 予想時間: {estimate['time']}\n"
        f"💰 推定費用: {estimate['cost']}\n"
        f"　（{estimate['note']}）\n\n"
        f"実行しますか？ → *はい* または *いいえ*\n"
        f"リサーチ対象を変更する場合はテキストで入力してください。"
    )
    with pending_tasks_lock:
        pending_tasks[thread_ts] = {"action": "confirm_research", "topic": topic, "channel": channel}
    post_message(channel, confirm_msg, thread_ts)


def _dispatch_intent(
    channel: str,
    intent: dict,
    text: str,
    thread_ts: str,
    original_message: str = "",
) -> None:
    """classify_intent の結果をもとにアクションを実行する"""
    action = intent.get("action")

    # ── research ──
    if action == "research":
        topic = intent.get("topic", "").strip()

        # トピックが曖昧な場合はまず確認
        vague_words = {"リサーチ", "調べて", "調査", "リサーチして", "調べ", "調査して",
                       "リサーチしてほしい", "調べてほしい"}
        is_vague = (
            not topic
            or topic == text.strip()
            or topic in vague_words
            or (len(topic) < 8 and any(w in topic for w in {"リサーチ", "調べ", "調査"}))
        )
        if is_vague:
            ask_msg = (
                "🔍 *何をリサーチしますか？*\n\n"
                "具体的なトピックを教えてください。\n"
                "（例: 「TikTok トレンド 2026」「Instagram リール 伸びる投稿パターン」）"
            )
            with pending_tasks_lock:
                pending_tasks[thread_ts] = {"action": "ask_research_topic", "channel": channel}
            post_message(channel, ask_msg, thread_ts)
            return

        _show_research_confirm(channel, topic, thread_ts)

    # ── mk_draft ──
    elif action == "mk_draft":
        hint     = intent.get("hint", "")
        estimate = get_estimate("mk_draft")
        info     = AGENT_INFO["mk_draft"]
        hint_note = f"📌 指示: _{hint}_\n" if hint else ""
        confirm_msg = (
            f"📋 *タスクを受け付けました*\n\n"
            f"🤖 *{info['name']}* で対応できます\n"
            f"📝 {info['label']}\n"
            f"{hint_note}"
            f"⏱ 予想時間: {estimate['time']}\n"
            f"💰 推定費用: {estimate['cost']}\n"
            f"　（{estimate['note']}）\n\n"
            f"実行しますか？ → *はい* または *いいえ*\n"
            f"希望があれば「はい、○○系で」のように添えてください"
        )
        with pending_tasks_lock:
            pending_tasks[thread_ts] = {"action": "confirm_mk_draft", "hint": hint, "channel": channel}
        post_message(channel, confirm_msg, thread_ts)

    # ── 質問 ──
    elif action == "ask":
        question = intent.get("question", "もう少し詳しく教えていただけますか？")
        with pending_tasks_lock:
            pending_tasks[thread_ts] = {
                "action": "awaiting_clarification",
                "original_message": original_message,
                "channel": channel,
            }
        post_message(channel, question, thread_ts)

    # ── 直接回答 ──
    elif action == "chat":
        reply = intent.get("reply", "すみません、うまく理解できませんでした。")
        post_message(channel, reply, thread_ts)

    else:
        post_message(channel, "すみません、うまく理解できませんでした。\n何をお手伝いできますか？", thread_ts)


# ─────────────────────────────────────────────
# Socket Mode ハンドラー
# ─────────────────────────────────────────────

def on_events_api(client: SocketModeClient, req: SocketModeRequest) -> None:
    client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))

    if req.type != "events_api":
        return

    event = req.payload.get("event", {})

    if event.get("type") != "message":
        return
    if event.get("bot_id"):
        return
    if event.get("subtype"):
        return

    channel = event.get("channel", "")
    text    = event.get("text", "").strip()

    if not text or not channel:
        return

    if not channel.startswith("D"):
        return

    msg_ts    = event.get("ts", "")
    thread_ts = event.get("thread_ts", msg_ts)

    t = threading.Thread(target=process_message, args=(channel, text, thread_ts), daemon=True)
    t.start()


# ─────────────────────────────────────────────
# エントリーポイント
# ─────────────────────────────────────────────

def main():
    print("=" * 60)
    print("🤖 Slack Bot 起動中...")
    print(f"   ルート: {PROJECT_ROOT}")
    print("   DM を送るとエージェントが応答します。Ctrl+C で停止")
    print("=" * 60)

    socket_client = SocketModeClient(app_token=SLACK_APP_TOKEN, web_client=web_client)
    socket_client.socket_mode_request_listeners.append(on_events_api)
    socket_client.connect()
    print("✅ 接続完了！Slack で DM を送ってみてください。")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n👋 Bot を停止します...")
        socket_client.close()
        sys.exit(0)


if __name__ == "__main__":
    main()
