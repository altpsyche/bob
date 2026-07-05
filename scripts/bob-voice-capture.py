"""
Record mic until silence, POST to whisper-server, print transcript.
Usage:
  python bob-voice-capture.py                    # record mic → transcript
  python bob-voice-capture.py --file <path>      # transcribe audio file
  python bob-voice-capture.py --port 8082 --silence-sec 1.5
Exit code 0 = success, 1 = error (server unreachable or no speech).
"""
import argparse
import os
import sys
import tempfile

# Force UTF-8 stdout so whisper transcripts with non-ASCII chars print cleanly on Windows.
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import osenv       # ONE-B3 — mic capture lives in the OS seam (single-sourced)
import bob_voice   # ONE-B4 — the STT client (transcribe) is single-sourced in the voice capability core
from bob_core import _port   # ONE-A Finding 4 — the STT port default lives in config/defaults.json (NB1)


def transcribe(wav_path: str, port: int) -> str:
    """POST a WAV file to whisper-server, return transcript text (delegates to the shared core)."""
    try:
        return bob_voice.transcribe(wav_path, port)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description='Mic capture + whisper transcription')
    parser.add_argument('--file',        help='Transcribe this audio file instead of mic')
    parser.add_argument('--port',        type=int,
                        default=int(os.environ.get('BOB_STT_PORT') or _port({}, 'sttPort')))
    parser.add_argument('--silence-sec', type=float, default=1.5, dest='silence_sec')
    args = parser.parse_args()

    if args.file:
        wav_path = args.file
        tmp_path = None
    else:
        print("Listening... (speak now, recording stops after silence)", file=sys.stderr)
        wav_bytes = osenv.record_audio(args.silence_sec)
        if not wav_bytes:
            print("Error: no speech detected", file=sys.stderr)
            sys.exit(1)
        fd, tmp_path = tempfile.mkstemp(suffix='.wav')
        try:
            with os.fdopen(fd, 'wb') as f:
                f.write(wav_bytes)
            wav_path = tmp_path
        except Exception:
            os.close(fd)
            raise

    try:
        transcript = transcribe(wav_path, args.port)
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)

    if not transcript:
        print("Error: empty transcript", file=sys.stderr)
        sys.exit(1)

    print(transcript)


if __name__ == '__main__':
    main()
