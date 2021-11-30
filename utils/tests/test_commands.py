import pytest 
from utils.commands import Commands
from utils.bots import GroupMeBot

class TestCommands:
    '''Test Commands class'''

    url = "https://discordapp.com/api/webhooks/123/abc"
    test_bot = GroupMeBot(url)
    test_text = "This is a test."
    commands = Commands(test_bot, "123456")

    def test_mock(self):
        msg_list = "Look guys I'm just saying...".lower().split(" ")
        mock_msg_list = []
        # Offset index by number of punctuation characters so that punctuation doesn't interfere with capitalization of letters
        punctuation = {",", "'", '"', "-", ":", ";", "!", "@", "#", "$", "%", "&"}
        for word in msg_list:
            mock_word = ""
            punctuation_offset = 0
            for idx, letter in enumerate(word):
                if letter in punctuation:
                    punctuation_offset += 1

                idx -= punctuation_offset

                if idx % 2 != 0:
                    letter = letter.upper()
                mock_word += letter
            mock_msg_list.append(mock_word)

        print(" ".join(mock_msg_list))
        assert " ".join(mock_msg_list) == "lOoK gUyS i'M jUsT sAyInG..."

    def test_nice(self):
        response = self.commands.parse({'text': 'This is attempt number 69.'})
        assert response == "Nice."
        response = self.commands.parse({'text': 'The 69th day of the year is March something'})
        assert response == "Nice."
        response = self.commands.parse({'text': "This shouldn't trigger a text response"})
        assert response == ''