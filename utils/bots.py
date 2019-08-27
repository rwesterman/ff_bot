import requests
import json


class GroupMeException(Exception):
	pass

class SlackException(Exception):
	pass

class DiscordException(Exception):
	pass

class GroupMeBot(object):
	#Creates GroupMe Bot to send messages
	def __init__(self, bot_id):
		self.bot_id = bot_id

	def __repr__(self):
		return "GroupMeBot(%s)" % self.bot_id

	def send_message(self, text):
		#Sends a message to the chatroom
		template = {
					"bot_id": self.bot_id,
					"text": text,
					"attachments": []
					}

		headers = {'content-type': 'application/json'}

		if self.bot_id not in (1, "1", ''):
			r = requests.post("https://api.groupme.com/v3/bots/post",
							  data=json.dumps(template), headers=headers)
			if r.status_code != 202:
				raise GroupMeException('Invalid BOT_ID')

			return r

class SlackBot(object):
	#Creates GroupMe Bot to send messages
	def __init__(self, webhook_url):
		self.webhook_url = webhook_url

	def __repr__(self):
		return "Slack Webhook Url(%s)" % self.webhook_url

	def send_message(self, text):
		#Sends a message to the chatroom
		message = "```{0}```".format(text)
		template = {
					"text":message
					}

		headers = {'content-type': 'application/json'}

		if self.webhook_url not in (1, "1", ''):
			r = requests.post(self.webhook_url,
							  data=json.dumps(template), headers=headers)

			if r.status_code != 200:
				raise SlackException('WEBHOOK_URL')

			return r

class DiscordBot(object):
	#Creates Discord Bot to send messages
	def __init__(self, webhook_url):
		self.webhook_url = webhook_url

	def __repr__(self):
		return "Discord Webhook Url(%s)" % self.webhook_url

	def send_message(self, text):
		#Sends a message to the chatroom
		message = "```{0}```".format(text)
		template = {
					"content":message
					}

		headers = {'content-type': 'application/json'}

		if self.webhook_url not in (1, "1", ''):
			r = requests.post(self.webhook_url,
							  data=json.dumps(template), headers=headers)

			if r.status_code != 204:
				raise DiscordException('WEBHOOK_URL')

			return r