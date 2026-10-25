"""Free text from people (an incident note) is data, never instructions to the agents. No model calls.

The note reaches the planner, the monitor and the router as quoted data inside JSON. On top of that, a note that
tries to instruct the agents (override rules, change speeds, approve itself, play a role, inject a prompt) is kept
out of every prompt: the agents see a placeholder, and the person who reads the incident sees the original.
"""

from __future__ import annotations

import re

PATTERNS = [
    r"\bignore\b.{0,40}\b(instruction|rule|previous|above|critic|safety)",
    r"\bdisregard\b", r"\boverride\b", r"\bbypass\b", r"\bjailbreak\b",
    r"\byou are (now|an?|the)\b", r"\bact as\b", r"\bpretend\b", r"\brole ?play\b",
    r"\bsystem prompt\b", r"\bdeveloper message\b", r"<\s*/?\s*(system|assistant|script)\b", r"```",
    r"\b(speed|crossing_speed)(_mps)?\s*[:=]", r"\bapprove (this|it|the plan|yourself)\b", r"\bauto[- ]?approve\b",
    r"\bskip (the )?(critic|check|gate|approval|dry ?run)\b", r"\bdo not (escalate|ask|check)\b",
    r"\bnext_agent\b", r"\bassignee_ids\b", r"\bagv_routes\b",
]
RX = re.compile("|".join(PATTERNS), re.I)
PLACEHOLDER = "[note withheld from the agents: it looked like instructions; a person reads the original]"


def screen(note: str | None) -> tuple[str | None, str | None]:
    """-> (text the agents may see, the matched phrase if the note was withheld)."""
    if not note:
        return note, None
    m = RX.search(note)
    return (PLACEHOLDER, m.group(0)) if m else (note, None)
