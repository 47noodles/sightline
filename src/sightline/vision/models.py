"""Model files the vision subsystem needs.

The Face Landmarker task bundles three networks: a face detector, the
478-point face mesh with iris refinement, and the blendshape head (52 ARKit
style coefficients).  It also outputs the 4x4 facial transformation matrix,
which is where head pose comes from.
"""

from __future__ import annotations

from sightline.core.models import ModelFile, ModelSpec

FACE_LANDMARKER_ID = "face_landmarker"
FACE_LANDMARKER_FILE = "face_landmarker.task"

# Pinned with sightline.core.models.sha256_file on the file downloaded from the URL below.
FACE_LANDMARKER = ModelSpec(
    id=FACE_LANDMARKER_ID,
    title="Face and eye tracking model",
    files=(
        ModelFile(
            url=(
                "https://storage.googleapis.com/mediapipe-models/face_landmarker/"
                "face_landmarker/float16/1/face_landmarker.task"
            ),
            path=FACE_LANDMARKER_FILE,
            sha256="64184e229b263107bc2b804c6625db1341ff2bb731874b0bcc2fe6544e0bc9ff",
            size=3_758_596,
        ),
    ),
    license="Apache-2.0",
    homepage="https://ai.google.dev/edge/mediapipe/solutions/vision/face_landmarker",
    required=True,
)

ALL_MODELS: tuple[ModelSpec, ...] = (FACE_LANDMARKER,)
