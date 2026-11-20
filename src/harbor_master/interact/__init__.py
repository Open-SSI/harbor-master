"""What a run can be told, by the boxes of its screen: the commands behind them, and the answers they show."""
from . import handlers  # noqa: F401  (registers the commands)
from .handlers import decide, waiting_reports
from .answers import context_for, explain, how_to_answer, ready_to_type, review_briefing
from .registry import COMMANDS, Command, execute, parse

__all__ = ["COMMANDS", "Command", "context_for", "execute", "explain", "decide", "how_to_answer", "waiting_reports", "ready_to_type",
           "parse", "review_briefing"]
