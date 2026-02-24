# Claude Agent SDK セットアップ & 使い方まとめ

## 概要

Claude Agent SDK は、Claude Code の技術をPython/TypeScriptライブラリとして使えるようにしたもの。
ブラウザ操作・ファイル操作・コマンド実行・Web検索などのツールをエージェントが自律的に使える。

**Claude Code CLI とは別物ではなく、同じ技術のライブラリ版。**
- Claude Code CLI → インタラクティブな開発用
- Claude Agent SDK → Pythonから呼び出す業務自動化用

---

## 環境情報

- Python: 3.13.7
- Node.js: v22.19.0
- claude-agent-sdk: 0.1.41
- Claude Code CLI: 2.1.52

---

## セットアップ手順

### 1. プロジェクト作成

```bash
mkdir business-agent
cd business-agent
python -m venv .venv
source .venv/Scripts/activate  # Windows (Git Bash)
```

### 2. パッケージインストール

```bash
pip install claude-agent-sdk
pip install slack_sdk
pip install python-dotenv
```

### 3. APIキーの取得

| キー | 取得場所 |
|---|---|
| `ANTHROPIC_API_KEY` | https://platform.claude.com/settings/keys |
| `SLACK_BOT_TOKEN` | https://api.slack.com/apps → OAuth & Permissions |
| `SLACK_USER_ID_GO` | Slackプロフィール → … → メンバーIDをコピー |

### 4. .env ファイル

```
ANTHROPIC_API_KEY=sk-ant-api03-...
SLACK_BOT_TOKEN=xoxb-...
SLACK_USER_ID_GO=U...
```

---

## 重要な注意点

### Claude Code の中からは実行できない

Claude Agent SDK は内部で Claude Code のサブプロセスを起動する。
そのため、Claude Code (Claude Code CLI / VSCode拡張) の中から Python スクリプトを実行すると以下のエラーが出る：

```
Error: Claude Code cannot be launched inside another Claude Code session.
```

**→ 必ず自分のターミナル（Git Bash等）から実行すること。**

### APIキーの種類に注意

| 形式 | 種類 | Agent SDKで使えるか |
|---|---|---|
| `sk-ant-oat01-...` | OAuth トークン（Claude.ai用） | ❌ 不可 |
| `sk-ant-api03-...` | Anthropic API キー | ✅ 可 |

Claude Pro プランとは別に、platform.claude.com でAPIアカウントを作成して課金設定が必要。

### モデル選択

```python
model="claude-haiku-4-5-20251001"   # 最安値・テスト向け
model="claude-sonnet-4-5"           # バランス型・デフォルト
model="claude-opus-4-6"             # 最高性能・高コスト
```

---

## 基本的な使い方

### シンプルなクエリ

```python
import asyncio
from dotenv import load_dotenv
from claude_agent_sdk import query, ClaudeAgentOptions

load_dotenv()

async def main():
    async for message in query(
        prompt="やらせたいことをここに書く",
        options=ClaudeAgentOptions(
            allowed_tools=["Bash", "Read", "Write"],
            model="claude-haiku-4-5-20251001",
        ),
    ):
        if hasattr(message, "result"):
            print(message.result)

asyncio.run(main())
```

### 主要オプション

| オプション | 説明 | 例 |
|---|---|---|
| `allowed_tools` | 使えるツールを指定 | `["Bash", "Read", "Write", "Glob", "Grep"]` |
| `model` | 使用モデル | `"claude-haiku-4-5-20251001"` |
| `permission_mode` | 許可モード | `"bypassPermissions"` |
| `mcp_servers` | MCP サーバー接続 | Playwright等 |

### 使えるツール一覧

| ツール | 用途 |
|---|---|
| `Read` | ファイル読み込み |
| `Write` | ファイル作成・上書き |
| `Edit` | ファイル編集 |
| `Bash` | コマンド実行 |
| `Glob` | ファイルパターン検索 |
| `Grep` | ファイル内容検索 |
| `WebFetch` | URLからコンテンツ取得 |
| `WebSearch` | Web検索 |
| `Task` | サブエージェント起動 |

---

## ブラウザ操作（Playwright MCP）

### インストール

```bash
npx @playwright/mcp@latest --version  # 初回インストール
```

### 使い方

```python
async for message in query(
    prompt="https://example.com を開いてタイトルを教えて",
    options=ClaudeAgentOptions(
        model="claude-haiku-4-5-20251001",
        permission_mode="bypassPermissions",
        mcp_servers={
            "playwright": {
                "command": "npx",
                "args": ["@playwright/mcp@latest"],
            }
        },
    ),
):
    if hasattr(message, "result"):
        print(message.result)
```

**ポイント：** `permission_mode="bypassPermissions"` が必要。ないとツール使用の許可を求めてきて止まる。

---

## Slack 連携（slack_sdk）

### Slack Bot の準備

1. https://api.slack.com/apps でアプリ作成
2. **OAuth & Permissions** で以下のスコープを追加：
   - `chat:write` - メッセージ送信
   - `channels:read` - チャンネル一覧
   - `im:write` - DM を開く
3. **Install to Workspace** → Bot Token (`xoxb-...`) を取得

### DM 送信

```python
import os
from dotenv import load_dotenv
from slack_sdk import WebClient

load_dotenv()

client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
user_id = os.environ["SLACK_USER_ID_GO"]

# DM チャンネルを開いてメッセージ送信
dm = client.conversations_open(users=[user_id])
channel_id = dm["channel"]["id"]

client.chat_postMessage(
    channel=channel_id,
    text="送りたいメッセージ"
)
```

---

## 組み合わせパターン（ブラウザ取得 → Slack通知）

```python
import asyncio
import os
from dotenv import load_dotenv
from claude_agent_sdk import query, ClaudeAgentOptions
from slack_sdk import WebClient

load_dotenv()

slack = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
user_id = os.environ["SLACK_USER_ID_GO"]

async def notify_slack(text: str):
    dm = slack.conversations_open(users=[user_id])
    slack.chat_postMessage(channel=dm["channel"]["id"], text=text)

async def main():
    result_text = ""
    async for message in query(
        prompt="https://example.com を開いて内容を要約してください",
        options=ClaudeAgentOptions(
            model="claude-haiku-4-5-20251001",
            permission_mode="bypassPermissions",
            mcp_servers={
                "playwright": {"command": "npx", "args": ["@playwright/mcp@latest"]}
            },
        ),
    ):
        if hasattr(message, "result"):
            result_text = message.result

    if result_text:
        await notify_slack(result_text)
        print("Slack に送信しました")

asyncio.run(main())
```

---

## ファイル構成

```
business-agent/
├── .venv/               # 仮想環境
├── .env                 # APIキー（Gitに含めない）
├── .gitignore
├── document/
│   └── ClaudeAgentSDK.md  # このファイル
├── test_agent.py        # 基本動作確認
├── test_browser.py      # ブラウザ操作確認
└── test_slack.py        # Slack DM 送信確認
```
