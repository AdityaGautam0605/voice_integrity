"""Regressions for bugs found in review.

Each test pins a defect that existed and was fixed.  They live apart from the
behavioural suites so it stays obvious what each one is guarding against.
"""

from __future__ import annotations

import asyncio
import os
import time

import numpy as np
import pytest

os.environ.setdefault("USE_TF", "0")  # keep transformers from importing TensorFlow

from vif.common.config import LivenessConfig, load_config  # noqa: E402
from vif.common.types import Side  # noqa: E402
from vif.data.datasets import FeatureDataset, crop_or_pad  # noqa: E402
from vif.data.manifests import Item  # noqa: E402
from vif.eval.metrics import det_curve, evaluate  # noqa: E402
from vif.serve.adapters.base import AudioFrame  # noqa: E402
from vif.serve.detector import BaseDetector  # noqa: E402
from vif.serve.liveness import (  # noqa: E402
    Utterance,
    build_transitions,
    compute_features,
    features_to_llr,
)
from vif.serve.session import CallSession  # noqa: E402
from vif.serve.vad import EnergyVAD  # noqa: E402


class _SlowDetector(BaseDetector):
    model_version = "slow"

    def __init__(self, delay: float):
        self.delay = delay

    def score_window(self, wav: np.ndarray) -> float:
        time.sleep(self.delay)
        return 0.0


# -- metrics ------------------------------------------------------------------


def _reference_det(labels, scores):
    """The original per-threshold definition, kept as the oracle."""
    bona, spoof = scores[labels == 1], scores[labels == 0]
    thresholds = np.sort(np.unique(np.concatenate([bona, spoof])))
    far = np.array([(bona >= t).mean() for t in thresholds])
    frr = np.array([(spoof < t).mean() for t in thresholds])
    return far, frr, thresholds


@pytest.mark.parametrize("ties", [False, True])
def test_det_curve_matches_the_per_threshold_definition(ties):
    rng = np.random.default_rng(7)
    labels = rng.integers(0, 2, 1500)
    scores = rng.normal(size=1500)
    if ties:
        scores = np.round(scores, 1)
    for got, want in zip(det_curve(labels, scores), _reference_det(labels, scores), strict=True):
        assert np.allclose(got, want)


def test_evaluate_scales_to_a_full_eval_set():
    """Was quadratic: roughly twenty seconds per call at ASVspoof eval size."""
    rng = np.random.default_rng(0)
    labels, scores = rng.integers(0, 2, 71237), rng.normal(size=71237)
    started = time.perf_counter()
    evaluate(labels, scores)
    assert time.perf_counter() - started < 2.0


# -- models and training ------------------------------------------------------


def test_score_orientation_is_spoof_minus_bonafide():
    """Class 0 is spoof.  The score was bonafide minus spoof, inverting every verdict."""
    torch = pytest.importorskip("torch")
    from vif.models.aasist import AASIST
    from vif.models.heads import LightHead

    logits = torch.tensor([[4.0, -4.0]])  # strongly prefers class 0, spoof
    assert AASIST.score_from_logits(logits).item() > 0
    assert LightHead.score_from_logits(logits).item() > 0


def test_aasist_runs_on_ssl_length_inputs():
    """Per-block time pooling reduced 201 SSL frames to zero by block five."""
    torch = pytest.importorskip("torch")
    from vif.models.aasist import AASIST

    model = AASIST(feat_dim=64).eval()
    with torch.no_grad():
        for frames in (201, 60, 8):
            assert tuple(model(torch.randn(2, frames, 64)).shape) == (2, 2)


def _feature_cache(directory, n, dim, rng):
    directory.mkdir()
    items = []
    for i in range(n):
        label = "spoof" if i % 2 else "bonafide"
        shift = 0.8 if label == "spoof" else 0.0
        features = rng.normal(size=(201, dim)) + shift
        np.save(directory / f"{i}.npy", features.astype(np.float16))
        items.append(Item(path=f"{i}.wav", label=label, split="x", corpus="synthetic"))
    return items


