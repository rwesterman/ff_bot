import os
import logging
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

client = WebClient(token=os.environ.get("SLACK_BOT_TOKEN"))
logger = logging.getLogger("__name__")

channel_name = "longview-ff"
conversation_id = None

try:
    for result in client.conversations_list():
        if conversation_id is not None:
            break
        for channel in result["channels"]:
            print(channel["name"])
            if channel["name"] == channel_name:
                conversation_id = channel["id"]
                print(f"Found conversation ID: {conversation_id}")
                break

except SlackApiError as e:
    print(f"Error: {e}")

# ID of channel that the message exists in
conversation_id = conversation_id

try:
    # Call the conversations.history method using the WebClient
    # The client passes the token you included in initialization
    result = client.conversations_history(channel=conversation_id, inclusive=True, oldest="1610144875.000600", limit=1)

    message = result["messages"][0]
    # Print message text
    print(message["text"])

except SlackApiError as e:
    print(f"Error: {e}")

# try:
#     # Call the conversations.history method using the WebClient
#     # conversations.history returns the first 100 messages by default
#     # These results are paginated, see: https://api.slack.com/methods/conversations.history$pagination
#     result = client.conversations_history(channel=conversation_id)

#     conversation_history = result["messages"]

#     # Print results
#     logger.info("{} messages found in {}".format(len(conversation_history), id))

# except SlackApiError as e:
#     logger.error("Error creating conversation: {}".format(e))
