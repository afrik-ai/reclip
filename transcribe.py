"""Speech-to-text for downloaded media, using Whisper models via faster-whisper.

faster-whisper runs OpenAI's Whisper models on CTranslate2: same accuracy as
the openai-whisper package, several times faster, no PyTorch, and it decodes
MP3/MP4 itself (PyAV) so it doesn't depend on the ffmpeg on PATH.

It's optional: install with ``pip install -r requirements-transcribe.txt``.
For an NVIDIA GPU also install ``requirements-transcribe-cuda.txt``.
"""

import glob
import importlib.util
import json
import os
import sys
import threading

CUDA_ERROR_HINTS = ("cublas", "cudnn", "cuda", "cudart", "nvrtc")


def is_available():
    return importlib.util.find_spec("faster_whisper") is not None


def _add_nvidia_dll_dirs():
    """Let ctranslate2 find cuBLAS/cuDNN installed from the nvidia-* pip wheels.

    On Linux those wheels are found automatically; on Windows their DLLs sit
    in site-packages/nvidia/*/bin, which isn't on the DLL search path.
    """
    if sys.platform != "win32":
        return
    for base in sys.path:
        for bin_dir in glob.glob(os.path.join(base, "nvidia", "*", "bin")):
            try:
                os.add_dll_directory(bin_dir)
            except (OSError, AttributeError):
                pass
            if bin_dir not in os.environ.get("PATH", ""):
                os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")


def _default_model_factory(name, device, compute_type):
    from faster_whisper import WhisperModel

    return WhisperModel(name, device=device, compute_type=compute_type)


def _cuda_device_count():
    try:
        import ctranslate2

        return ctranslate2.get_cuda_device_count()
    except Exception:
        return 0


class Transcriber:
    """Loads one Whisper model lazily and runs transcriptions with it.

    Callers serialize work (the engine uses a single worker), but the lock
    keeps model loading safe regardless.
    """

    def __init__(self, model="turbo", device="auto", compute_type="auto", model_factory=None):
        self.model_name = model
        self.requested_device = device
        self.requested_compute_type = compute_type
        self._factory = model_factory or _default_model_factory
        self._model = None
        self.device = None
        self._lock = threading.Lock()

    def _compute_type(self, device):
        if self.requested_compute_type != "auto":
            return self.requested_compute_type
        return "float16" if device == "cuda" else "int8"

    def _load(self, device):
        if device == "cuda":
            _add_nvidia_dll_dirs()
        self._model = self._factory(self.model_name, device, self._compute_type(device))
        self.device = device

    def _ensure_model(self):
        with self._lock:
            if self._model is not None:
                return
            device = self.requested_device
            if device == "auto":
                device = "cuda" if _cuda_device_count() > 0 else "cpu"
            try:
                self._load(device)
            except Exception as e:
                if not self._can_fall_back(e):
                    raise
                self._load("cpu")

    def _can_fall_back(self, error):
        # Only when the user didn't pin a device and the GPU libraries are the problem.
        return (self.requested_device == "auto" and self.device != "cpu"
                and any(h in str(error).lower() for h in CUDA_ERROR_HINTS))

    def transcribe(self, audio_path, language=None, on_progress=None):
        """Return {"language", "duration", "segments": [{"start","end","text"}], "text"}."""
        self._ensure_model()
        try:
            return self._run(audio_path, language, on_progress)
        except Exception as e:
            # Missing cuDNN/cuBLAS DLLs often only surface on the first decode.
            if not self._can_fall_back(e):
                raise
            with self._lock:
                self._load("cpu")
            return self._run(audio_path, language, on_progress)

    def _run(self, audio_path, language, on_progress):
        segments_iter, info = self._model.transcribe(
            audio_path, language=language or None, vad_filter=True, beam_size=5,
        )
        duration = float(getattr(info, "duration", 0) or 0)
        segments = []
        for seg in segments_iter:
            text = seg.text.strip()
            if text:
                segments.append({"start": round(seg.start, 2), "end": round(seg.end, 2), "text": text})
            if on_progress and duration:
                on_progress(min(99.0, round(seg.end * 100.0 / duration, 1)))
        return {
            "language": getattr(info, "language", None) or language,
            "duration": duration or None,
            "segments": segments,
            "text": "\n".join(s["text"] for s in segments),
        }


# --------------------------------------------------------------------------- #
# Output files
# --------------------------------------------------------------------------- #

TRANSCRIPT_FORMATS = ("txt", "srt", "vtt", "json")


def _timestamp(seconds, sep):
    ms = int(round(max(0.0, seconds) * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def to_srt(segments):
    blocks = [f"{i}\n{_timestamp(s['start'], ',')} --> {_timestamp(s['end'], ',')}\n{s['text']}\n"
              for i, s in enumerate(segments, 1)]
    return "\n".join(blocks)


def to_vtt(segments):
    blocks = [f"{_timestamp(s['start'], '.')} --> {_timestamp(s['end'], '.')}\n{s['text']}\n"
              for s in segments]
    return "WEBVTT\n\n" + "\n".join(blocks)


def write_outputs(result, out_dir, stem="transcript"):
    """Write txt/srt/vtt/json next to the media. Returns {format: path}."""
    os.makedirs(out_dir, exist_ok=True)
    contents = {
        "txt": result["text"] + ("\n" if result["text"] else ""),
        "srt": to_srt(result["segments"]),
        "vtt": to_vtt(result["segments"]),
        "json": json.dumps(result, ensure_ascii=False, indent=1),
    }
    paths = {}
    for fmt in TRANSCRIPT_FORMATS:
        path = os.path.join(out_dir, f"{stem}.{fmt}")
        # newline="\n": keep LF on Windows too, so files match across platforms.
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(contents[fmt])
        paths[fmt] = path
    return paths