def test_training_on_separable_data_saves_a_working_checkpoint(tmp_path):
    """Inverted orientation trained to 100% EER, and EER 1.0 never beat the initial best."""
    pytest.importorskip("torch")
    from vif.common.config import HeadConfig
    from vif.models.heads import build_head
    from vif.train.loop import TrainConfig, train_head

    rng, dim = np.random.default_rng(0), 32
    train_items = _feature_cache(tmp_path / "train", 40, dim, rng)
    dev_items = _feature_cache(tmp_path / "dev", 20, dim, rng)
    checkpoint = tmp_path / "head.pt"

    history = train_head(
        build_head(HeadConfig(arch="light"), feat_dim=dim),
        train_items,
        dev_items,
        tmp_path / "train",
        tmp_path / "dev",
        TrainConfig(epochs=5, batch_size=8, learning_rate=1e-3, early_stop_patience=10),
        device="cpu",
        checkpoint_path=checkpoint,
        checkpoint_meta={
            "arch": "light",
            "feat_dim": dim,
            "frontend_id": "test",
            "window_samples": 64600,
            "condition": "synthetic",
        },
    )
    assert history.best_eer < 0.3
    assert checkpoint.exists()


def test_train_head_refuses_missing_metadata_before_training(tmp_path):
    """Used to run a full epoch, then crash at the first checkpoint save."""
    pytest.importorskip("torch")
    from vif.common.config import HeadConfig
    from vif.models.heads import build_head
    from vif.train.loop import TrainConfig, train_head

    items = [Item(path="0.wav", label="spoof", split="x", corpus="t")]
    with pytest.raises(ValueError, match="checkpoint_meta is missing"):
        train_head(
            build_head(HeadConfig(arch="light"), feat_dim=8),
            items,
            items,
            tmp_path / "does-not-exist",  # proves it failed before touching data
            tmp_path / "does-not-exist",
            TrainConfig(epochs=1),
            device="cpu",
            checkpoint_path=tmp_path / "x.pt",
        )


def test_frames_for_matches_wav2vec2_geometry():
    """Was one frame short: 400-sample receptive field, stride 320."""
    pytest.importorskip("torch")
    from vif.models.frontend import SSLFrontend

    assert SSLFrontend.frames_for(None, 64600) == 201
    assert SSLFrontend.frames_for(None, 16000) == 49


def test_extraction_resume_keeps_realised_codec_labels(tmp_path, monkeypatch):
    """A run killed before its final manifest write relabelled everything as clean."""
    pytest.importorskip("torch")
    transformers = pytest.importorskip("transformers")
    import soundfile as sf

    from vif.data.manifests import read_manifest
    from vif.train.extract import extract_features

    tiny = transformers.Wav2Vec2Model(
        transformers.Wav2Vec2Config(
            hidden_size=32, num_hidden_layers=1, num_attention_heads=2, intermediate_size=32
        )
    )
    monkeypatch.setattr(
        transformers.Wav2Vec2Model, "from_pretrained", staticmethod(lambda *a, **k: tiny)
    )

    config = load_config("configs")
    config.model.frontend.hidden_dim = 32
    config.augment.apply_probability = 1.0
    config.augment.packet_loss.probability = 1.0  # always relabels, even without ffmpeg

    rng = np.random.default_rng(0)
    items = []
    for i in range(6):
        path = tmp_path / f"{i}.wav"
        sf.write(path, (0.2 * rng.normal(size=16000)).astype(np.float32), 16000)
        items.append(Item(path=str(path), label="spoof", split="x", corpus="t"))

    out = tmp_path / "features"
    extract_features(items, config, out, device="cpu", augment=True, batch_size=4, seed=1)
    first = [item.condition for item in read_manifest(out / "manifest.jsonl")]
    assert all(condition != "clean" for condition in first)

    (out / "manifest.jsonl").unlink()  # the run "died" before writing it
    fresh = [Item(path=i.path, label=i.label, split=i.split, corpus=i.corpus) for i in items]
    extract_features(fresh, config, out, device="cpu", augment=True, batch_size=4, seed=1)
    assert [item.condition for item in read_manifest(out / "manifest.jsonl")] == first


