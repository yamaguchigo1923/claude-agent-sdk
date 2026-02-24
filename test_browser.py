import asyncio
from dotenv import load_dotenv
from claude_agent_sdk import query, ClaudeAgentOptions

load_dotenv()

async def main():
    print("ブラウザ操作テスト...")
    async for message in query(
        prompt="https://qiita.com/akira_papa_AI/items/f6b342f7d67e097287eb をブラウザで開いて、ページのタイトルと本文を教えてください。",
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

asyncio.run(main())
