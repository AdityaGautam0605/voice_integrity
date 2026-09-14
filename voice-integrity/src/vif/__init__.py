"""Voice Integrity Verification Framework.

Layered after the SRS:
    L0 ingest        vif.serve.adapters
    L1 conditioning  vif.serve.vad, vif.serve.ringbuffer
    L2 encoding      vif.models.frontend
    L3 detection     vif.models.heads, vif.serve.liveness
    L4 scoring       vif.serve.scoring
    L5 policy        vif.serve.policy
    L6 action        vif.serve.server, vif.serve.challenge

Cross-cutting: vif.crypto (attestation, vault, audit log).
"""

__version__ = "0.1.0"
