import time
import json
import re
from slackclient import SlackClient
from tabulate import tabulate
from peewee import *
from datetime import datetime
from dateutil import tz
from itertools import takewhile
from collections import defaultdict

from models import db, Player, Match

# We allow for an optional backdoor that allows any user to run any command
BACKDOOR_ENABLED = True
BACKDOOR_PREFIX = "__"
BACKDOOR_REGEX = "(?:{})?".format(BACKDOOR_PREFIX) if BACKDOOR_ENABLED else ''

BEAT_TERMS        = 'crushed|rekt|beat|whooped|destroyed|smashed|demolished|decapitated|smothered|creamed'

WINNER_REGEX      = re.compile(BACKDOOR_REGEX + 'I (?:{}) <@([A-z0-9]*)> (\d+) ?- ?(\d+)'.format(BEAT_TERMS), re.IGNORECASE)
CONFIRM_REGEX     = re.compile(BACKDOOR_REGEX + 'Confirm (\d+)', re.IGNORECASE)
CONFIRM_ALL_REGEX = re.compile(BACKDOOR_REGEX + 'Confirm all', re.IGNORECASE)
DELETE_REGEX      = re.compile(BACKDOOR_REGEX + 'Delete (\d+)', re.IGNORECASE)
LEADERBOARD_REGEX = re.compile(BACKDOOR_REGEX + 'Print leaderboard', re.IGNORECASE)
UNCONFIRMED_REGEX = re.compile(BACKDOOR_REGEX + 'Print unconfirmed', re.IGNORECASE)

from_zone = tz.gettz('UTC')
to_zone = tz.gettz('America/Los_Angeles')

class SlackClient(SlackClient):
    def is_bot(self, user_id):
        return self.api_call('users.info', user=user_id)['user']['is_bot']

    def get_name(self, user_id):
        return self.api_call('users.info', user=user_id)['user']['profile']['display_name_normalized']

    def get_channel_id(self, channel_name):
        channels = self.api_call("channels.list")

        for channel in channels['channels']:
            if channel['name'] == channel_name:
                return channel['id']

        print('Unable to find channel: ' + channel_name)
        quit()

