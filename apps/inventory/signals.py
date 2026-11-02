"""Signals the inventory sends to the rest of LabStock.

``ask_answered``: someone answered an ask (``AskReply``); sent on commit with ``thread`` and
``reply``. The guard records a borrow ("lend" on an alert's borrow ask) as a verified outcome.
"""

from django.dispatch import Signal

ask_answered = Signal()
