"""Expected application errors, independent of Telegram and storage."""


class GameError(Exception):
    """Base class for expected game-rule failures."""


class GameNotFound(GameError):
    pass


class InvalidGameSetup(GameError):
    pass


class InviteUnavailable(GameError):
    pass


class CannotJoinOwnGame(GameError):
    pass


class NotAPlayer(GameError):
    pass


class NotYourTurn(GameError):
    pass


class DailyChallengeClosed(GameError):
    """A daily attempt cannot be played outside its one-hour window."""


class DailyChallengeRequiresActivation(GameError):
    """The account must be activated before joining a daily challenge."""


class InvalidFist(GameError):
    pass


class InvalidCell(GameError):
    pass


class InvalidWord(GameError):
    pass


class InvalidWordLength(GameError):
    pass


class StaleAction(GameError):
    """The callback was already handled or belongs to an older game state."""


class InvalidFinalChoice(GameError):
    pass


class InvalidFinalMessage(GameError):
    pass


class RepeatedQuestion(GameError):
    pass


class PendingFinalResponse(GameError):
    pass
