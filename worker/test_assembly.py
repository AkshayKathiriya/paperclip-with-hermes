"""
test_assembly.py
----------------
Standalone test for the FFmpeg-based video assembly pipeline.
No Piper TTS, no Whisper, no Pexels needed.

Creates all inputs synthetically with ffmpeg + macOS `say`, then
calls assemble_video() directly and opens the result.

Run:
    python test_assembly.py
"""

import os
import sys
import subprocess
import tempfile
import shutil

WORK_DIR = tempfile.mkdtemp(prefix="yw_assembly_test_")
print(f"\n📁 Work dir: {WORK_DIR}\n")


def run(cmd: list, label: str, timeout: int = 120) -> None:
    print(f"  ▶ {label}...")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        print(f"  ❌ FAILED:\n{result.stderr[-600:]}")
        sys.exit(1)
    print(f"  ✅ done")


def check_ffmpeg():
    r = subprocess.run(["ffmpeg", "-version"], capture_output=True)
    if r.returncode != 0:
        print("❌ ffmpeg not found. Run:  brew install ffmpeg")
        sys.exit(1)
    print("✅ ffmpeg found")


# ── Step 1: generate synthetic inputs ─────────────────────────────────────────

def make_test_clips(n: int = 6, duration: int = 8) -> list[str]:
    """Generate n short solid-colour test clips using ffmpeg lavfi."""
    colors = ["0x1a1a2e", "0x16213e", "0x0f3460", "0x533483", "0xe94560", "0x0f3460"]
    clips = []
    clips_dir = os.path.join(WORK_DIR, "clips")
    os.makedirs(clips_dir, exist_ok=True)
    for i in range(n):
        path = os.path.join(clips_dir, f"scene_{str(i+1).zfill(2)}.mp4")
        color = colors[i % len(colors)]
        run([
            "ffmpeg", "-y",
            "-f", "lavfi",
            "-i", f"color=c={color}:size=1920x1080:rate=30:duration={duration}",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
            "-pix_fmt", "yuv420p",
            path,
        ], f"Clip {i+1}/{n}")
        clips.append(path)
    return clips


def make_test_audio(duration_approx: int = 48) -> str:
    """
    Generate a test narration WAV.
    Uses macOS `say` if available, otherwise a silent tone via ffmpeg.
    """
    wav_path = os.path.join(WORK_DIR, "narration.wav")

    # try macOS say first
    say_ok = subprocess.run(["which", "say"], capture_output=True).returncode == 0
    if say_ok:
        aiff_path = os.path.join(WORK_DIR, "narration.aiff")
        text = (
            "Welcome to this test video about the deep sea. "
            "The ocean covers more than seventy percent of the earth's surface. "
            "Scientists estimate that more than eighty percent of the ocean remains unexplored. "
            "Creatures like the anglerfish and the giant squid lurk in the darkness below. "
            "Modern ROVs equipped with HD cameras now let us observe the abyss directly. "
            "New species are discovered on almost every deep-sea expedition. "
            "Subscribe to learn more about ocean science every week."
        )
        run(["say", "-o", aiff_path, text], "macOS say TTS")
        run(["ffmpeg", "-y", "-i", aiff_path, "-ar", "22050", "-ac", "1", wav_path],
            "Convert AIFF → WAV")
    else:
        # silent test tone
        run([
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", f"sine=frequency=440:duration={duration_approx}",
            "-ar", "22050", wav_path,
        ], "Generate test tone WAV")

    return wav_path


def make_test_srt(audio_path: str) -> str:
    """Write a minimal SRT file matching the audio duration."""
    srt_path = os.path.join(WORK_DIR, "subtitles.srt")
    srt = (
        "1\n00:00:00,000 --> 00:00:04,000\nWelcome to this test video about the deep sea.\n\n"
        "2\n00:00:04,000 --> 00:00:08,000\nThe ocean covers 70% of the earth.\n\n"
        "3\n00:00:08,000 --> 00:00:14,000\n80% of the ocean remains unexplored.\n\n"
        "4\n00:00:14,000 --> 00:00:20,000\nAnglerfish lurk in the darkness below.\n\n"
        "5\n00:00:20,000 --> 00:00:28,000\nROVs let us observe the abyss directly.\n\n"
        "6\n00:00:28,000 --> 00:00:36,000\nNew species found on every expedition.\n\n"
        "7\n00:00:36,000 --> 00:00:46,000\nSubscribe to learn more every week.\n"
    )
    with open(srt_path, "w") as f:
        f.write(srt)
    return srt_path


# ── Step 2: run assembly ───────────────────────────────────────────────────────

def run_assembly(clips, audio_path, srt_path) -> str:
    # import from local pipeline
    sys.path.insert(0, os.path.dirname(__file__))
    from pipeline.assemble import assemble_video

    output_path = os.path.join(WORK_DIR, "final_video.mp4")
    print("\n🎬 Running assemble_video()...")
    assemble_video(
        video_clips=clips,
        audio_path=audio_path,
        srt_path=srt_path,
        output_path=output_path,
        resolution="1920x1080",
        fps=30,
    )
    return output_path


# ── Step 3: verify + open ─────────────────────────────────────────────────────

def verify(output_path: str) -> None:
    if not os.path.exists(output_path):
        print("❌ Output file not created!")
        sys.exit(1)

    size_mb = os.path.getsize(output_path) / 1024 / 1024
    print(f"\n✅ Output: {output_path}  ({size_mb:.1f} MB)")

    # probe the output with ffprobe
    r = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-show_streams", "-show_format", output_path],
        capture_output=True, text=True
    )
    import json
    info = json.loads(r.stdout)
    fmt  = info.get("format", {})
    streams = info.get("streams", [])
    video_s = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio_s = next((s for s in streams if s.get("codec_type") == "audio"), {})

    print(f"\n📊 Video info:")
    print(f"   Duration : {float(fmt.get('duration', 0)):.1f}s")
    print(f"   Video    : {video_s.get('codec_name')} {video_s.get('width')}×{video_s.get('height')} @ {video_s.get('r_frame_rate')} fps")
    print(f"   Audio    : {audio_s.get('codec_name')} {audio_s.get('sample_rate')}Hz")

    # open in QuickTime
    subprocess.run(["open", output_path])
    print("\n▶ Opening in QuickTime Player...")


# ── main ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 55)
    print("  YouTube Worker — Assembly Pipeline Local Test")
    print("=" * 55)

    check_ffmpeg()

    print("\n[1/3] Generating test inputs...")
    clips     = make_test_clips(n=6, duration=8)
    audio     = make_test_audio()
    srt       = make_test_srt(audio)
    print(f"  Clips : {len(clips)} × synthetic 1080p")
    print(f"  Audio : {audio}")
    print(f"  SRT   : {srt}")

    print("\n[2/3] Assembling video...")
    output = run_assembly(clips, audio, srt)

    print("\n[3/3] Verifying output...")
    verify(output)

    print(f"\n🧹 Work dir kept for inspection: {WORK_DIR}")
    print("   Delete when done:  rm -rf", WORK_DIR)
