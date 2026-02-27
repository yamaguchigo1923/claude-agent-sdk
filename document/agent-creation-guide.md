# エージェント作成ガイド

> **新しいエージェントを作るときは必ずこのドキュメントを参照してください。**
> 全エージェントはこの規約に従って作成することで、Slackプラットフォームへの統合が一貫して行えます。

---

## ディレクトリ構成（テンプレート）

```
agents/
└── {agent_name}/
    ├── __init__.py       # 公開関数のエクスポート
    ├── agent.py          # コアロジック
    ├── output/           # サイクルごとの出力MD（.gitignore に追加すること）
    └── history.json      # 実行履歴（自動生成）
```

---

## 1. agent.py の標準構造

### 必須設定定数

```python
MODEL = "claude-haiku-4-5-20251001"   # コスト最適化モデル
INPUT_COST_PER_M  = 0.80              # USD per 1M input tokens
OUTPUT_COST_PER_M = 4.00              # USD per 1M output tokens
USD_TO_JPY = 150

OUTPUT_DIR = Path(__file__).parent / "output"
```

### run 関数の戻り値フォーマット（全エージェント共通）

全てのエージェントは以下のdictを返す。HITL型は各フェーズ関数が部分的なコストを返す。

```python
{
    "output_file":      Path | None,  # agents/{name}/output/{name}_YYYYMMDD_HHMMSS.md
    "elapsed_seconds":  int,          # 実行時間（秒）
    "elapsed_str":      str,          # 表示用 "X分X秒"
    "cost_usd":         float,        # 推定コスト（USD）
    "cost_jpy":         float,        # 推定コスト（円）
    "error":            str | None,   # エラーメッセージ（なければ None）
}
```

---

## 2. 時間・コスト集計パターン

### 自律型エージェント（claude-agent-sdk 使用）

```python
start_time = datetime.now()
total_input_tokens = 0
total_output_tokens = 0
cost_usd = 0.0

async for message in client.receive_response():
    if isinstance(message, AssistantMessage):
        if hasattr(message, "usage") and message.usage:
            total_input_tokens += getattr(message.usage, "input_tokens", 0)
            total_output_tokens += getattr(message.usage, "output_tokens", 0)

    elif isinstance(message, ResultMessage):
        elapsed = (datetime.now() - start_time).seconds
        elapsed_str = f"{elapsed // 60}分{elapsed % 60}秒"

        # total_cost_usd が使えれば優先
        if hasattr(message, "total_cost_usd") and message.total_cost_usd:
            cost_usd = message.total_cost_usd
        else:
            cost_usd = (
                total_input_tokens / 1_000_000 * INPUT_COST_PER_M
                + total_output_tokens / 1_000_000 * OUTPUT_COST_PER_M
            )
        cost_jpy = cost_usd * USD_TO_JPY
```

### HITL型エージェント（Anthropic SDK 直接使用）

各フェーズ関数でコストを返し、呼び出し側で積算する。

```python
# フェーズ関数側
def some_phase(...) -> tuple[str, float]:
    response = client.messages.create(...)
    cost = (
        response.usage.input_tokens / 1_000_000 * INPUT_COST_PER_M
        + response.usage.output_tokens / 1_000_000 * OUTPUT_COST_PER_M
    )
    return response.content[0].text.strip(), cost

# 呼び出し側（slack_bot）で積算
task["total_cost_usd"] += cost_from_phase
```

---

## 3. output/ への保存パターン

```python
def save_output(...) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file = OUTPUT_DIR / f"{AGENT_NAME}_{timestamp}.md"

    content = f"""# {AGENT_NAME}: {topic}
作成日: {datetime.now().strftime("%Y年%m月%d日 %H:%M")}

{main_content}

---

## 実行サマリー
- ⏱ 実行時間: {elapsed_str}
- 💰 推定コスト: ${cost_usd:.4f} USD (約 {cost_jpy:.1f} 円)
"""
    output_file.write_text(content, encoding="utf-8")
    return output_file
```

---

## 4. history.json の読み書きパターン

`slack_bot/main.py` の `HISTORY_FILES` に追加し、既存の `load_history` / `save_history` / `get_estimate` を使う。

```python
# slack_bot/main.py に追加
HISTORY_FILES = {
    "research":  PROJECT_ROOT / "agents" / "research"  / "history.json",
    "mk_draft":  PROJECT_ROOT / "agents" / "mk_draft"  / "history.json",
    # "{new_agent}": PROJECT_ROOT / "agents" / "{new_agent}" / "history.json",
}
```

