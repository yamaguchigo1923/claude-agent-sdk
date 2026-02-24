import asyncio
import os
from dotenv import load_dotenv
from claude_agent_sdk import query, ClaudeAgentOptions

load_dotenv()

async def main():
    print("Claude Agent SDK 動作確認...")
    async for message in query(
        prompt="こんにちは。今日の日付と、あなたが使えるツールを教えてください。",
        options=ClaudeAgentOptions(
            allowed_tools=["Bash"],
            model="claude-haiku-4-5-20251001",  # 最安値モデル
        ),
    ):
        if hasattr(message, "result"):
            print(message.result)

asyncio.run(main())
