"""From a word-level transcript to caller clips: where to cut, what to call each cut.

Pure functions over word lists, so the rules can be tested without audio.

A word is ``{"text": str, "start": float, "end": float, "speaker_id": str}``.
The agent is whichever speaker says the first word (an agent greets first on an
inbound line); pass ``agent_speaker`` when that does not hold.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# Words of one speaker closer than this belong to one stretch of speech.
WORD_GAP_S = 0.9
# Two caller stretches this close, with no agent word between, are one answer
# said hesitantly ("B nine ... zero RS", "It's Elliot ... Allen.").
MERGE_GAP_S = 2.5
PAD_S = 0.15
# Shorter than a spoken "no": more likely a stray noise than an answer.
TOO_SHORT_S = 0.12

_YES_NO = re.compile(
    r"^(?:uh,?\s*|um,?\s*)?(?:yes|yeah|yep|yup|correct|that'?s (?:right|correct|fine)|right|"
    r"no|nope|it'?s wrong|wrong|mm-?hmm|okay|ok|sure|fine)(?:[,.!\s]+(?:please|thank you|thanks|"
    r"that'?s (?:right|correct|fine)|it is|i do|i have|i haven'?t))*[.!\s]*$",
    re.IGNORECASE,
)
_ASKS_TO_CONFIRM = re.compile(
    r"(?:is that (?:all )?right|all right|is that correct|shall i go ahead|"
    r"(?:the )?one you'?d like to (?:move|cancel)|would you like the appointment at[^?]*)\s*\?\s*$",
    re.I,
)
_GOODBYE = re.compile(r"^(?:okay[,.]?\s*)?(?:bye|goodbye)(?![a-z])", re.I)

# First match wins. Order matters: specific questions before general ones.
_QUESTION_RULES: tuple[tuple[str, str], ...] = (
    ("opener", r"how can i help"),
    ("name", r"full name|your name|registered under"),
    ("been_before", r"been to see us before|visited us before|been to the clinic|been here before"),
    ("insurer", r"which insurer"),
    ("insurance", r"health insurance|paying yourself|paying privately"),
    ("body_part", r"which part of the body"),
    ("referral_sent", r"already been sent|how to submit|online form"),
    ("referral", r"referral"),
    ("mobility", r"mobility|getting around"),
    ("phone", r"mobile number|phone number"),
    ("postcode", r"post ?code"),
    ("dob", r"date of birth"),
    ("reason", r"bringing you in|knee or shoulder"),
    ("slot", r"which suits you|which one would suit|which of those"),
    ("when", r"when would suit|particular day|earliest available|soonest we have"),
    ("anything_else", r"anything else"),
)
_CONFIRM_RULES: tuple[tuple[str, str], ...] = (
    ("confirm_fee", r"fee|pounds|£"),
    ("confirm_details", r"i have the name|that'?s the name|date of birth"),
    ("confirm_practitioner", r"that would be with"),
    ("confirm_choice", r"one you'?d like to (?:move|cancel)"),
    (
        "confirm_booking",
        r"read that back|that'?s (?:a|an) |moving you|cancel|go ahead|appointment at",
    ),
)


@dataclass
class Segment:
    speaker: str
    start: float
    end: float
    text: str


@dataclass
class ClipPlan:
    """One caller answer to cut: its span, its label and anything a reviewer should know."""

    start: float
    end: float
    text: str
    question: str
    category: str
    review: list[str] = field(default_factory=list)

    @property
    def seconds(self) -> float:
        return self.end - self.start


def segments(words: list[dict[str, Any]], gap_s: float = WORD_GAP_S) -> list[Segment]:
    out: list[Segment] = []
    for word in words:
        speaker = str(word.get("speaker_id", ""))
        start, end = float(word["start"]), float(word["end"])
        text = str(word.get("text", ""))
        if out and out[-1].speaker == speaker and start - out[-1].end < gap_s:
            out[-1].end = end
            out[-1].text = f"{out[-1].text} {text}".strip()
        else:
            out.append(Segment(speaker, start, end, text))
    return out


def label(question: str, reply: str) -> str:
    """What kind of answer ``reply`` is, given the agent line it follows."""
    asked = question.strip()
    if _GOODBYE.match(reply.strip()):
        return "goodbye"
    if _ASKS_TO_CONFIRM.search(asked) and _YES_NO.match(reply.strip()):
        for name, pattern in _CONFIRM_RULES:
            if re.search(pattern, asked, re.I):
                return name
        return "confirm"
    if _ASKS_TO_CONFIRM.search(asked) and re.search(
        r"i have the name|that'?s the name", asked, re.I
    ):
        # A read-back answered with something other than yes or no is a correction.
        return "correction"
    for name, pattern in _QUESTION_RULES:
        if re.search(pattern, asked, re.I):
            return name
    return "other"


def plan_clips(
    words: list[dict[str, Any]],
    *,
    agent_speaker: str | None = None,
    pad_s: float = PAD_S,
    merge_gap_s: float = MERGE_GAP_S,
) -> list[ClipPlan]:
    """Every caller answer in the call, merged where one answer was said in two breaths."""
    if not words:
        return []
    agent = agent_speaker if agent_speaker is not None else str(words[0].get("speaker_id", ""))
    agent_words = [
        (float(w["start"]), float(w["end"])) for w in words if str(w.get("speaker_id", "")) == agent
    ]
    segs = segments(words)

    plans: list[ClipPlan] = []
    question = ""
    for seg in segs:
        if seg.speaker == agent:
            question = seg.text
            continue
        previous = plans[-1] if plans else None
        agent_between = previous is not None and any(
            previous.end <= a_start < seg.start for a_start, _ in agent_words
        )
        if (
            previous is not None
            and not agent_between
            and previous.question == question
            and seg.start - previous.end <= merge_gap_s
        ):
            previous.end = seg.end
            previous.text = f"{previous.text} {seg.text}".strip()
            if "split_merged" not in previous.review:
                previous.review.append("split_merged")
            continue
        plans.append(ClipPlan(seg.start, seg.end, seg.text, question, ""))

    for plan in plans:
        plan.category = label(plan.question, plan.text)
        cut_start, cut_end = max(0.0, plan.start - pad_s), plan.end + pad_s
        if any(a_start < cut_end and a_end > cut_start for a_start, a_end in agent_words):
            plan.review.append("overlaps_agent")
        if plan.seconds < TOO_SHORT_S:
            plan.review.append("too_short")
    return plans
