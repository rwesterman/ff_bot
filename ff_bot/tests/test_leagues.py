import unittest

from ff_espn_api import League, Team
# from ff_bot.ff_bot import (GroupMeBot, GroupMeException, )
from ff_bot.bots import get_power_rankings, get_last_place_team, get_close_scores, get_matchups, get_scoreboard, get_scoreboard_short, get_trophies


class EspnTestCase(unittest.TestCase):
    '''Test ESPN League methods class'''

    def setUp(self):
        league_id = 950634
        year = 2018
        # Secret stuff
        espn_s2 = "AECcqBAxkb6iztLdTzvhM6dSAdobKKCPuSY8DF3qSTGmjjVUtPZT8NSSv7KywiL569X2Ml8wZb0rxUNrUY%2F1ky%2FSzYlFigLbX%2FQZhA8D7nkkB752d9kMJmWO6B43%2FZFspi1tyvRPUPSciqK1A0hsYMI9HYyUa37MLrQFTbXrEcSwpb1%2BH0uwWdmm2%2BS2GZM04fjCWtC4GjuIgdBx%2FxE8VYOz6STEAPyGSn9RxDonMuDrCGHEljM1a1I2vi4m3eesI9Rmx%2FqH0kq0Sv7ybGL0YxHD"
        swid = "{BFD1DF0E-0120-4EF1-A54E-128EAE53AA82}"
        # Set up bot with debug mode
        self.league = League(league_id, year, espn_s2, swid)
        # self.test_bot = GroupMeBot("d6b7111ac8a3b7da98aed334ed")
        # self.test_text = "This is a test."

    def test_get_last_place_team(self):
        '''Does the last place team's owner return correctly?'''
        self.assertEqual(get_last_place_team(self.league).owner, "Carter Axelsen")

    def test_get_trophies(self):
        print(get_trophies(self.league))
