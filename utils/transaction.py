from typing import Optional, Union

class Transaction:
    """This class is used to format transaction info from ESPN's recent activity into a human-parseable format."""

    """
    Cases:
        * Single add or drop:
            * "TeamName added PlayerName"
            * "TeamName dropped PlayerName"
        * Single trade:
            * "TeamName sends PlayerName, PlayerName, PlayerName to TeamName2 for PlayerName, PlayerName, PlayerName" (or some emoji simplification)
        * Multiple adds or drops:
            * "TeamName :arrow_up: AddedPlayer, :arrow_down: DroppedPlayer, $BidAmount"
    """
    def __init__(self, actions):
        self.actions = actions

    @property
    def teams_involved(self):
        """Return set of all teams involved in the transaction."""
        return {action[0].team_name for action in self.actions}
    
    @property
    def bid_amount(self) -> Optional[int]:
        """Return the bid amount for the transaction."""
        amount=None
        for action in self.actions:
            if action[1] == "WAIVER ADDED":
                amount = action[3]

        return amount

    @property
    def transaction_type(self) -> dict[str, int]:
        """Return the type of transaction."""
        action_types = {"add": -1, "drop": -1, "trade": -1}
        for idx, action in enumerate(self.actions):
            if "ADDED" in action[1]:
                action_types["add"] = idx
            elif "DROPPED" in action[1]:
                action_types["drop"] = idx
            # NOTE: Currently not supporting trade actions
            elif "TRADE" in action[1]:
                action_types["trade"] = idx

        return action_types

    def build_message_emoji(self) -> str:
        """Build a message to send to the Discord channel."""
        output_str = ""
        teams_involved = self.teams_involved
        action_types = self.transaction_type
        if len(teams_involved) == 1:
            output_str += f"{teams_involved.pop()}: "
            if action_types["add"] > -1:
                output_str += f":arrow_up: {self.actions[action_types['add']][2].name}"
                if action_types["drop"] > -1:
                    output_str += "  "
            if action_types["drop"] > -1:
                output_str += f":arrow_down: {self.actions[action_types['drop']][2].name}"
            if self.bid_amount:
                output_str += f" for :moneybag: {self.bid_amount}"
        else:
            output_str = f"There was a trade here but I haven't implemented that logic yet. Raw output is: {self.actions}"
            return output_str
        return output_str
    
    def build_message_csv(self) -> str:
        """Build a message to send as a CSV to the Discord channel."""
        output_str = ""
        teams_involved = self.teams_involved
        action_types = self.transaction_type
        if len(teams_involved) == 1:
            output_str += f"{teams_involved.pop()},"
            if action_types["add"] > -1:
                output_str += f"{self.actions[action_types['add']][2].name}"
            output_str += ","
            if action_types["drop"] > -1:
                output_str += f"{self.actions[action_types['drop']][2].name}"
            output_str += ","
            if self.bid_amount:
                output_str += f"{self.bid_amount}"
        else:
            output_str = f"There was a trade here but I haven't implemented that logic yet. Raw output is: {self.actions}"
            return output_str
        return output_str

    def build_message_tabulate(self) -> list[Union[str, int]]:
        output_list = []
        teams_involved = self.teams_involved
        action_types = self.transaction_type
        if len(teams_involved) == 1:
            output_list.append(teams_involved.pop())
            if action_types["add"] > -1:
                output_list.append(self.actions[action_types['add']][2].name)
            else:
                output_list.append("")
            if action_types["drop"] > -1:
                output_list.append(self.actions[action_types['drop']][2].name)
            else:
                output_list.append("")
            if self.bid_amount:
                output_list.append(self.bid_amount)
            else:
                output_list.append(0)
        # Todo: Figure out how to represent trades
        return output_list
        