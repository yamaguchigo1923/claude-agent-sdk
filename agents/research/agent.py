"""
SNS Marketing Research Agent
呼び出し可能なモジュールとして提供。Slack Bot や CLI から使用する。
"""

import asyncio
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from claude_agent_sdk import AgentDefinition, ClaudeAgentOptions, ClaudeSDKClient
from claude_agent_sdk.types import AssistantMessage, ResultMessage, TextBlock

# ─────────────────────────────────────────────
# 設定
# ─────────────────────────────────────────────

MAX_TURNS = 5
TIMEOUT_MINUTES = 20

# claude-haiku-4-5 の料金（USD per 1M tokens）
INPUT_COST_PER_M = 0.80
OUTPUT_COST_PER_M = 4.00
USD_TO_JPY = 150

# デフォルト出力先: claude-agent-sdk/output/
DEFAULT_OUTPUT_DIR = Path(__file__).parent.parent.parent / "output"

# ─────────────────────────────────────────────
# プロンプト定義
# ─────────────────────────────────────────────

SYSTEM_PROMPT = """あなたはSEOを考慮したSNSマーケティング記事のための専門リサーチエージェントです。

## あなたの役割
- SNS（Instagram、X/Twitter、TikTok、YouTube等）のトレンドを自律的に調査する
- SEO観点から価値の高いキーワードやトピックを収集する
- 記事執筆に活用できる具体的な情報・データ・成功事例を集める

## リサーチの進め方
1. trend-scout でSNSトレンドを調査
2. seo-analyst でキーワード分析
3. content-strategist で記事テーマ・タイトル案を作成
4. 収集した全情報を指定ファイルに保存して完了

## 重要な指針
- 各サブエージェントは1回だけ呼び出す（繰り返し呼び出さない）
- WebSearchは各サブエージェントで最大3回まで
- 情報収集が完了したら速やかにファイルに保存して終了する
- 日本市場に特化した情報を重視する
- 収集した情報は必ず指定ファイルに保存する
"""

TREND_SCOUT_PROMPT = """あなたはSNSトレンド調査の専門家です。
WebSearchを最大3回使って以下を簡潔に調査してください：
- 指定トピックに関連するSNSトレンド（Instagram・X・TikTok・YouTube）
- 最近バズっているコンテンツの傾向
- 人気ハッシュタグ

調査したら結果をテキストでまとめて返してください。ファイル保存は不要です。"""

SEO_ANALYST_PROMPT = """あなたはSEOキーワード分析の専門家です。
WebSearchを最大3回使って以下を簡潔に調査してください：
- 関連キーワードの検索トレンド
- 競合記事のタイトル傾向
- ロングテールキーワード候補

調査したら結果をテキストでまとめて返してください。ファイル保存は不要です。"""

CONTENT_STRATEGIST_PROMPT = """あなたはコンテンツ戦略立案の専門家です。
提供された情報を元に以下を提案してください（WebSearch不要）：
- 記事テーマ10件
- 各テーマのタイトル案3つ
- 見出し構成案

結果をテキストでまとめて返してください。"""


# ─────────────────────────────────────────────
# メイン関数
# ─────────────────────────────────────────────