# -- data ---------------------------------------------------------------------


def test_crop_or_pad_returns_full_length_for_empty_audio():
    """Tiling an empty array returned an empty array, breaking batch collation."""
    out = crop_or_pad(np.zeros(0, np.float32), 1000)
    assert out.shape == (1000,)
    assert not out.any()


def test_feature_dataset_skips_missing_features_and_keeps_indices(tmp_path):
    """One unreadable file used to crash training mid-epoch."""
    for i in (0, 2):
        np.save(tmp_path / f"{i}.npy", np.zeros((201, 8), np.float32))
    items = [Item(path=f"{i}.wav", label="spoof", split="x", corpus="t") for i in range(3)]
    dataset = FeatureDataset(tmp_path, items)
    assert len(dataset) == 2
    assert [dataset[k][2] for k in range(len(dataset))] == [0, 2]


# -- liveness -----------------------------------------------------------------


def _alternating(gap_ms: float, turns: int = 10, duration: float = 1.5) -> list[Utterance]:
    utterances, clock = [], 0.0
    for i in range(turns):
        side = Side.AGENT if i % 2 == 0 else Side.CALLER
        utterances.append(Utterance(side, clock, clock + duration))
        clock += duration + gap_ms / 1000.0
    return utterances


def test_symmetric_slow_conversation_is_not_evidence_of_a_machine():
    """Scored +1.46: 'no fast responses' ignored that the known human was equally slow."""
    config = LivenessConfig()
    features = compute_features(build_transitions(_alternating(500.0), config), config)
    assert features.fast_response_count == 0
    assert features.agent_fast_response_count == 0
    assert features_to_llr(features, config) == pytest.approx(0.0, abs=1e-9)


def test_perfectly_regular_caller_counts_as_compressed_variance():
    """A CV of exactly 0.0 is the most machine-like case, and truthiness skipped it."""
    config = LivenessConfig()
    rng = np.random.default_rng(3)
    utterances, clock = [], 0.0
    for i in range(14):
        side = Side.AGENT if i % 2 == 0 else Side.CALLER
        utterances.append(Utterance(side, clock, clock + 1.5))
        # The caller always replies after 400 ms; the agent varies like a human.
        gap = 400.0 if side == Side.AGENT else float(rng.uniform(-150.0, 900.0))
        clock += 1.5 + gap / 1000.0
    features = compute_features(build_transitions(utterances, config), config)
    assert features.caller_gap_cv == 0.0
    assert features.variance_ratio == 0.0


# -- serving ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_windows_arriving_during_inference_are_dropped_not_queued():
    """Inference was awaited inline, so a slow detector made the score lag without bound."""
    config = load_config("configs")
    session = CallSession(
        "backpressure", config, _SlowDetector(0.25), EnergyVAD(config.model.audio.vad_frame)
    )
    sample_rate, frame = 16000, 320
    for i in range(int(10 * sample_rate / frame)):
        n = np.arange(frame) + i * frame
        if i < 25:
            pcm = np.zeros(frame, np.float32)
        else:
            pcm = (0.4 * np.sin(2 * np.pi * 220 * n / sample_rate)).astype(np.float32)
        await session.handle_frame(AudioFrame(Side.CALLER, pcm, time.monotonic_ns()))
    await session.wait_idle()
    assert session.stats.windows_scored >= 1
    assert session.stats.windows_dropped >= 1
    session.close()


def test_decode_pcm_tolerates_a_partial_trailing_sample():
    """A frame with an odd byte count raised, ending the whole session."""
    from vif.serve.server import _decode_pcm

    assert len(_decode_pcm(bytes([1]))) == 0
    assert len(_decode_pcm(bytes([1, 2, 3]))) == 1


