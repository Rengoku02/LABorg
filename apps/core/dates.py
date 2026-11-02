"""Small date helpers shared by the apps."""

import calendar


def months_before(moment, months):
    """The same day ``months`` calendar months earlier (the month's last day if shorter)."""
    year, month = divmod(moment.month - 1 - months, 12)
    year, month = moment.year + year, month + 1
    day = min(moment.day, calendar.monthrange(year, month)[1])
    return moment.replace(year=year, month=month, day=day)
