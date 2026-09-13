"""Policy evaluation (L5).

Deliberately thin.  The scoring layer already produced a risk band from two
printable thresholds; this maps that band to an action and attaches a reason a
human can read.

Two rules are enforced in code rather than left to documentation.

**The score gates the action, never the call.**  A trustworthy score needs
10-15 seconds of speech that a live conversation does not politely provide, so
the decision belongs where the loss occurs: the approval, the disclosure, the
reset.  A configuration that tries to terminate calls is rejected at load.

**Fail closed.**  A missing or unverifiable verdict is elevated risk, not
absence of risk - otherwise the cheapest attack on the whole system is a cut
network cable rather than a voice clone.
"""

from __future__ import annotations

from dataclasses import dataclass

from vif.common.config import PolicyConfig
from vif.common.logging import get_logger
from vif.common.types import Action, Risk, SpeakerStatus

log = get_logger(__name__)


@dataclass
class Decision:
    risk: Risk
    action: Action
    reason: str
    identity_warning: bool = False

    def as_dict(self) -> dict:
        return {
            "risk": self.risk.value,
            "action": self.action.value,
            "reason": self.reason,
            "identity_warning": self.identity_warning,
        }


class PolicyEngine:
    def __init__(self, config: PolicyConfig | None = None):
        self.config = config or PolicyConfig()

    def evaluate(
        self,
        risk: Risk,
        spoof_probability: float,
        speaker_status: SpeakerStatus = SpeakerStatus.NOT_ENROLLED,
        verdict_verified: bool = True,
    ) -> Decision:
        """Map a band to an action, with a reason worth showing an operator."""
        if self.config.fail_closed and not verdict_verified:
            return Decision(
                risk=Risk.RED,
                action=Action(self.config.actions.red),
                reason="verdict missing or signature invalid - failing closed",
                identity_warning=False,
            )

        action = Action(getattr(self.config.actions, risk.value.lower()))

        reason = {
            Risk.RED: f"spoof probability {spoof_probability:.2f} at or above the red threshold",
            Risk.AMBER: f"spoof probability {spoof_probability:.2f} at or above the amber threshold",
            Risk.GREEN: f"spoof probability {spoof_probability:.2f} below the amber threshold",
        }[risk]

        # The identity branch raises a separate flag rather than moving the
        # band.  A voice that is synthetic and a voice that belongs to someone
        # else are different findings and deserve different words.
        warning = self.config.identity_warning and speaker_status == SpeakerStatus.MISMATCH
        if warning:
            reason += "; enrolled voiceprint does not match"

        return Decision(risk=risk, action=action, reason=reason, identity_warning=warning)
