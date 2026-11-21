"""Locate a stable facecam in source footage using the local UltraFace detector."""
from functools import lru_cache
from pathlib import Path
from statistics import median
import subprocess

import numpy as np
import onnxruntime as ort

import cliptool as ct


WIDTH, HEIGHT = 640, 360


@lru_cache(maxsize=1)
def detector():
    options = ort.SessionOptions()
    options.intra_op_num_threads = 2
    options.inter_op_num_threads = 1
    options.log_severity_level = 3
    return ort.InferenceSession(str(ct.ROOT / "models" / "ultraface.onnx"), options, providers=["CPUExecutionProvider"])


@lru_cache(maxsize=1)
def opencv():
    try:
        import cv2
    except (ImportError, OSError):
        return None
    cv2.setNumThreads(1)
    return cv2


def detections(frame):
    cv2 = opencv()
    if cv2 is None:
        rows = np.linspace(0, frame.shape[0] - 1, 240).astype(int)
        cols = np.linspace(0, frame.shape[1] - 1, 320).astype(int)
        frame = frame[rows[:, None], cols]
    else:
        frame = cv2.resize(frame, (320, 240), interpolation=cv2.INTER_AREA)
    rgb = frame.astype(np.float32)
    pixels = np.ascontiguousarray(((rgb - 127) / 128).transpose(2, 0, 1)[None, ...])
    session = detector()
    scores, boxes = session.run(["scores", "boxes"], {session.get_inputs()[0].name: pixels})
    keep = np.flatnonzero(scores[0, :, 1] >= .79)
    ordered = sorted(keep, key=lambda index: scores[0, index, 1], reverse=True)
    found = []
    for index in ordered:
        left, top, right, bottom = np.clip(boxes[0, index], 0, 1)
        x, y = (left + right) / 2, (top + bottom) / 2
        if right - left > .018 and bottom - top > .027 and all(abs(x - a[0]) > .035 or abs(y - a[1]) > .045 for a in found):
            found.append((float(x), float(y), float(right - left), float(bottom - top)))
    return found[:6]


def grab_frame(path, offset):
    cmd = [ct.FFMPEG, "-nostdin", "-v", "error", "-ss", f"{offset:.3f}", "-i", str(path),
           "-frames:v", "1", "-vf", f"scale={WIDTH}:{HEIGHT}", "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1"]
    result = subprocess.run(cmd, capture_output=True, timeout=15, check=True, creationflags=ct.NOWIN)
    if len(result.stdout) == WIDTH * HEIGHT * 3:
        return np.frombuffer(result.stdout, dtype=np.uint8).reshape(HEIGHT, WIDTH, 3)
    return None


def grab_frames(path, offsets):
    """Sample a source in one decoder session, with FFmpeg for unsupported files."""
    try:
        import av
    except ImportError:
        return [grab_frame(path, offset) for offset in offsets]
    try:
        with av.open(str(path)) as container:
            stream = container.streams.video[0]
            stream.thread_count = 1
            origin = stream.start_time or 0
            frames = []
            for offset in offsets:
                target = origin + round(offset / float(stream.time_base))
                container.seek(target, stream=stream, backward=True)
                frame = next((frame for frame in container.decode(stream)
                              if frame.pts is not None and frame.pts >= target), None)
                frames.append(frame.reformat(width=WIDTH, height=HEIGHT, format="rgb24").to_ndarray()
                              if frame is not None else None)
        if sum(frame is not None for frame in frames) >= 2:
            return frames
    except (av.FFmpegError, ValueError, TypeError, IndexError):
        pass
    return [grab_frame(path, offset) for offset in offsets]


def checked_box(box):
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        return None
    x, y, w, h = box
    if 0 <= x < 1 and 0 <= y < 1 and .04 <= w <= .5 and .06 <= h <= .66 and x + w <= 1.001 and y + h <= 1.001:
        return x, y, w, h
    return None


def locate(path, start, duration, preferred=None):
    """Verify a configured crop or find a face that stays in the same region across the clip."""
    hint = checked_box(preferred)
    # Include an opening sample and separated points so a quick cutscene is less likely to look like a webcam.
    times = [.8, duration * .22, duration * .55, duration * .78]
    samples = grab_frames(path, [start + min(duration - .25, t) for t in times])
    frames = [frame for frame in samples if frame is not None]
    if len(frames) < 2:
        return None
    if hint:
        x, y, w, h = hint
        left, right, top, bottom = round(x * WIDTH), round((x + w) * WIDTH), round(y * HEIGHT), round((y + h) * HEIGHT)
        if sum(bool(detections(frame[top:bottom, left:right])) for frame in frames) >= 2:
            return list(hint)
    spots = [[face for face in detections(frame) if (face[0] < .29 or face[0] > .71) and face[2] < .24]
             for frame in frames]
    best = []
    for sample in spots:
        for face in sample:
            track = []
            for candidates in spots:
                close = [other for other in candidates if abs(other[0] - face[0]) < .075 and abs(other[1] - face[1]) < .095]
                if close:
                    track.append(min(close, key=lambda other: abs(other[0] - face[0]) + abs(other[1] - face[1])))
            if len(track) > len(best):
                best = track
    if len(best) < min(3, len(frames)):
        return None
    x, y, w, h = (median(axis) for axis in zip(*best))
    w, h = min(.34, max(.17, w * 3.1)), min(.4, max(.18, h * 2.8))
    left, top = min(max(0, x - w / 2), 1 - w), min(max(0, y - h * .44), 1 - h)
    return [round(value, 5) for value in (left, top, w, h)]