def test_sessions_that_never_stream_are_pruned():
    """Every abandoned session used to live for the life of the process."""
    from vif.serve import server

    config = load_config("configs")

    def make(session_id):
        return CallSession(
            session_id, config, _SlowDetector(0.0), EnergyVAD(config.model.audio.vad_frame)
        )

    stale, streaming, fresh = make("stale"), make("streaming"), make("fresh")
    old = time.monotonic_ns() - int((server.IDLE_SESSION_TTL_S + 60) * 1e9)
    stale.started_ns = old
    streaming.started_ns = old
    streaming.stats.frames_ingested = 5

    server.state.sessions.clear()
    server.state.sessions.update({"stale": stale, "streaming": streaming, "fresh": fresh})
    server._prune_idle_sessions()
    assert set(server.state.sessions) == {"streaming", "fresh"}
    server.state.sessions.clear()


def test_auto_backend_never_silently_falls_back_to_the_stub(tmp_path, monkeypatch):
    """A server with no weights used to start anyway and sign verdicts from a stand-in."""
    from vif.serve.detector import build_detector

    config = load_config("configs")
    monkeypatch.chdir(tmp_path)  # no checkpoints and no exported model here
    started = time.perf_counter()
    with pytest.raises(RuntimeError, match="VIF_BACKEND=stub"):
        build_detector(config.model, backend="auto")
    # The checkpoint is checked before the front end, so nothing is downloaded.
    assert time.perf_counter() - started < 10.0


def test_serve_passes_the_config_directory_to_the_server(monkeypatch):
    """--config was accepted by the CLI and then ignored by the server's lifespan."""
    from vif import cli
    from vif.serve import server

    monkeypatch.setenv("VIF_CONFIG", "unchanged")
    monkeypatch.setattr(server, "run", lambda host, port: None)
    assert cli.main(["--config", "elsewhere", "serve"]) == 0
    assert os.environ["VIF_CONFIG"] == "elsewhere"


@pytest.mark.asyncio
async def test_verdict_carries_the_liveness_score_when_enabled():
    """Stream messages reported liveness_score; the signed verdict dropped it."""
    from vif.serve.adapters.file import SyntheticAdapter

    config = load_config("configs")
    config.model.liveness.enabled = True
    adapter = SyntheticAdapter(n_turns=16, pipeline_floor_ms=320.0, seed=1, realtime=False)
    session = CallSession(
        "liveness", config, _SlowDetector(0.0), EnergyVAD(config.model.audio.vad_frame)
    )
    await asyncio.gather(
        session.consume(adapter, Side.CALLER),
        session.consume(adapter, Side.AGENT),
    )
    payload = session.finalise()
    session.close()
    assert payload.liveness_score is not None
    assert payload.liveness_score > 0


def test_onnx_export_does_not_quantise_convolutions(tmp_path, monkeypatch):
    """Quantising every op produced ConvInteger, about 13x slower than fp32 on CPU."""
    pytest.importorskip("torch")
    pytest.importorskip("onnxruntime")
    onnx = pytest.importorskip("onnx")
    transformers = pytest.importorskip("transformers")
    import importlib.util
    import sys
    from pathlib import Path

    from vif.common.config import HeadConfig
    from vif.models.heads import build_head, save_checkpoint

    tiny = transformers.Wav2Vec2Model(
        transformers.Wav2Vec2Config(
            hidden_size=32, num_hidden_layers=1, num_attention_heads=2, intermediate_size=32
        )
    ).eval()
    monkeypatch.setattr(
        transformers.Wav2Vec2Model, "from_pretrained", staticmethod(lambda *a, **k: tiny)
    )
    spec = importlib.util.spec_from_file_location("export_onnx", Path("scripts/export_onnx.py"))
    export_onnx = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "export_onnx", export_onnx)
    spec.loader.exec_module(export_onnx)

    config = load_config("configs")
    checkpoint = tmp_path / "head.pt"
    save_checkpoint(
        build_head(HeadConfig(arch="aasist"), feat_dim=32),
        checkpoint,
        arch="aasist",
        feat_dim=32,
        frontend_id=config.model.frontend.model_id,
        window_samples=config.model.audio.window_samples,
        condition="test",
    )
    exported = export_onnx.export(str(checkpoint), str(tmp_path / "det.onnx"), "configs")
    ops = {node.op_type for node in onnx.load(str(exported)).graph.node}
    assert "ConvInteger" not in ops
    assert "MatMulInteger" in ops


