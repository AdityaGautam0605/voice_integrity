"""Cancelable biometric transform.

The governing fact: **a voiceprint is irrevocable.**  A password can be
rotated; a voice cannot.  If an embedding database leaks, those people are
permanently compromised against every voice biometric system that exists.
That is why encryption at rest is necessary but not sufficient here.

ISO/IEC 24745 asks for three properties encryption alone does not give:

    irreversibility   the stored template cannot be inverted to the biometric
    revocability      if compromised, issue a NEW template from the same voice
    unlinkability     templates for one person at two tenants cannot be linked

**Why you cannot simply hash it.**  Biometric matching is fuzzy: two
recordings of the same person produce different embeddings, so
``hash(a) == hash(b)`` is always false.  Password-style hashing is
structurally inapplicable, and this trips up almost everyone.

The construction below is random projection plus sign quantisation, in the
BioHashing family.  A per-subject, per-tenant seed defines the projection;
cosine distances survive it approximately (Johnson-Lindenstrauss), so matching
still works, and rotating the seed invalidates every old template.

Honest scope note: this is the basic construction.  Proving formal security
bounds is a research problem we are not solving.  What it does buy - and the
property that matters most for irrevocable data - is revocability.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass

import numpy as np

from vif.common.logging import get_logger

log = get_logger(__name__)


@dataclass
class TransformParams:
    """Public parameters of a subject's transform.

    The seed is a secret: with it, an attacker who also holds the template can
    attempt inversion.  It is stored encrypted alongside, never in plaintext.
    """

    seed: str
    in_dim: int = 192
    out_dim: int = 256
    version: int = 1

    def rotate(self) -> TransformParams:
        """New seed, same subject.  This is the revocation operation."""
        return TransformParams(
            seed=secrets.token_hex(32),
            in_dim=self.in_dim,
            out_dim=self.out_dim,
            version=self.version + 1,
        )


def new_params(in_dim: int = 192, out_dim: int = 256, tenant: str = "default") -> TransformParams:
    """Fresh transform parameters, bound to a tenant.

    Binding the seed to the tenant is what delivers unlinkability: the same
    voice enrolled at two institutions yields templates that cannot be
    correlated (SEC-09).
    """
    seed_material = f"{tenant}:{secrets.token_hex(32)}"
    return TransformParams(
        seed=hashlib.sha256(seed_material.encode()).hexdigest(),
        in_dim=in_dim,
        out_dim=out_dim,
    )


def _projection_matrix(params: TransformParams) -> np.ndarray:
    """Deterministic projection derived from the seed.

    Regenerated on demand rather than stored: the seed is the only secret
    that needs protecting, and it is far smaller than the matrix.
    """
    seed_int = int(hashlib.sha256(params.seed.encode()).hexdigest()[:16], 16)
    rng = np.random.default_rng(seed_int)
    matrix = rng.normal(0.0, 1.0 / np.sqrt(params.out_dim), size=(params.in_dim, params.out_dim))
    return matrix.astype(np.float32)


def transform(embedding: np.ndarray, params: TransformParams) -> np.ndarray:
    """Embedding -> protected template.

    Projection preserves distances approximately; the tanh compresses the
    tails so a single outlying dimension cannot dominate a match.  The result
    is not invertible without the seed, and is worthless once the seed rotates.
    """
    embedding = np.asarray(embedding, dtype=np.float32).ravel()
    if embedding.shape[0] != params.in_dim:
        raise ValueError(f"expected {params.in_dim}-d embedding, got {embedding.shape[0]}")

    norm = np.linalg.norm(embedding)
    if norm > 0:
        embedding = embedding / norm

    projected = embedding @ _projection_matrix(params)
    template = np.tanh(projected * 2.0)

    tnorm = np.linalg.norm(template)
    return (template / tnorm).astype(np.float32) if tnorm > 0 else template.astype(np.float32)


def compare(template_a: np.ndarray, template_b: np.ndarray) -> float:
    """Cosine similarity between two protected templates, in [-1, 1].

    Both must derive from the same seed.  Comparing across seeds is
    meaningless by construction - that is precisely what makes the transform
    revocable and unlinkable.
    """
    a = np.asarray(template_a, dtype=np.float32).ravel()
    b = np.asarray(template_b, dtype=np.float32).ravel()
    if a.shape != b.shape:
        raise ValueError("templates differ in dimension - different transform versions?")
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    return float(np.dot(a, b) / denom) if denom > 0 else 0.0


def distance_preservation_error(
    embeddings: np.ndarray,
    params: TransformParams,
    n_pairs: int = 200,
    seed: int = 0,
) -> float:
    """Mean absolute change in pairwise cosine similarity under the transform.

    Run this after changing `out_dim` or the compression.  If the transform
    distorts distances badly, verification accuracy quietly degrades and the
    speaker branch starts producing nonsense that looks like a model problem.
    """
    embeddings = np.asarray(embeddings, dtype=np.float32)
    if embeddings.ndim != 2 or embeddings.shape[0] < 2:
        raise ValueError("need at least two embeddings arranged as (n, dim)")

    rng = np.random.default_rng(seed)
    templates = np.stack([transform(e, params) for e in embeddings])

    errors = []
    for _ in range(n_pairs):
        i, j = rng.integers(0, len(embeddings), size=2)
        if i == j:
            continue
        a, b = embeddings[i], embeddings[j]
        original = float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-12))
        protected = compare(templates[i], templates[j])
        errors.append(abs(original - protected))

    return float(np.mean(errors)) if errors else 0.0
