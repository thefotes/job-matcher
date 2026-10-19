"""Plain-text, date and remote-signal helpers shared by provider adapters."""

import html
import re
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

_BLOCKS = {
    "p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6",
    "tr", "table", "section", "article", "blockquote", "pre", "hr",
}  # fmt: skip
_CONTROL = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f  ]")
_REMOTE = re.compile(r"\b(remote|anywhere|work from home|wfh|distributed)\b", re.I)


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.skip += 1
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag in _BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.skip = max(0, self.skip - 1)
        elif tag in _BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def html_to_text(value):
    """Readable plain text from ATS HTML, including entity-escaped HTML (Greenhouse)."""
    if not value:
        return ""
    value = str(value)
    if "&lt;" in value and "<" not in value:
        value = html.unescape(value)
    parser = _Text()
    parser.feed(value)
    parser.close()
    text = _CONTROL.sub(" ", "".join(parser.parts).replace("\xa0", " "))
    out, bullet = [], False
    for line in (" ".join(raw.split()) for raw in text.split("\n")):
        if line == "-":
            bullet = True
        elif not line:
            if out and out[-1]:
                out.append("")
        else:
            line = "- " + line if bullet else line
            if line.startswith("- ") and len(out) > 1 and not out[-1] and out[-2][:2] == "- ":
                out.pop()  # keep list items together
            out.append(line)
            bullet = False
    return "\n".join(out).strip()


def join_text(*parts):
    return "\n\n".join(part for part in (p.strip() for p in parts if p) if part)


def parse_date(value):
    """Datetime (UTC) from ISO strings, 'YYYY-MM-DD HH:MM:SS UTC', or epoch milliseconds."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip().replace(" UTC", "+00:00")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def iso(value):
    parsed = parse_date(value)
    return parsed.isoformat() if parsed else None


def relative_days(text):
    """Days since posting from Workday-style 'Posted Today' / 'Posted 3 Days Ago' / '30+'."""
    lowered = (text or "").lower()
    if "today" in lowered:
        return 0
    if "yesterday" in lowered:
        return 1
    match = re.search(r"(\d+)\+?\s*days?\s*ago", lowered)
    return int(match[1]) if match else None


def days_ago(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


def infer_remote(workplace=None, flag=None, location=""):
    """True/False when the ATS or location text says so; None when unknown.

    An explicit workplace type wins, then a boolean flag, then the location text.
    """
    kind = re.sub(r"[^a-z]", "", str(workplace or "").lower())
    if kind == "remote":
        return True
    if kind in {"onsite", "hybrid", "inoffice", "office"}:
        return False
    if isinstance(flag, bool) and flag:
        return True
    if location and _REMOTE.search(location):
        return True
    if isinstance(flag, bool):
        return False
    return None


def money(minimum, maximum, currency="", interval=""):
    """'USD 200,000–250,000 per year' from structured salary fields; '' when absent."""
    values = [v for v in (minimum, maximum) if isinstance(v, (int, float)) and v > 0]
    if not values:
        return ""
    amount = "–".join(f"{v:,.0f}" for v in dict.fromkeys(values))
    interval = str(interval or "").replace("-", " ").replace("_", " ").lower()
    return " ".join(part for part in (str(currency or ""), amount, interval) if part)


_AMOUNT = r"(\d{2,3}(?:,\d{3})+(?:\.\d+)?|\d{2,3}(?:\.\d+)?\s?[kK])\b"
_SALARY_RANGE = re.compile(
    rf"(?:\$|USD\s*\$?)\s?{_AMOUNT}\s*(?:-|–|—|to)\s*(?:\$|USD\s*\$?)?\s?{_AMOUNT}"
)


def _dollars(amount):
    amount = amount.replace(",", "").replace(" ", "")
    return float(amount[:-1]) * 1000 if amount[-1] in "kK" else float(amount)


def salary_ranges(text):
    """Dollar ranges ('$129,000 - $210,000', '$200K–$250K', 'USD 200,000–250,000') in the
    order they appear, as (low, high). Only amounts of $10,000 or more count, which skips
    hourly rates; pay zones by location come back as separate ranges."""
    ranges = []
    for low, high in _SALARY_RANGE.findall(text or ""):
        pair = (_dollars(low), _dollars(high))
        if pair[0] <= pair[1] and pair not in ranges:
            ranges.append(pair)
    return ranges