def test_missing_config_directory_warns(tmp_path, monkeypatch):
    """A mistyped --config used to run silently on built-in defaults."""
    import vif.common.config as config_module

    messages = []
    monkeypatch.setattr(
        config_module.log, "warning", lambda msg, *args: messages.append(msg % args)
    )
    load_config(tmp_path / "no-such-dir")
    assert any("not found" in message for message in messages)

    messages.clear()
    load_config("configs")
    assert not messages


# -- second review pass ---------------------------------------------------------


class _FailingVault:
    """Enrolled, but matching raises - a dimension mismatch or a corrupt record."""

    def is_enrolled(self, speaker_id):
        return True

    def match(self, speaker_id, embedding):
        raise ValueError("templates differ in dimension")


class _EmbeddingDetector(_SlowDetector):
    def embed_speaker(self, wav):
        return np.ones(192, dtype=np.float32)


async def _speak(session, seconds: float, start_ns: int = 0) -> None:
    """Half a second of silence for the energy VAD to calibrate on, then a tone."""
    sample_rate, frame = 16000, 320
    for i in range(int(seconds * sample_rate / frame)):
        n = np.arange(frame) + i * frame
        if i < 25:
            pcm = np.zeros(frame, np.float32)
        else:
            pcm = (0.4 * np.sin(2 * np.pi * 220 * n / sample_rate)).astype(np.float32)
        timestamp = start_ns + int(i * frame / sample_rate * 1e9)
        await session.handle_frame(AudioFrame(Side.CALLER, pcm, timestamp))
    await session.wait_idle()


@pytest.mark.asyncio
async def test_a_failing_speaker_branch_keeps_the_score_and_the_verdict():
    """An exception from the vault escaped the scoring task, and teardown lost the verdict."""
    config = load_config("configs")
    session = CallSession(
        "speaker-fails",
        config,
        _EmbeddingDetector(0.0),
        EnergyVAD(config.model.audio.vad_frame),
        speaker_id="ceo-001",
        vault=_FailingVault(),
    )
    await _speak(session, seconds=8.0)
    payload = session.finalise()
    session.close()
    assert payload.windows_scored >= 1
    assert payload.speaker_status.value == "NOT_ENROLLED"


@pytest.mark.asyncio
async def test_liveness_closes_open_utterances_on_the_media_clock():
    """Open turns were closed at wall-clock time, which under fast replay precedes them."""
    config = load_config("configs")
    config.model.liveness.enabled = True
    session = CallSession(
        "media-clock", config, _SlowDetector(0.0), EnergyVAD(config.model.audio.vad_frame)
    )
    # Media time a minute ahead of the wall clock, and the tone never stops, so
    # the caller's utterance is still open at teardown.
    await _speak(session, seconds=3.0, start_ns=time.monotonic_ns() + int(60e9))
    session.finalise()
    utterances = session.liveness.tracker.utterances
    session.close()
    assert len(utterances) == 1
    assert 2000.0 < utterances[0].duration_ms < 3000.0


def test_smoothing_is_not_capped_at_eight_windows():
    """The history deque held eight windows whatever smoothing_windows said."""
    from vif.common.config import ScoringConfig
    from vif.serve.scoring import Scorer

    scorer = Scorer(ScoringConfig(smoothing_windows=12))
    for _ in range(11):
        scorer.update_spoof(-4.0)
    scorer.update_spoof(30.0)
    # Over twelve windows one confident window cannot outvote eleven; over eight it could.
    assert scorer.spoof_probability < 0.5


