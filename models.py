from peewee import *
from playhouse.sqlite_ext import SqliteExtDatabase

db = SqliteExtDatabase('elo.db', pragmas=(('foreign_keys', True),))

class BaseModel(Model):
    class Meta:
        database = db

class Player():
    """ Player is an *in memory only* object that is built up by replaying the match history """

    def __init__(self):
        self.wins = 0
        self.losses = 0
        self.rating = 1500

    @property
    def k_factor(self):
        if self.rating > 2400:
            return 16
        elif self.rating < 2100:
            return 32
        return 24

    def __str__(self):
        return '<Player {} {} {}>'.format(self.wins, self.losses, self.rating)

class Match(BaseModel):
    winner_handle = CharField()
    winner_score  = IntegerField(default=0)
    loser_handle  = CharField()
    loser_score   = IntegerField(default=0)
    pending       = BooleanField(default=True)
    played        = DateTimeField(constraints=[SQL('DEFAULT CURRENT_TIMESTAMP')])

    def save(self, *args, **kwargs):
        if self.winner_handle != self.loser_handle:
            return super(Match, self).save(*args, **kwargs)
        raise IntegrityError('Winner cannot be the same as loser')

