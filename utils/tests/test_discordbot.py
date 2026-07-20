import pytest


from utils.bots import (
    DiscordBot,
    DiscordException,
)


class TestDiscordBot:
    """Test DiscordBot class"""

    url = "https://discordapp.com/api/webhooks/123/abc"
    test_text = "This is a test."

    def test_send_message(self, requests_mock):
        """Does the message send successfully?"""
        requests_mock.post(self.url, status_code=204)

        response = DiscordBot(self.url).send_message(self.test_text)

        assert response.status_code == 204
        assert requests_mock.last_request.json() == {"content": "```This is a test.```"}

    def test_bad_webhook_url(self, requests_mock):
        """Does the expected error raise when a bot id is incorrect?"""
        requests_mock.post(self.url, status_code=404)

        with pytest.raises(DiscordException, match="WEBHOOK_URL"):
            DiscordBot(self.url).send_message(self.test_text)