def test_platt_calibration_does_not_bake_in_the_class_ratio():
    """Fitted at nine spoofs per bonafide, a score carrying no evidence read as p = 0.9."""
    from vif.eval.calibration import fit_platt

    rng = np.random.default_rng(0)
    labels = np.array([1] * 1000 + [0] * 9000)
    scores = rng.normal(0.0, 1.0, labels.size)  # no information about the label
    params = fit_platt(labels, scores, split="dev")
    assert abs(params.to_llr(0.0)) < 0.2
    assert params.prior_log_odds == pytest.approx(np.log(9.0))


def test_unknown_action_is_rejected_at_config_load():
    """A misspelt action passed startup, then failed on the first scored window."""
    from vif.common.config import ActionConfig

    with pytest.raises(ValueError, match="unknown action for amber"):
        ActionConfig(amber="CHALENGE")


def test_concurrent_audit_appends_keep_the_chain_intact(tmp_path):
    """The tip was read outside the write lock, so concurrent appends collided on seq."""
    from concurrent.futures import ThreadPoolExecutor

    from vif.common.types import Risk, Verdict, VerdictPayload
    from vif.crypto.auditlog import AuditLog

    audit = AuditLog(tmp_path / "audit.db")
    verdict = Verdict(
        payload=VerdictPayload(session_id="s", spoof_probability=0.1, risk=Risk.GREEN)
    )
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: audit.append(verdict), range(200)))
    assert audit.count() == 200
    assert audit.verify_chain()[0]
    audit.close()


def test_list_enrolled_reports_the_stored_transform_version(tmp_path):
    """Every record was listed as transform version 1, plain embeddings included."""
    from vif.crypto.keys import LocalKeyStore
    from vif.crypto.vault import VoiceprintVault

    keystore = LocalKeyStore(tmp_path / "keys.json")
    embedding = np.random.default_rng(0).normal(size=192).astype(np.float32)
    for use_cancelable, expected in ((False, 0), (True, 1)):
        vault = VoiceprintVault(
            tmp_path / f"{expected}.db", keystore, use_cancelable=use_cancelable
        )
        vault.enrol("speaker", embedding)
        assert [r.transform_version for r in vault.list_enrolled()] == [expected]
        vault.close()


def test_finetuned_head_refuses_to_load_without_its_front_end_layers(tmp_path):
    """A fine-tuned head was served on the stock pretrained layers, with no error."""
    pytest.importorskip("torch")
    from types import SimpleNamespace

    from vif.models.frontend import load_released_layers

    frontend = SimpleNamespace(config=SimpleNamespace(model_id="facebook/wav2vec2-xls-r-300m"))
    with pytest.raises(FileNotFoundError, match="frontend_top_layers"):
        load_released_layers(frontend, tmp_path / "finetuned.pt")


