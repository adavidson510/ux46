"""Offline-only speech recognition worker. This process has no agent or dispatch API."""
import json
from pathlib import Path
import sys


def main():
    from faster_whisper import WhisperModel
    source, output, model_path = sys.argv[1:]
    model = WhisperModel(model_path, device='cpu', compute_type='int8', cpu_threads=4, local_files_only=True)
    segments, info = model.transcribe(source, beam_size=3, vad_filter=True, condition_on_previous_text=False)
    rows = [{'start': s.start, 'end': s.end, 'text': s.text.strip()} for s in segments]
    Path(output).write_text(json.dumps({'text': '\n'.join(s['text'] for s in rows), 'segments': rows, 'language': info.language}))


if __name__ == '__main__': main()
