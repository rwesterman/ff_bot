from utils.commands import Commands


class TestCommands:
    """Test Commands class"""

    commands = Commands(league=None)

    def test_mock(self):
        response = self.commands.mock_user("Look guys I'm just saying...")
        assert response == "lOoK gUyS i'M jUsT sAyInG..."