@pytest.fixture
def api(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from contextlib import asynccontextmanager

    from fastapi.testclient import TestClient

    from vif.serve import server

    @asynccontextmanager
    async def no_lifespan(_app):
        yield

    monkeypatch.delenv("VIF_API_TOKEN", raising=False)
    server.bootstrap(
        config_dir="configs", backend="stub", keys_dir=tmp_path / "keys", data_dir=tmp_path / "data"
    )
    app = server.create_app()
    app.router.lifespan_context = no_lifespan
    with TestClient(app) as client:
        yield client
    server.state.sessions.clear()
    server.state.verdicts.clear()


def test_malformed_requests_are_rejected_rather_than_crashing(api):
    """A malformed verdict or embedding raised inside the handler and became a 500."""
    assert api.post("/v1/verdict/verify", json={"signature": "x"}).status_code == 422
    session_id = api.post("/v1/session").json()["session_id"]
    enrol = {"speaker_id": "a", "embedding_b64": "AAA"}  # not valid base64
    assert api.post(f"/v1/enroll/{session_id}", json=enrol).status_code == 422


# -- third review pass ----------------------------------------------------------


def test_every_aasist_parameter_takes_part_in_the_forward_pass():
    """The heterogeneous layers defined per-type projections and never applied them."""
    torch = pytest.importorskip("torch")
    from vif.models.aasist import AASIST

    model = AASIST(feat_dim=64)
    model(torch.randn(2, 60, 64)).sum().backward()
    assert [name for name, p in model.named_parameters() if p.grad is None] == []


def test_train_head_returns_the_best_epoch_weights(tmp_path, monkeypatch):
    """The head came back in its last-epoch state, so calibration fitted weights never saved."""
    torch = pytest.importorskip("torch")
    import vif.train.loop as loop
    from vif.common.config import HeadConfig
    from vif.eval.metrics import MetricResult
    from vif.models.heads import build_head

    # Dev EER improves at epoch 1 and then worsens, so the best is not the last.
    eers = iter([0.30, 0.10, 0.20, 0.25])
    monkeypatch.setattr(
        loop,
        "evaluate",
        lambda labels, scores: MetricResult(
            eer=next(eers), eer_threshold=0.0, tpr_at_1pct_fpr=0.0, tpr_at_5pct_fpr=0.0, auc=0.5
        ),
    )
    dim = 16
    items = _feature_cache(tmp_path / "train", 16, dim, np.random.default_rng(0))
    checkpoint = tmp_path / "head.pt"
    head = build_head(HeadConfig(arch="light"), feat_dim=dim)
    loop.train_head(
        head,
        items,
        items,
        tmp_path / "train",
        tmp_path / "train",
        loop.TrainConfig(epochs=4, batch_size=8, learning_rate=1e-2, early_stop_patience=10),
        device="cpu",
        checkpoint_path=checkpoint,
        checkpoint_meta={
            "arch": "light",
            "feat_dim": dim,
            "frontend_id": "test",
            "window_samples": 64600,
            "condition": "test",
        },
    )
    saved = torch.load(checkpoint, map_location="cpu", weights_only=False)
    assert saved["epoch"] == 1
    for key, value in saved["state_dict"].items():
        assert torch.equal(head.state_dict()[key].cpu(), value), key


# -- fourth review pass ---------------------------------------------------------


def test_metrics_do_not_leak_session_ids_without_the_token(api, monkeypatch):
    """/metrics listed every live session id unauthenticated - and an id is enough to stream."""
    session_id = api.post("/v1/session").json()["session_id"]
    monkeypatch.setenv("VIF_API_TOKEN", "s3cret")
    assert api.get("/metrics").status_code == 401
    assert api.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 401
    authorised = api.get("/metrics", headers={"Authorization": "Bearer s3cret"})
    assert authorised.status_code == 200
    assert session_id in authorised.json()["sessions"]


# -- fifth review pass (found by running the server for real) -------------------


def test_sessions_do_not_retry_an_unavailable_neural_vad(api, monkeypatch):
    """Each new session retried Silero inside the request handler, stalling every live stream."""
    import vif.serve.vad as vad_module
    from vif.serve import server

    attempts = []

    def unavailable(self, *args, **kwargs):
        attempts.append(1)
        raise ImportError("silero_vad is not installed")

    monkeypatch.setattr(vad_module.SileroVAD, "__init__", unavailable)
    monkeypatch.setattr(server.state, "vad_kind", "energy")  # what the startup probe found
    for _ in range(3):
        assert api.post("/v1/session").status_code == 200
    assert attempts == []


def test_manifest_paths_use_forward_slashes():
    """A manifest built on Windows stored backslashes, which a Linux training machine cannot open."""
    item = Item(path="data\\raw\\LA\\x.flac", label="spoof", split="train", corpus="t")
    assert item.path == "data/raw/LA/x.flac"