```python
# history.json のエントリ形式
{
    "timestamp":        "2026-02-25T13:51:05",
    "topic":            "調査したトピック or 作成したテーマ",
    "elapsed_seconds":  512,
    "cost_usd":         0.35,
    "cost_jpy":         52.6,
}
```

---

## 5. get_estimate() パターン（history.json から平均見積もり）

`slack_bot/main.py` の既存関数をそのまま使う。エージェント名を渡すだけ。

```python
estimate = get_estimate("new_agent_name")
# → {"time": "X〜Y分", "cost": "約X〜Y円", "note": "過去N件の実績より"}
```

初回は `AGENT_INFO` のデフォルト値を表示し、2回目以降は自動的に実績ベースに切り替わる。

---

## 6. slack_bot/main.py へのルーティング追加手順

### ① AGENT_INFO に追加

```python
AGENT_INFO = {
    "research": { ... },
    "{agent_name}": {
        "name":  "{agent_name}-agent",
        "label": "エージェントの説明",
        "time":  "X〜Y分",   # 初回概算
        "cost":  "約X〜Y円", # 初回概算
    },
}
```

### ② ROUTING_SYSTEM プロンプトに追加

```
利用可能なエージェント:
- research: SNS・SEOのリサーチ
- {agent_name}: {何をするエージェントか}

{agent_name}の依頼の場合:
{{"action": "{agent_name}", "hint": "追加指示があれば"}}
```

### ③ _dispatch_intent に分岐を追加

```python
elif action == "{agent_name}":
    hint = intent.get("hint", "")
    info = AGENT_INFO["{agent_name}"]
    estimate = get_estimate("{agent_name}")
    confirm_msg = (
        f"📋 *タスクを受け付けました*\n\n"
        f"🤖 *{info['name']}* で対応できます\n"
        f"📝 {info['label']}\n"
        f"⏱ 予想時間: {estimate['time']}\n"
        f"💰 推定費用: {estimate['cost']}\n"
        f"　（{estimate['note']}）\n\n"
        f"実行しますか？ → *はい* または *いいえ*"
    )
    with pending_tasks_lock:
        pending_tasks[thread_ts] = {
            "action": "confirm_{agent_name}",
            "hint": hint,
            "channel": channel,
        }
    post_message(channel, confirm_msg, thread_ts)
```

---

## 7. Human-in-the-loop（HITL）ステートマシンパターン

### ステート設計

```
confirm_{agent}     → はい → start_phase1 → {agent}_phase2
{agent}_phase2      → 選択/指示 → start_phase3 → {agent}_phase3
{agent}_phase3      → はい or 修正 → (修正ループ or 次フェーズ)
{agent}_review      → 確定 → finalize → 完了
                     → 修正テキスト → revise → {agent}_review (ループ)
```

### pending_tasks のデータ構造

```python
pending_tasks[thread_ts] = {
    "action":           "current_phase_name",
    "channel":          "D...",
    "start_time":       datetime,      # 最初の開始時刻
    "total_cost_usd":   0.0,           # フェーズ跨ぎで積算
    # フェーズごとの中間データ
    "phase2_result":    "...",
    "phase3_result":    "...",
}
```

### キャンセル処理

全フェーズで以下を先頭でチェックする。

```python
CANCEL_WORDS = {"やめる", "キャンセル", "cancel", "やめて", "中止", "stop"}

if text.lower() in CANCEL_WORDS:
    with pending_tasks_lock:
        pending_tasks.pop(thread_ts, None)
    post_message(channel, "キャンセルしました。", thread_ts)
    return
```

### 確定ワード

```python
CONFIRM_FINALIZE = {"確定", "OK", "ok", "はい", "yes", "承認", "よし", "いいよ", "👍"}
```

---

## 8. .gitignore への追加

新エージェントを作るたびに `.gitignore` に追加する。

```
agents/{new_agent}/output/
```

---

## 9. チェックリスト（新エージェント作成時）

- [ ] `agents/{name}/` ディレクトリを作成
- [ ] `__init__.py`, `agent.py` を作成
- [ ] `output/` ディレクトリを作成（空ファイルは不要）
- [ ] `history.json` は自動生成されるので不要
- [ ] `.gitignore` に `agents/{name}/output/` を追加
- [ ] `pyproject.toml` に必要な依存を追加
- [ ] `slack_bot/main.py` の `HISTORY_FILES`, `AGENT_INFO`, `ROUTING_SYSTEM`, `_dispatch_intent`, `process_message` を更新
- [ ] `agents/{name}/__init__.py` に公開関数をエクスポート
- [ ] run 戻り値に `output_file`, `elapsed_seconds`, `cost_usd`, `cost_jpy`, `error` を含める
- [ ] `save_history()` を呼んで実行後に履歴を保存する
