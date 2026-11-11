"""Post-processing for the "pro" video model: motion-interpolated higher frame rate.

Flow renders 24 fps clips. The pro model raises them to VIDEO_PRO_FPS with ffmpeg's
motion-compensated interpolation (minterpolate, mci). It is CPU work (the server once
overheated on a busy browser cluster), so it runs niced, with a bounded thread count,
one clip at a time, and with a timeout. If ffmpeg is missing or fails, the caller
reports the pro model as unavailable instead of silently delivering a plain clip.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import tempfile

from . import logs
from . import settings as cfg

log = logs.get("video_post")
_ONE_AT_A_TIME = asyncio.Lock()


class PostError(RuntimeError):
    pass


def ffmpeg_path() -> str | None:
    return cfg.FFMPEG_BIN if os.path.isfile(cfg.FFMPEG_BIN or "") else shutil.which("ffmpeg")


def interpolate_args(src: str, dst: str, fps: int, threads: int) -> list[str]:
    vf = f"minterpolate=fps={fps}:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1"
    return ["-hide_banner", "-loglevel", "error", "-y", "-threads", str(threads), "-i", src,
            "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
            "-c:a", "copy", "-movflags", "+faststart", dst]


async def raise_fps(clip: bytes, fps: int | None = None) -> bytes:
    binary = ffmpeg_path()
    if not binary:
        raise PostError("ffmpeg is not installed")
    fps = fps or cfg.VIDEO_PRO_FPS
    folder = tempfile.mkdtemp(prefix="video-post-", dir=cfg.ATTACHMENT_DIR or None)
    src, dst = os.path.join(folder, "in.mp4"), os.path.join(folder, "out.mp4")
    try:
        with open(src, "wb") as f:
            f.write(clip)
        async with _ONE_AT_A_TIME:
            proc = await asyncio.create_subprocess_exec(
                "nice", "-n", "10", binary, *interpolate_args(src, dst, fps, cfg.VIDEO_POST_THREADS),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
            try:
                _, err = await asyncio.wait_for(proc.communicate(), timeout=cfg.VIDEO_POST_TIMEOUT)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise PostError("interpolation timed out")
        if proc.returncode != 0 or not os.path.isfile(dst):
            raise PostError(f"ffmpeg exited {proc.returncode}: {(err or b'').decode(errors='replace')[-200:]}")
        with open(dst, "rb") as f:
            out = f.read()
        if out[4:8] != b"ftyp":
            raise PostError("ffmpeg produced no mp4")
        return out
    finally:
        shutil.rmtree(folder, ignore_errors=True)
