"""Typed configuration loading.

Config is validated on load rather than on first use, so a malformed threshold
or a window/crop mismatch fails at startup instead of producing quietly wrong
numbers for three weeks.

Production-only capabilities are present as **flags defaulted off** rather than
as absent code.  The default path is the simple one: one detector, deterministic
thresholds, no key-management ceremony.  Turning a flag on enables something
that is already written and tested, which is a very different proposition from
building it under time pressure.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field, model_validator

from vif.common.logging import get_logger
from vif.common.types import Action

log = get_logger(__name__)


class AudioConfig(BaseModel):
    sample_rate: int = 16000
    window_samples: int = 64600
    hop_samples: int = 16000
    vad_frame: int = 512
    vad_threshold: float = 0.5

    @property
    def window_seconds(self) -> float:
        return self.window_samples / self.sample_rate

    @property
    def hop_seconds(self) -> float:
        return self.hop_samples / self.sample_rate

    @model_validator(mode="after")
    def _check(self) -> AudioConfig:
        if self.hop_samples > self.window_samples:
            raise ValueError("hop_samples must not exceed window_samples")
        if self.sample_rate not in (8000, 16000):
            raise ValueError("sample_rate must be 8000 or 16000")
        return self


class FrontendConfig(BaseModel):
    """Self-supervised front end.

    `enabled: false` runs a raw-waveform detector instead, which is what the
    edge tier uses and what a CPU-only demo machine may need.
    """

    enabled: bool = True
    model_id: str = "facebook/wav2vec2-xls-r-300m"
    hidden_dim: int = 1024
    frozen: bool = True
    unfreeze_top_n: int = 6
    layer: int = -1


class HeadConfig(BaseModel):
    arch: str = "aasist"
    gat_dims: list[int] = Field(default_factory=lambda: [64, 32])
    pool_ratios: list[float] = Field(default_factory=lambda: [0.5, 0.7, 0.5, 0.5])
    temperatures: list[float] = Field(default_factory=lambda: [2.0, 2.0, 100.0, 100.0])
    checkpoint: str = "models/checkpoints/codec_robust.pt"
    baseline_checkpoint: str = "models/checkpoints/baseline.pt"


class SpeakerConfig(BaseModel):
    enabled: bool = True
    model_id: str = "speechbrain/spkrec-ecapa-voxceleb"
    embedding_dim: int = 192


class ProsodyConfig(BaseModel):
    enabled: bool = False


class LivenessConfig(BaseModel):
    """Conversational liveness.

    Off by default: it needs both call directions and 10-20 turn transitions
    before it says anything trustworthy, so it is a stretch capability rather
    than part of the core demo path.  The implementation is complete and
    tested - flip `enabled` when both streams are available.
    """

    enabled: bool = False
    min_utterance_ms: float = 250.0
    resume_window_ms: float = 700.0
    backchannel_max_ms: float = 900.0
    fast_response_ms: float = 150.0
    min_transitions: int = 4


class ScoringConfig(BaseModel):
    """Deterministic thresholds on an independent spoof probability.

    No cross-branch fusion: branches are reported side by side so it is always
    visible which one fired.
    """

    amber_threshold: float = 0.50
    red_threshold: float = 0.80
    speaker_threshold: float = 0.25
    smoothing_windows: int = 5
    logit_scale: float = 2.0
    calibration: str = "configs/calibration.json"

    @model_validator(mode="after")
    def _check(self) -> ScoringConfig:
        if not 0.0 < self.amber_threshold < self.red_threshold < 1.0:
            raise ValueError("thresholds must satisfy 0 < amber < red < 1")
        if self.smoothing_windows < 1:
            raise ValueError("smoothing_windows must be at least 1")
        return self


class ModelConfig(BaseModel):
    audio: AudioConfig = Field(default_factory=AudioConfig)
    frontend: FrontendConfig = Field(default_factory=FrontendConfig)
    head: HeadConfig = Field(default_factory=HeadConfig)
    speaker: SpeakerConfig = Field(default_factory=SpeakerConfig)
    prosody: ProsodyConfig = Field(default_factory=ProsodyConfig)
    liveness: LivenessConfig = Field(default_factory=LivenessConfig)
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)


class ActionConfig(BaseModel):
    """What each band does.  Gating an action, never ending a call."""

    green: str = "PROCEED"
    amber: str = "CHALLENGE"
    red: str = "GATE_ACTION"

    @model_validator(mode="after")
    def _check(self) -> ActionConfig:
        if "TERMINATE" in self.red.upper():
            raise ValueError("the system gates the action, it never terminates the call")
        # Checked at load, not at first use: a misspelt action would otherwise
        # pass startup and then fail on the first scored window, verdict and all.
        known = sorted(action.value for action in Action)
        for band, value in (("green", self.green), ("amber", self.amber), ("red", self.red)):
            if value not in known:
                raise ValueError(f"unknown action for {band}: {value!r} (expected one of {known})")
        return self


class PolicyConfig(BaseModel):
    version: str = "policy-1.0.0"
    actions: ActionConfig = Field(default_factory=ActionConfig)
    fail_closed: bool = True
    challenge_on_amber: bool = True
    identity_warning: bool = True


class SecurityConfig(BaseModel):
    """Demo-appropriate security.

    Deferred to a production roadmap and defaulted off: KMS/HSM custody,
    per-tenant unlinkable templates, Merkle checkpoints with external
    anchoring, sender-constrained credentials, enclaves, homomorphic matching,
    federated updates.
    """

    sign_verdicts: bool = True
    api_token: str = ""  # empty disables auth; set VIF_API_TOKEN in deployment
    audit_log: bool = True
    merkle_checkpoints: bool = False
    cancelable_templates: bool = False
    encrypt_templates: bool = True


class CodecSpec(BaseModel):
    name: str
    rate: int
    bitrate: int | None = None
    weight: float = 1.0


class PacketLossConfig(BaseModel):
    probability: float = 0.3
    span_ms: list[float] = Field(default_factory=lambda: [20.0, 60.0])
    max_drops: int = 4


class NoiseConfig(BaseModel):
    probability: float = 0.4
    snr_db: list[float] = Field(default_factory=lambda: [15.0, 35.0])


class AugmentConfig(BaseModel):
    apply_probability: float = 0.7
    codecs: list[CodecSpec] = Field(default_factory=list)
    packet_loss: PacketLossConfig = Field(default_factory=PacketLossConfig)
    noise: NoiseConfig = Field(default_factory=NoiseConfig)


class AppConfig(BaseModel):
    model_config = {"protected_namespaces": ()}

    model: ModelConfig = Field(default_factory=ModelConfig)
    policy: PolicyConfig = Field(default_factory=PolicyConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    augment: AugmentConfig = Field(default_factory=AugmentConfig)
    root: Path = Path(".")

    def path(self, relative: str) -> Path:
        """Resolve a config-relative path against the project root."""
        p = Path(relative)
        return p if p.is_absolute() else self.root / p


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_config(
    config_dir: str | Path = "configs",
    root: str | Path | None = None,
) -> AppConfig:
    """Load model, policy, security and augment config from a directory.

    Missing files fall back to the defaults declared above, so the tests and
    the synthetic smoke path work without any YAML present.
    """
    config_dir = Path(config_dir)
    if not config_dir.is_dir():
        # Every setting silently taking its default is how a mistyped --config
        # goes unnoticed, so say so.
        log.warning("config directory %s not found - using built-in defaults", config_dir)
    root_path = Path(root) if root is not None else config_dir.parent
    policy_blob = _read_yaml(config_dir / "policy.yaml")
    return AppConfig(
        model=ModelConfig(**_read_yaml(config_dir / "model.yaml")),
        policy=PolicyConfig(**{k: v for k, v in policy_blob.items() if k != "security"}),
        security=SecurityConfig(**policy_blob.get("security", {})),
        augment=AugmentConfig(**_read_yaml(config_dir / "augment.yaml")),
        root=root_path,
    )
