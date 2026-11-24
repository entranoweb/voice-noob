"""The value a spoken answer carries, in the same normal form grading uses.

A careful transcript writes letters and digits as words ("A five six T B",
"zero seven eight double two"). These helpers turn that into the value a tool
should receive, so a clip's manifest row can be compared with a stored argument.
"""

from __future__ import annotations

import re

from synthiq_caller.grading import normalise_date, normalise_name, normalise_phone

_DIGIT_WORDS = {
    "zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
}  # fmt: skip
_TEENS = {
    "ten": "10", "eleven": "11", "twelve": "12", "thirteen": "13", "fourteen": "14",
    "fifteen": "15", "sixteen": "16", "seventeen": "17", "eighteen": "18", "nineteen": "19",
}  # fmt: skip
_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
    "eighty": 80, "ninety": 90,
}  # fmt: skip
_LETTER_WORDS = {"zed": "Z", "zee": "Z", "double-u": "W"}
_LEAD = re.compile(
    r"^(?:(?:uh|um|er|it'?s|it is|the postcode is|postcode is|my number is|is)\b[\s,]*)+", re.I
)


def spoken_characters(text: str, *, digits_only: bool = False) -> str:
    """Letters and digits said one at a time, as the string they spell.

    ``A five six T B`` gives ``A56TB``; ``B forty-five eight HT`` gives ``B458HT``;
    ``double two`` gives ``22``. With ``digits_only`` every letter is dropped,
    which is what a phone number wants ("O" is read as zero there).
    """
    tokens = re.findall(r"[A-Za-z]+(?:-[A-Za-z]+)?|\d+", _LEAD.sub("", text.strip()))
    out: list[str] = []
    repeat = 1
    index = 0
    while index < len(tokens):
        raw = tokens[index]
        low = raw.lower()
        nxt = tokens[index + 1].lower() if index + 1 < len(tokens) else ""
        piece: str | None
        if low in ("double", "triple"):
            repeat = 2 if low == "double" else 3
            index += 1
            continue
        if raw.isdigit():
            piece = raw
        elif low in _TENS or (low.split("-")[0] in _TENS and "-" in low):
            head, _, tail = low.partition("-")
            value = _TENS[head]
            if tail in _DIGIT_WORDS:
                value += int(_DIGIT_WORDS[tail])
            elif not tail and nxt in _DIGIT_WORDS and nxt not in ("oh", "o", "zero"):
                value += int(_DIGIT_WORDS[nxt])
                index += 1
            piece = str(value)
        elif low in _TEENS:
            piece = _TEENS[low]
        elif low in _DIGIT_WORDS:
            piece = _DIGIT_WORDS[low]
        elif low == "o":
            # The letter in a postcode, a zero in a phone number.
            piece = "0" if digits_only else "O"
        elif low in _LETTER_WORDS:
            piece = None if digits_only else _LETTER_WORDS[low]
        elif "-" in raw:
            # "E-one-two-X-Y": spell each part.
            piece = spoken_characters(raw.replace("-", " "), digits_only=digits_only)
        elif len(raw) <= 3 and raw.isalpha():
            piece = None if digits_only else raw.upper()
        else:
            piece = None  # an ordinary word ("postcode", "please")
        if piece:
            out.append(piece * repeat)
        repeat = 1
        index += 1
    return "".join(out)


def clip_value(category: str, text: str) -> str:
    """The normalised value of an answer, or "" when the category carries none."""
    if category == "dob":
        return normalise_date(text) or ""
    if category == "name":
        return normalise_name(text) or ""
    if category == "postcode":
        return spoken_characters(text)
    if category == "phone":
        return normalise_phone(spoken_characters(text, digits_only=True)) or ""
    return ""
