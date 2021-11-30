import pytest 
from utils.commands import Commands
from utils.bots import GroupMeBot

class TestCommands:
    '''Test Commands class'''

    url = "https://discordapp.com/api/webhooks/123/abc"
    test_bot = GroupMeBot(url)
    test_text = "This is a test."
    commands = Commands(test_bot, "123456")

    # # Test that the tie breaking logic works correctly
    # def test_break_ties(self):
    #     # (wins, losses, team_name, points_for)
    #     league_stats = [
    #         (10, 1, "Team1", 1000),
    #         (10, 2, "Team2", 1000),
    #         (10, 1, "Team3", 999),
    #         (8, 3, "Team4", 1200)
    #     ]
    #     # Results should be:
    #     # Team1, Team3, Team2, Team4
    #     ref = [
    #         (10, 1, "Team1", 1000),
    #         (10, 1, "Team3", 999),
    #         (10, 2, "Team2", 1000),
    #         (8, 3, "Team4", 1200)    
    #     ]

    #     # break_ties assumes that input list is sorted by wins
    #     hyp = self.commands._break_ties(sorted(league_stats, key=lambda x: x[0], reverse=True))
    #     assert hyp == ref

    #     assert ref == sorted(league_stats, key=lambda x: (x[0], -x[1], x[3]), reverse=True)
    #     assert ref == sorted(league_stats, key=lambda x: (-x[0], x[1], -x[3]))

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
