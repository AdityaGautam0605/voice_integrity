"""Challenge phrase generation (L6).

Fires on amber.  Two honest caveats that belong in the code rather than only
in the report:

**The social cost is real.**  Asking a senior caller to repeat a nonce is
expensive, and under pressure a junior employee will skip it - which is
exactly the scenario where it was needed.  Any countermeasure requiring a
subordinate to be rude to a superior during a manufactured emergency has a
low compliance rate.  This is a supplement to the passive branches, not a
replacement for them.

**What it actually defeats.**  A pre-recorded clone cannot respond to a nonce
at all.  A live conversion pipeline can, but it must relay the phrase through
its own buffering delay, which the liveness branch is already watching.

Words are chosen to be short, phonetically distinct, and unambiguous over a
narrowband 300-3400 Hz channel - which rules out most minimal pairs.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

# Deliberately concrete, high-frequency words.  Avoided: rhyming sets,
# sibilant-initial pairs that smear through a low-bitrate codec, and any
# word whose vowel collapses at 8 kHz.
_WORDS = (
    "anchor",
    "basket",
    "candle",
    "dolphin",
    "ember",
    "falcon",
    "granite",
    "harbour",
    "ivory",
    "jasmine",
    "kettle",
    "lantern",
    "marble",
    "nutmeg",
    "orchid",
    "pepper",
    "quiver",
    "ribbon",
    "saffron",
    "temple",
    "umbrella",
    "velvet",
    "walnut",
    "yellow",
)

_NUMBERS = (
    "seven",
    "twelve",
    "nineteen",
    "twenty-four",
    "thirty-one",
    "forty-five",
    "fifty-eight",
    "sixty-three",
    "seventy-seven",
    "eighty-two",
)


@dataclass
class Challenge:
    phrase: str
    words: list[str]
    issued_ms: int

    def as_dict(self) -> dict:
        return {"phrase": self.phrase, "words": self.words, "issued_ms": self.issued_ms}


def generate_challenge(n_words: int = 3) -> Challenge:
    """Build an unpredictable phrase.

    `secrets` rather than `random`: a predictable challenge is no challenge,
    and an attacker who can anticipate the phrase can pre-synthesise it.
    """
    import time

    words: list[str] = []
    if n_words >= 1:
        words.append(secrets.choice(_NUMBERS))
    while len(words) < n_words:
        candidate = secrets.choice(_WORDS)
        if candidate not in words:
            words.append(candidate)

    return Challenge(
        phrase="-".join(words),
        words=words,
        issued_ms=int(time.time() * 1000),
    )


def prompt_text(challenge: Challenge) -> str:
    """What the agent is shown.

    Phrased as a routine verification step rather than an accusation, because
    an agent who feels they are accusing the caller will not read it out.
    """
    return (
        "Standard verification for this request. "
        f'Please ask the caller to repeat back: "{challenge.phrase}"'
    )
