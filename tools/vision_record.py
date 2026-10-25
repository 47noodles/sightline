"""Record face tracking data from a webcam, for tuning blink, wink and pointer settings offline.

    python tools/vision_record.py                     # guided session, about three minutes
    python tools/vision_record.py --free 60           # 60 seconds, no steps
    python tools/vision_record.py --video             # also keep the camera video (MJPG)
    python tools/vision_record.py --camera "Integrated Webcam"
    python tools/vision_record.py --list-cameras
    python tools/vision_record.py --source clip.mp4   # read a video file instead of a camera

Only the face measurements (landmarks, blendshapes, head pose) are saved
unless ``--video`` is given, and everything stays on this computer, in the
Sightline recordings folder.  The camera picture is never shown and the
pointer never moves.  During a guided session each step is printed and
marked with the system beep, so the steps that ask for closed eyes can be
followed.  The step names become labels in the recording, which
``tools/vision_replay.py`` uses for its report.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

GUIDED: list[tuple[str, float, str]] = [
    ("settle", 5, "Sit as you normally do and look at the middle of the screen."),
    ("open", 8, "Keep looking at the middle of the screen. Blink only when you need to."),
    ("natural", 25, "Read anything on the screen for 25 seconds. Blink as you normally would."),
    ("closed", 5, "Close both eyes and keep them closed until the next beep."),
    ("open", 3, "Open your eyes and look at the middle of the screen."),
    ("long_blink", 15, "Every few seconds, close both eyes for about half a second. Do it 4 times."),
    ("hold_blink", 15, "Close both eyes for about one and a half seconds, then open them. Do it 3 times."),
    ("wink_left", 12, "Wink your left eye 3 times, a few seconds apart."),
    ("wink_right", 12, "Wink your right eye 3 times, a few seconds apart."),
    ("head", 15, "Turn your head slowly left, right, up and down, then back to the middle."),
    ("look_down", 8, "Look down at your keyboard for a moment, then back at the screen. Do it twice."),
    ("away", 6, "Look away from the screen, or lean out of the camera's view."),
    ("back", 5, "Come back and look at the middle of the screen."),
]


def screen_size() -> list[int] | None:
    """Size of the primary screen, read only, so a replay can use the same pointer geometry."""
    try:
        import ctypes

        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        return [int(user32.GetSystemMetrics(0)), int(user32.GetSystemMetrics(1))]
    except Exception:
        return None


def beep() -> None:
    try:
        import winsound

        winsound.MessageBeep(winsound.MB_OK)  # asynchronous: does not hold up the frames
    except Exception:
        print("\a", end="", flush=True)


def open_source(args):
    """The frame source and a description of it for the recording's notes."""
    from sightline.vision.camera import OpenCVCamera, VideoFileSource, choose_camera, list_cameras

    if args.source:
        src = VideoFileSource(Path(args.source))
        src.open()
        return src, {"camera": src.description, "camera_virtual": False, "file": str(args.source)}
    devices = list_cameras()
    device = choose_camera(devices, args.camera)
    if device is None:
        raise SystemExit("No camera found. Check that a webcam is plugged in and not turned off.")
    if args.camera and args.camera not in (device.name, device.label):
        print(f"No camera called {args.camera!r}; using {device.label}.")
    if device.virtual:
        print(f"Note: {device.label} is a virtual camera. Pick a webcam with --camera if you have one.")
    cam = OpenCVCamera(device, args.width, args.height, args.fps)
    cam.open()
    return cam, {"camera": device.label, "camera_virtual": device.virtual, "backend": cam.backend}


