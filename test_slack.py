import os
from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

load_dotenv()

client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
user_id = os.environ["SLACK_USER_ID_GO"]

try:
    # DM チャンネルを開く
    dm = client.conversations_open(users=[user_id])
    channel_id = dm["channel"]["id"]

    # メッセージ送信
    client.chat_postMessage(
        channel=channel_id,
        text="Claude Agent SDK からのテストメッセージです :robot_face:"
    )
    print("✅ DM を送信しました！")

except SlackApiError as e:
    print(f"エラー: {e.response['error']}")
