import unittest


import requests_mock


from utils.bots import (SlackBot, SlackException, )


class SlackTestCase(unittest.TestCase):
    '''Test SlackBot class'''

    def setUp(self):
        self.url = "https://hooks.slack.com/services/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"
        self.test_bot = SlackBot(self.url)
        self.test_text = "This is a test of the automated bot system. This is only a test."

    @requests_mock.Mocker()
    def test_send_message(self, m):
        '''Does the message send successfully?'''
        m.post(self.url, status_code=200)
        self.assertEqual(self.test_bot.send_message(self.test_text).status_code, 200)

    @requests_mock.Mocker()
    def test_bad_bot_id(self, m):
        '''Does the expected error raise when a bot id is incorrect?'''
        m.post(self.url, status_code=404)
        with self.assertRaises(SlackException):
            self.test_bot.send_message(self.test_text)