def model_path(download: bool) -> Path:
    from sightline.core.models import ModelManager
    from sightline.vision.models import ALL_MODELS, FACE_LANDMARKER_ID

    models = ModelManager()
    models.register_all(ALL_MODELS)
    if not models.is_ready(FACE_LANDMARKER_ID):
        if not download:
            raise SystemExit("The face model is not downloaded yet. Run Sightline's setup, or add --download.")
        print("Downloading the face model (about 4 MB) ...")
        models.ensure([FACE_LANDMARKER_ID])
    return models.path(FACE_LANDMARKER_ID)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--free", type=float, metavar="SECONDS", help="record this long without guided steps")
    ap.add_argument("--video", action="store_true", help="also save the camera video")
    ap.add_argument("--camera", default="", help="camera name (default: the one in Sightline's settings)")
    ap.add_argument("--list-cameras", action="store_true", help="list cameras and exit")
    ap.add_argument("--source", help="read frames from a video file instead of a camera")
    ap.add_argument("--width", type=int, default=0)
    ap.add_argument("--height", type=int, default=0)
    ap.add_argument("--fps", type=int, default=0)
    ap.add_argument("--out", type=Path, help="recording folder (default: a new folder in the recordings folder)")
    ap.add_argument("--download", action="store_true", help="download the face model if it is missing")
    ap.add_argument("--quiet", action="store_true", help="no beeps")
    args = ap.parse_args(argv)

    from sightline.core import paths
    from sightline.settings import Settings
    from sightline.vision.camera import list_cameras, silence_opencv_logs
    from sightline.vision.landmarks import FaceLandmarker
    from sightline.vision.recorder import Recorder

    silence_opencv_logs()
    if args.list_cameras:
        devices = list_cameras()
        if not devices:
            print("No cameras found.")
        for d in devices:
            print(f"  {d.label}")
        return 0

    cfg = Settings.load().vision
    args.camera = args.camera or cfg.camera_name
    args.width = args.width or cfg.camera_width
    args.height = args.height or cfg.camera_height
    args.fps = args.fps or cfg.camera_fps
    landmarker = FaceLandmarker(model_path(args.download))
    source, info = open_source(args)
    live = not args.source
    steps = [("free", float(args.free), "Recording. Use the computer as you normally would.")] if args.free else GUIDED

    folder = args.out or paths.recordings_dir() / time.strftime("%Y-%m-%d_%H%M%S")
    meta = {
        **info,
        "mode": "free" if args.free else "guided",
        "steps": [list(s) for s in steps],
        "screen": screen_size(),
        "vision": {"camera_mirrored": bool(cfg.camera_mirrored), "camera_fov_deg": float(cfg.camera_fov_deg)},
    }
    rec = Recorder(folder, meta, video_fps=float(args.fps) if args.video else None)
    total = sum(s[1] for s in steps)
    print(f"Recording to {folder}")
    print(f"About {total:.0f} seconds. Press Ctrl+C to stop early; what is recorded so far is kept.\n")

    step = -1
    step_end: float | None = None
    started: float | None = None
    found = frames = 0
    last_print = 0.0
    try:
        while True:
            got = source.read(2.0)
            if got is None:
                if not live:
                    break
                print("\nWaiting for the camera ...")
                continue
            image, t = got
            if started is None:
                started = step_end = t
            assert step_end is not None
            if t >= step_end:
                step += 1
                if step >= len(steps):
                    break
                label, seconds, prompt = steps[step]
                rec.label(t, label)
                step_end += seconds
                print(f"\n[{step + 1}/{len(steps)}] {prompt}")
                if live and not args.quiet:
                    beep()
            face = landmarker.detect(image, t)
            rec.add(t, face, image if args.video else None, size=(image.shape[1], image.shape[0]))
            frames += 1
            found += face is not None
            if live and t - last_print >= 1.0:
                last_print = t
                rate = frames / max(t - started, 1e-6)
                status = "face found" if face is not None else "no face  "
                print(f"\r  {status}  {rate:4.1f} frames a second  {max(step_end - t, 0):4.0f} s left in this step", end="")
    except KeyboardInterrupt:
        print("\nStopped early.")
    finally:
        rec.close()
        source.close()
        landmarker.close()
    if live and not args.quiet:
        beep()
    share = 100.0 * found / max(frames, 1)
    print(f"\n\nSaved {frames} frames ({share:.0f}% with a face) to {folder}")
    print(f"Replay with: python tools/vision_replay.py \"{folder}\"")
    return 0


if __name__ == "__main__":
    sys.exit(main())
