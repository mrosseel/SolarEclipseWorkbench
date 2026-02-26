"""Formatting helpers for the Solar Eclipse Workbench UI."""

import datetime

TIME_FORMATS = {
    "24 hours": "%H:%M:%S",
    "12 hours": "%I:%M:%S"}

DATE_FORMATS = {
    "dd Month yyyy": "%d %b %Y",
    "dd/mm/yyyy": "%d/%m/%Y",
    "mm/dd/yy": "%m/%d/%Y"
}

BEFORE_AFTER = {
    "before": 1,
    "after": -1
}

REFERENCE_MOMENTS = ["C1", "C2", "MAX", "C3", "C4", "sunset", "sunrise"]


def format_countdown(countdown: datetime.timedelta):
    """ Format the given countdown.

    Args:
        - countdown: Countdown as datetime

    Returns: Formatted countdown, with the days (if any), hours (if any), minutes, and seconds.
    """

    formatted_countdown = ""
    days = countdown.days

    if days > 0:
        formatted_countdown += f" {days}d "

    hours = countdown.seconds // 3600
    if days > 0 or hours > 0:
        formatted_countdown += f"{hours:02d}:"

    minutes, seconds = (countdown.seconds // 60) % 60, countdown.seconds % 60
    formatted_countdown += f"{minutes:02d}:{seconds:02d}"

    return formatted_countdown


def format_time(time: datetime.datetime, time_format: str) -> str:
    """ Format the given time according to the given time format.

    Args:
        - time: Time as datetime

    Returns: Formatted time, according to the given time format.
    """

    suffix = ""
    if time_format == "12 hours":
        suffix = " am" if time.hour < 12 else " pm"

    return f"{datetime.datetime.strftime(time, TIME_FORMATS[time_format])}{suffix}"