async def run_research(topic: str, output_dir: Optional[Path] = None) -> dict:
    """
    SNSマーケティングリサーチを実行する。

    Args:
        topic: リサーチトピック
        output_dir: 出力ディレクトリ（省略時は claude-agent-sdk/output/）

    Returns:
        dict: {
            "output_file": Path | None,
            "cost_usd": float,
            "cost_jpy": float,
            "elapsed_str": str,
            "error": str | None,
        }
    """
    if output_dir is None:
        output_dir = DEFAULT_OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file = output_dir / f"research_{timestamp}.md"
    output_file_str = str(output_file).replace("\\", "/")

    start_time = datetime.now()

    print(f"\n{'='*60}")
    print("SNS Marketing Research Agent 起動")
    print(f"リサーチトピック: {topic}")
    print(f"出力ファイル    : {output_file}")
    print(f"最大ターン数    : {MAX_TURNS}")
    print(f"タイムアウト    : {TIMEOUT_MINUTES}分")
    print(f"{'='*60}\n")

    options = ClaudeAgentOptions(
        model="claude-haiku-4-5-20251001",
        system_prompt=SYSTEM_PROMPT,
        allowed_tools=["WebSearch", "WebFetch", "Write", "Read", "Task"],
        permission_mode="bypassPermissions",
        max_turns=MAX_TURNS,
        agents={
            "trend-scout": AgentDefinition(
                description=(
                    "SNSトレンドとバイラルコンテンツの調査専門エージェント。"
                    "Instagram・X・TikTok・YouTubeの最新トレンド、ハッシュタグ、バズコンテンツを調査します。"
                ),
                prompt=TREND_SCOUT_PROMPT,
                tools=["WebSearch", "WebFetch"],
            ),
            "seo-analyst": AgentDefinition(
                description=(
                    "SEOキーワード分析の専門エージェント。"
                    "検索トレンド・競合分析・キーワード戦略・ロングテールキーワードを担当します。"
                ),
                prompt=SEO_ANALYST_PROMPT,
                tools=["WebSearch"],
            ),
            "content-strategist": AgentDefinition(
                description=(
                    "コンテンツ戦略立案の専門エージェント。"
                    "収集した情報を元に記事テーマ・構成・タイトル案を提案します。"
                ),
                prompt=CONTENT_STRATEGIST_PROMPT,
                tools=[],
            ),
        },
    )

    today = datetime.now().strftime("%Y年%m月%d日")
    prompt = f"""以下のトピックについてリサーチを実行してください。

## リサーチトピック
{topic}

## 実行手順（この順番で必ず実行し、各エージェントは1回だけ呼ぶ）
1. **trend-scout** でSNSトレンドを調査
2. **seo-analyst** でキーワード分析
3. **content-strategist** で記事テーマ・タイトル案を作成
4. 収集した全情報を以下のファイルに保存して完了: `{output_file_str}`

## 保存するファイルの形式
```markdown
# リサーチレポート: {topic}
実施日: {today}

## 1. SNSトレンド分析
### 1.1 主要プラットフォーム別トレンド
### 1.2 注目コンテンツの傾向
### 1.3 注目ハッシュタグ・キーワード

## 2. SEOキーワード分析
### 2.1 主要キーワード
### 2.2 ロングテールキーワード
### 2.3 ユーザーの検索意図

## 3. コンテンツ戦略提案
### 3.1 推奨記事テーマ一覧
### 3.2 推奨記事タイトル案（上位10件）
### 3.3 コンテンツ作成のポイント

## 4. 参考情報源
```

ファイルを保存したら必ず終了してください。追加の調査は不要です。"""

    total_input_tokens = 0
    total_output_tokens = 0
    cost_usd = 0.0

    try:
        async with asyncio.timeout(TIMEOUT_MINUTES * 60):
            async with ClaudeSDKClient(options=options) as client:
                await client.query(prompt)

                print("リサーチ中... (エージェントが自律的に情報収集しています)\n")

                async for message in client.receive_response():
                    if isinstance(message, AssistantMessage):
                        if hasattr(message, "usage") and message.usage:
                            total_input_tokens += getattr(message.usage, "input_tokens", 0)
                            total_output_tokens += getattr(message.usage, "output_tokens", 0)
                        for block in message.content:
                            if isinstance(block, TextBlock):
                                print(block.text, end="", flush=True)

                    elif isinstance(message, ResultMessage):
                        elapsed = (datetime.now() - start_time).seconds
                        elapsed_str = f"{elapsed // 60}分{elapsed % 60}秒"

                        if hasattr(message, "total_cost_usd") and message.total_cost_usd:
                            cost_usd = message.total_cost_usd
                        else:
                            cost_usd = (
                                total_input_tokens / 1_000_000 * INPUT_COST_PER_M
                                + total_output_tokens / 1_000_000 * OUTPUT_COST_PER_M
                            )
                        cost_jpy = cost_usd * USD_TO_JPY

                        print(f"\n\n{'='*60}")
                        print(f"⏱  実行時間: {elapsed_str}")
                        if total_input_tokens or total_output_tokens:
                            print(f"📊 トークン使用量:")
                            print(f"   入力: {total_input_tokens:,} tokens")
                            print(f"   出力: {total_output_tokens:,} tokens")
                        if cost_usd > 0:
                            print(f"💰 推定コスト: ${cost_usd:.4f} USD (約 {cost_jpy:.1f} 円)")

                        if message.is_error:
                            print(f"❌ エラー: {message.result}")
                            print(f"{'='*60}\n")
                            return {
                                "output_file": None,
                                "elapsed_seconds": elapsed,
                                "cost_usd": cost_usd,
                                "cost_jpy": cost_jpy,
                                "elapsed_str": elapsed_str,
                                "error": message.result,
                            }

                        print("✅ リサーチ完了!")
                        if output_file.exists():
                            size_kb = output_file.stat().st_size / 1024
                            print(f"📄 結果ファイル: {output_file} ({size_kb:.1f} KB)")
                        print(f"{'='*60}\n")

                        return {
                            "output_file": output_file if output_file.exists() else None,
                            "elapsed_seconds": elapsed,
                            "cost_usd": cost_usd,
                            "cost_jpy": cost_jpy,
                            "elapsed_str": elapsed_str,
                            "error": None,
                        }

    except asyncio.TimeoutError:
        elapsed = (datetime.now() - start_time).seconds
        elapsed_str = f"{elapsed // 60}分{elapsed % 60}秒"
        print(f"\n\n{'='*60}")
        print(f"⏰ タイムアウト: {TIMEOUT_MINUTES}分を超えました（{elapsed_str}経過）")
        print(f"{'='*60}\n")
        return {
            "output_file": output_file if output_file.exists() else None,
            "elapsed_seconds": elapsed,
            "cost_usd": cost_usd,
            "cost_jpy": cost_usd * USD_TO_JPY,
            "elapsed_str": elapsed_str,
            "error": f"タイムアウト（{TIMEOUT_MINUTES}分）",
        }

    # フォールバック（通常は ResultMessage ハンドラーで return される）
    elapsed = (datetime.now() - start_time).seconds
    return {
        "output_file": output_file if output_file.exists() else None,
        "elapsed_seconds": elapsed,
        "cost_usd": cost_usd,
        "cost_jpy": cost_usd * USD_TO_JPY,
        "elapsed_str": f"{elapsed // 60}分{elapsed % 60}秒",
        "error": None,
    }
