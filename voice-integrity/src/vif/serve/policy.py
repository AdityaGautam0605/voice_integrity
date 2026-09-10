"""Policy evaluation (L5).

Two rules encoded here that are easy to get wrong.

**The score gates the action, never the call.**  A trustworthy score needs
10-15 seconds of speech that a live conversation does not politely provide, so
by the time we are confident the social engineering is already underway.  The
resolution is to put the decision where the loss actually occurs: the approval,
the disclosure, the reset.  A configuration that tries to terminate calls is
rejected at load (FR-PO-02).

**Thresholds are tiered by what is at stake.**  A single global operating
point is non-conforming.  A five-thousand-rupee transfer and a fifty-lakh
transfer should not share a threshold, because the cost of a false alarm
relative to a miss is completely different between them.
"""

from __future__ import annotations

from dataclasses import dataclass

from vif.common.config import PolicyConfig, TierConfig
from vif.common.logging import get_logger
from vif.common.types import Action, Band

log = get_logger(__name__)


@dataclass
class Decision:
    band: Band
    action: Action
    tier: str
    amber_threshold: float
    red_threshold: float
    reason: str

    def as_dict(self) -> dict:
        return {
            "band": self.band.value,
            "action": self.action.value,
            "tier": self.tier,
            "amber_threshold": self.amber_threshold,
            "red_threshold": self.red_threshold,
            "reason": self.reason,
        }


class PolicyEngine:
    def __init__(self, config: PolicyConfig | None = None):
        self.config = config or PolicyConfig()

    def select_tier(self, transaction_value: float | None) -> TierConfig:
        """Pick the tier whose ceiling the transaction falls under.

        With no value supplied we use the most conservative tier, because an
        unknown stake is not the same as a low one.
        """
        if not self.config.tiers:
            return TierConfig(name="default", max_value=None, amber=40.0, red=75.0)

        if transaction_value is None:
            return self.config.tiers[-1]

        for tier in self.config.tiers:
            if tier.max_value is None or transaction_value <= tier.max_value:
                return tier
        return self.config.tiers[-1]

    def evaluate(
        self,
        risk: float,
        transaction_value: float | None = None,
        verdict_verified: bool = True,
    ) -> Decision:
        """Map a risk score to a band and an action.

        `verdict_verified=False` short-circuits to the most severe outcome.
        A missing or unverifiable verdict is elevated risk, not absence of
        risk - otherwise the cheapest attack on this system is a cut network
        cable rather than a voice clone (SEC-01).
        """
        tier = self.select_tier(transaction_value)

        if self.config.fail_closed and not verdict_verified:
            return Decision(
                band=Band.RED,
                action=Action(self.config.actions.get("red", "gate_action")),
                tier=tier.name,
                amber_threshold=tier.amber,
                red_threshold=tier.red,
                reason="verdict missing or signature invalid - failing closed",
            )

        if risk >= tier.red:
            band = Band.RED
            reason = f"risk {risk:.1f} at or above red threshold {tier.red} for tier '{tier.name}'"
        elif risk >= tier.amber:
            band = Band.AMBER
            reason = (
                f"risk {risk:.1f} at or above amber threshold {tier.amber} for tier '{tier.name}'"
            )
        else:
            band = Band.GREEN
            reason = f"risk {risk:.1f} below amber threshold {tier.amber} for tier '{tier.name}'"

        action = Action(self.config.actions.get(band.value, "proceed"))
        return Decision(
            band=band,
            action=action,
            tier=tier.name,
            amber_threshold=tier.amber,
            red_threshold=tier.red,
            reason=reason,
        )

    def band_only(self, risk: float, transaction_value: float | None = None) -> Band:
        return self.evaluate(risk, transaction_value).band
