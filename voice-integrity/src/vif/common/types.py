"""Shared data types.

These are the contracts between layers.  Everything that crosses a module
boundary is one of these, so a change here is visible as a type error rather
than as a silently wrong number.
"""

from __future__ import annotations

import time
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class Side(str, Enum):
    """Which end of the call a stream belongs to.

    CALLER is the party under assessment.  AGENT is our own side: a known
    human, and the in-call reference the liveness branch compares against.
    """

    CALLER = "caller"
    AGENT = "agent"


class Band(str, Enum):
    GREEN = "green"
    AMBER = "amber"
    RED = "red"


class Action(str, Enum):
    PROCEED = "proceed"
    CHALLENGE = "challenge"
    GATE_ACTION = "gate_action"


class TurnEventKind(str, Enum):
    START = "start"
    END = "end"


class TurnEvent(BaseModel):
    """A speech boundary.  The entire feature set of the liveness branch.

    Deliberately carries no audio, no features and no transcript - which is
    why this branch can run where the acoustic branch legally cannot.
    """

    side: Side
    kind: TurnEventKind
    monotonic_ns: int

    @property
    def seconds(self) -> float:
        return self.monotonic_ns / 1e9


class BranchScores(BaseModel):
    """Raw, uncalibrated per-branch output for one window.

    None means the branch did not run (no enrolment, too few turns, disabled),
    which is distinct from a score of zero.
    """

    spoof: float | None = None
    speaker: float | None = None
    prosody: float | None = None
    liveness: float | None = None


class LivenessFeatures(BaseModel):
    """Shape statistics of the response-gap distribution.

    Every feature is a within-call comparison against the agent side, which is
    what cancels language, network, task and speaker-pair effects.
    """

    n_transitions: int = 0
    caller_floor_ms: float | None = None
    agent_floor_ms: float | None = None
    floor_delta_ms: float | None = None
    caller_overlap_rate: float | None = None
    agent_overlap_rate: float | None = None
    overlap_delta: float | None = None
    caller_gap_cv: float | None = None
    agent_gap_cv: float | None = None
    variance_ratio: float | None = None
    fast_response_count: int = 0
    rtt_ms: float | None = None


class ScoreFrame(BaseModel):
    """Streaming, advisory only.  No action may be taken on a score frame."""

    call_id: str
    seq: int
    risk: float
    ci: float
    band: Band
    branches: BranchScores
    speech_s: float
    model_version: str
    kind: Literal["score"] = "score"


class VerdictPayload(BaseModel):
    """The signed decision.  The only output an action may be taken on."""

    call_id: str
    risk: float
    band: Band
    branches: BranchScores
    liveness: LivenessFeatures
    speech_s: float
    n_windows: int
    model_version: str
    policy_version: str
    action: Action
    ts_ms: int = Field(default_factory=lambda: int(time.time() * 1000))
    nonce: str = ""


class Verdict(BaseModel):
    payload: VerdictPayload
    alg: str = "Ed25519"
    key_id: str = ""
    signature: str = ""