class EloBot(object):
    players = defaultdict(Player)

    def __init__(self, slack_client, channel_id, name, min_streak_len):
        self.name = name
        self.slack_client = slack_client
        self.min_streak_len = min_streak_len
        self.channel_id = channel_id

        self.last_ping = 0

        self.init_players()
        self.ensure_connected()
        self.run()

    def rank_game(self, winner, loser):
        # From https://metinmediamath.wordpress.com/2013/11/27/how-to-calculate-the-elo-rating-including-example/
        winner_transformed_rating = 10 ** (winner.rating / 400.0)
        loser_transformed_rating  = 10 ** (loser.rating  / 400.0)

        winner_expected_score = winner_transformed_rating / (winner_transformed_rating + loser_transformed_rating)
        loser_expected_score  = loser_transformed_rating  / (winner_transformed_rating + loser_transformed_rating)

        winner_new_elo = round(winner.rating + winner.k_factor() * (1 - winner_expected_score))
        loser_new_elo  = round(loser.rating  + loser.k_factor()  * (0 - loser_expected_score))

        winner_elo_delta = abs(winner_new_elo - winner.rating)
        loser_elo_delta  = abs(loser_new_elo  - loser.rating)

        winner.wins += 1
        loser.losses += 1

        winner.rating = winner_new_elo
        loser.rating = loser_new_elo

        return winner_elo_delta, loser_elo_delta

    def init_players(self):
        """Initializes self.players with the games stored in the database"""
        print('Initializing in-memory player objects')
        matches = list(Match.select().order_by(Match.id))
        for match in matches:
            print("Match: {}".format(match.__dict__))
            if not match.pending:
                winner = self.players[match.winner_handle]
                loser  = self.players[match.loser_handle]
                self.rank_game(winner, loser)
        print('Finished initializing players')
        print('Players: {}'.format([str(player) for player in self.players.values()]))

    def ensure_connected(self):
        sleeptime = 0.1
        while not self.slack_client.server.connected:
            print('Was disconnected, attemping to reconnect...')
            try:
                self.slack_client.rtm_connect()
            except:  # TODO: Except what
                pass
            time.sleep(sleeptime)
            sleeptime = min(30, sleeptime * 2) # Exponential back off with a max wait of 30s

    def heartbeat(self):
        """Send a heartbeat if necessary"""
        now = int(time.time())
        if now > self.last_ping + 3:
            self.slack_client.server.ping()
            self.last_ping = now

    def talk(self, message):
        """Send a message to the Slack channel"""
        self.slack_client.api_call('chat.postMessage', channel=self.channel_id, text=message, username=self.name)

    def run(self):
        print('Running!')
        #self.talk(self.name + ' online!')

        while True:
            time.sleep(0.1)
            self.heartbeat()
            self.ensure_connected()

            messages = self.slack_client.rtm_read()
            for message in messages:
                if 'user' in message and message.get('type', False) == 'message' and message.get('channel', False) == self.channel_id and message.get('text', False):
                    print('Message received:\n{}'.format(message))

                    text = message['text']
                    user_handle = message['user']
                    has_backdoor = BACKDOOR_ENABLED and text.startswith(BACKDOOR_PREFIX)
                    if WINNER_REGEX.match(text):
                        loser_handle, winner_score, loser_score = re.search(WINNER_REGEX, text).groups()
                        self.winner(user_handle, loser_handle, int(winner_score), int(loser_score))
                    elif CONFIRM_REGEX.match(text):
                        match_id, = re.search(CONFIRM_REGEX, text).groups()
                        self.confirm(user_handle, match_id, has_backdoor)
                    elif CONFIRM_ALL_REGEX.match(text):
                        self.confirm_all(user_handle)
                    elif DELETE_REGEX.match(text):
                        match_id, = re.search(DELETE_REGEX, text).groups()
                        self.delete(user_handle, match_id, has_backdoor)
                    elif LEADERBOARD_REGEX.match(text):
                        self.print_leaderboard()
                    elif UNCONFIRMED_REGEX.match(text):
                        self.print_unconfirmed()

    def winner(self, winner_handle, loser_handle, winner_score, loser_score):
        match = Match.create(winner_handle=winner_handle, winner_score=winner_score, loser_handle=loser_handle, loser_score=loser_score)
        self.talk(f'<@{loser_handle}>, type "Confirm {match.id}" to confirm the above match, or ignore it if it\'s incorrect')

    def confirm_all(self, user_handle):
        matches = list(Match.select(Match).where(Match.loser_handle == user_handle, Match.pending == True))
        for match in matches:
            self.confirm(user_handle, match.id)
        self.talk("Confirmed {} matches!".format(len(matches)))

    def confirm(self, user_handle, match_id, has_backdoor=False):
        try:
            match = Match.select().where(Match.id == match_id, Match.pending == True).get()
        except Exception:  # TODO: Figure out how to import MatchDoesNotExist
            self.talk('No pending match #{}'.format(match_id))
            return

        if not has_backdoor and match.loser_handle != user_handle:
            self.talk('<@{}>, you are not allowed to confirm match #{}!'.format(user_handle, match_id))
            return

        with db.transaction():
            winner = self.players[match.winner_handle]
            loser  = self.players[match.loser_handle]
            winner_elo_delta, loser_elo_delta = self.rank_game(winner, loser)

            match.pending = False
            match.save()

            self.talk('<@{}>, your new ELO is: {} (+{})'.format(match.winner_handle, winner.rating, winner_elo_delta))
            self.talk('<@{}>, your new ELO is: {} (-{})'.format(match.loser_handle, loser.rating, loser_elo_delta))

    def delete(self, user_handle, match_id, has_backdoor=False):
        try:
            match = Match.select(Match).where(Match.id == match_id, Match.pending == True).get()
        except Exception:  # TODO: Figure out how to import MatchDoesNotExist
            self.talk('No match #{}'.format(match_id))

        if not has_backdoor and match.winner_handle != user_handle:
            self.talk('<@{}>, you are not allowed to delete match #{}!'.format(user_handle, match_id))

        match.delete_instance()
        self.talk('Deleted match #{}'.format(match_id))

    def print_leaderboard(self):
        table = []

        for handle_player_tuple in sorted(self.players.items(), key=lambda p: p[1].rating, reverse=True):
            slack_handle, player = handle_player_tuple
            win_streak = self.get_win_streak(slack_handle)
            streak_text = '(won {} in a row)'.format(win_streak) if win_streak >= self.min_streak_len else ''
            table.append([self.slack_client.get_name(slack_handle), player.rating, player.wins, player.losses, streak_text])

        self.talk('```' + tabulate(table, headers=['Name', 'ELO', 'Wins', 'Losses', 'Streak']) + '```')

    def print_unconfirmed(self):
        table = []

        for match in Match.select().where(Match.pending == True).order_by(Match.played.desc()).limit(25):
            match_played_utc = match.played.replace(tzinfo=from_zone)
            match_played_pst = match_played_utc.astimezone(to_zone)
            table.append([
                match.id,
                self.slack_client.get_name(match.loser_handle),
                self.slack_client.get_name(match.winner_handle),
                '{} - {}'.format(match.winner_score, match.loser_score),
                match_played_pst.strftime('%m/%d/%y %I:%M %p')
            ])

        self.talk('```' + tabulate(table, headers=['Match', 'Needs to Confirm', 'Opponent', 'Score', 'Date']) + '```')

    def get_win_streak(self, player_slack_id):
        win_streak = 0
        matches = Match.select().where(Match.pending == False, (player_slack_id == Match.winner_handle) | (player_slack_id == Match.loser_handle)).order_by(Match.played.desc())
        return len(list(takewhile(lambda m: m.winner_handle == player_slack_id, matches)))

if __name__ == '__main__':
    with open('config.json') as config_data:
        config = json.load(config_data)

    slack_client = SlackClient(config['slack_token'])
    db.connect()
    Match.create_table()
    EloBot(slack_client, slack_client.get_channel_id(config['channel']), config['bot_name'], config['min_streak_length'])

