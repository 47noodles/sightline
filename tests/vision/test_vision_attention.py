"""Attention: was a face there, turned to the screen, with the mouth moving, during an utterance?"""

from __future__ import annotations

import math
from dataclasses import replace

import numpy as np
import pytest
from vision_fakes import OPEN, FakeLandmarker, PacedSource, image, wait_for
from vision_synth import SyntheticUser

from sightline.core.events import PointerSample
from sightline.core.types import Attention
from sightline.vision.attention import FACING_DEG, LOWER_LIP, UPPER_LIP, AttentionTracker
from sightline.vision.landmarks import BS
from sightline.vision.pipeline import VisionPipeline
from sightline.vision.service import VisionService

IOD = 150.0  # the synthetic face's outer eye corner distance in pixels


def face_at(user: SyntheticUser, t: float, jaw: float | None = None, lip_gap_px: float | None = None, bs: bool = True):
    f = user.look(t, 960, 540, noise_px=0.0, bs_noise=0.0)
    blend = f.blendshapes.copy() if bs else np.zeros_like(f.blendshapes)
    if jaw is not None and bs:
        blend[BS["jawOpen"]] = jaw
    lm = f.landmarks.copy()
    if lip_gap_px is not None:
        lm[LOWER_LIP, 1] = lm[UPPER_LIP, 1] + lip_gap_px / f.height
    return replace(f, blendshapes=blend, landmarks=lm)


def feed(tracker: AttentionTracker, user, t0: float, seconds: float, *, yaw: float = 0.0, **kw) -> float:
    t = t0
    for _ in range(int(round(seconds * 30))):
        jaw = kw.get("jaw")
        gap = kw.get("gap")
        face = face_at(user, t, jaw(t) if callable(jaw) else jaw, gap(t) if callable(gap) else gap, kw.get("bs", True))
        tracker.update(t, yaw, 0.0, face, IOD)
        t += 1 / 30
    return t


def talking(t: float) -> float:
    return 0.25 + 0.2 * math.sin(2 * math.pi * 4.0 * t)  # about four syllables a second


def test_facing_the_screen_or_turned_away(user):
    tr = AttentionTracker()
    t = feed(tr, user, 0.0, 1.0, yaw=3.0)
    feed(tr, user, t, 1.0, yaw=FACING_DEG + 15)
    first = tr.query(0.0, 0.99)
    assert first.face == 1.0 and first.facing == 1.0 and first.frames == 30
    assert tr.query(1.0, 2.0).facing == 0.0
    assert tr.query(0.0, 2.0).facing == pytest.approx(0.5, abs=0.02)


def test_mouth_moves_while_talking_and_not_while_still(user):
    tr = AttentionTracker()
    t = feed(tr, user, 0.0, 2.0, jaw=talking)
    feed(tr, user, t, 2.0, jaw=0.12)
    speaking = tr.query(0.0, 2.0)
    still = tr.query(2.2, 4.0)
    assert speaking.mouth is not None and speaking.mouth > 0.5
    assert still.mouth == 0.0


def test_the_lip_gap_alone_counts(user):
    tr = AttentionTracker()
    feed(tr, user, 0.0, 2.0, jaw=0.1, gap=lambda t: 4.0 + 10.0 * (1 + math.sin(2 * math.pi * 4.0 * t)))
    a = tr.query(0.0, 2.0)
    assert a.mouth is not None and a.mouth > 0.4


def test_mouth_is_unknown_without_blendshapes(user):
    tr = AttentionTracker()
    feed(tr, user, 0.0, 1.0, jaw=talking, bs=False)
    a = tr.query(0.0, 1.0)
    assert a.mouth is None and a.face == 1.0 and a.frames == 30


def test_no_face_and_empty_windows(user):
    tr = AttentionTracker()
    t = feed(tr, user, 0.0, 1.0, jaw=talking)
    for i in range(30):
        tr.lost(t + i / 30)
    a = tr.query(0.0, 2.0)
    assert a.face == pytest.approx(0.5, abs=0.02) and a.frames == 60
    assert tr.query(5.0, 6.0) == Attention(0.0, 0.0, None, 0)
    # After a gap the first frames have nothing to compare with: unknown, not "still".
    feed(tr, user, t + 1.0, 0.05, jaw=talking)
    assert tr.query(t + 1.0, t + 1.05).mouth is None


def test_history_is_bounded(user):
    tr = AttentionTracker(history_s=15.0)
    feed(tr, user, 0.0, 20.0, jaw=0.1)
    assert tr.query(0.0, 4.0).frames == 0
    assert abs(tr.query(10.0, 20.0).frames - 300) <= 1


def test_pipeline_records_attention_relative_to_the_neutral_pose(vctx, user):
    pipe = VisionPipeline(vctx)
    t = 10.0
    for yaw, seconds in ((0.0, 1.0), (35.0, 1.0)):
        for _ in range(30 * int(seconds)):
            pipe.process(user.look(t, 960, 540, yaw=yaw), t)
            t += 1 / 30
    first = pipe.attention.query(10.0, 10.95)
    assert first.facing == 1.0
    assert first.mouth is not None and first.mouth < 0.1  # a still face with the face model's usual jitter
    assert pipe.attention.query(11.2, 12.0).facing == 0.0
    # "Recenter" makes the turned pose the new neutral.
    pipe.request_recenter()
    start = t
    for _ in range(30):
        pipe.process(user.look(t, 960, 540, yaw=35.0), t)
        t += 1 / 30
    assert pipe.attention.query(start + 0.1, t).facing == 1.0
    for _ in range(10):
        pipe.process(None, t)
        t += 1 / 30
    assert pipe.attention.query(t - 0.3, t).face == 0.0


def test_service_attention_only_while_running(vctx, rec):
    user = SyntheticUser(seed=9)
    svc = VisionService(
        vctx,
        source_factory=lambda: PacedSource([image(OPEN)] * 10, t0=700.0),
        landmarker_factory=lambda: FakeLandmarker(user),
        start_pump=False,
    )
    assert svc.attention(0.0, 1e9) is None
    svc.start()
    try:
        assert wait_for(lambda: len(rec.of(PointerSample)) > 20)
        a = svc.attention(700.0, 1e9)
        assert a is not None and a.frames > 20 and a.face == 1.0 and a.facing == 1.0
    finally:
        svc.stop()
    assert svc.attention(0.0, 1e9) is None
