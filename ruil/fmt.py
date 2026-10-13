"""Dutch date formatting helpers."""

DAYS = ["ma", "di", "wo", "do", "vr", "za", "zo"]
MONTHS = ["januari", "februari", "maart", "april", "mei", "juni", "juli",
          "augustus", "september", "oktober", "november", "december"]


def day_short(dt):
    return DAYS[dt.weekday()]


def month_name(dt):
    return MONTHS[dt.month - 1]


def short_date(dt):
    """'vr 9 okt'"""
    return f"{DAYS[dt.weekday()]} {dt.day} {MONTHS[dt.month - 1][:3]}"


def moment(dt, now):
    """'vandaag 10:12', 'gisteren 18:02' or 'vr 9 okt'."""
    days = (now.date() - dt.date()).days
    if days == 0:
        return f"vandaag {dt:%H:%M}"
    if days == 1:
        return f"gisteren {dt:%H:%M}"
    return short_date(dt)


def days_until(dt, now):
    return (dt.date() - now.date()).days


def week_range(monday, sunday):
    """'ma 19 – zo 25 oktober' (or across months: 'ma 26 oktober – zo 1 november')."""
    if monday.month == sunday.month:
        return f"ma {monday.day} – zo {sunday.day} {month_name(sunday)}"
    return f"ma {monday.day} {month_name(monday)} – zo {sunday.day} {month_name(sunday)}"
