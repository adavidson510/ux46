[← UX46](../README.md) · [Architecture](architecture.md)

# Record something, decide what to do with it later

Choose **Record**, then **Record microphone**. Opening the panel does not turn on
the microphone. A visible timer, Pause and Stop remain available while you use
other conversations. This is separate from the microphone that dictates a message.
Recording never sends a prompt to an agent.

After Stop, open **Recordings** under Projects and sessions or in the top bar.
Recordings begin **Unfiled**. Open one to name it, choose a project, add tags,
participants or a location, and play or download the original audio. An expanded
project also has its own Recordings button. Moving something to trash is reversible.

When local transcription is configured, select **Transcribe locally**, or choose
the checkbox before recording. Refresh the panel to check progress. Timestamped
text links back to the audio. This uses a speech model on the workspace host,
not an agent or a paid transcription API. Recognition can be wrong; keep the audio
as the source. The recording itself does not consume agent tokens.

**Draft a summary request** places a request and transcript excerpt in the current
conversation's composer. You review and send it when wanted; that step uses your
normal agent. Save a reviewed summary under **Project, tags and summary** to make
it searchable in Session Vault. Ordinary transcript search also works inside the
recording library. Location and participants stay unknown unless supplied; there
is no GPS collection or speaker identification.

## Alpha limits

This records the selected microphone, not system or conference-call audio.
Headphones can prevent it from hearing other callers. Keep the page open and the
device awake; continuous capture with a locked phone or background mobile browser
is not supported. Browsers may interrupt capture. Saved pieces recover on reopening
and interrupted recordings are labeled. Device testing is still needed on Safari,
iPhone and long real meetings; synthetic Chromium capture and interrupted uploads
are covered by tests. Each recording is limited to 128 MiB.

Audio is saved in the browser before upload to the authenticated workspace host.
If the network fails, keep this browser's data: it holds the unuploaded pieces.
The library offers a local download while upload waits. Clear browser data only
after checking the host copy. UX46 does not automatically restart the microphone.

## For the AI maintaining this installation

The implementation is [recordings.js](../app/console/recordings.js),
[ux46_recordings.py](../tools/ux46_recordings.py) and the isolated
[ux46_transcribe.py](../tools/ux46_transcribe.py) worker. The store is
`<workspace-store>/recordings/recordings.sqlite3`, with ordered, retry-safe chunks.
The authenticated `/api/recordings/view`, `/action` and `/audio?id=…` endpoints
share the workspace login and mutation Origin/CSRF checks. Metadata edits require
the current recording version. No native runtime owns a recording.

`recordings/index/*.md` contains metadata and reviewed summaries, not raw audio
or transcripts. Session Vault's registry field `recordings_roots` lists these
index directories. Search/show/path can read them with `kind: recording`; native
session list/resume and runtime pointers remain separate. New standalone
registries include this path. For an existing installation, preserve and back up
its registry before adding the index path. Project choices come from the same
`ATLAS_REGISTRY` (or the host's default Atlas registry).

### Optional local transcription setup

Use an isolated Python environment outside the source tree. On a compatible
macOS or Linux host, install `faster-whisper==1.2.1`; the tested Python 3.9 host
also pins `numpy==1.26.4`. Download a compatible CTranslate2 Whisper model once
from its publisher, for example
[Systran/faster-whisper-base.en](https://huggingface.co/Systran/faster-whisper-base.en).
The base.en model is English-only. Choose a multilingual model if needed.
No model download occurs when a recording is transcribed.

Create private `<workspace-store>/recording-transcription.json` containing:

```json
{"python":"/absolute/path/to/venv/bin/python","model":"/absolute/path/to/model-directory"}
```

The executable and model directory must already exist. The worker forces offline
model loading and runs one recording at a time on CPU. Test with synthetic speech
before enabling the checkbox for real use. Configuration is optional: recording
and playback still work without transcription. Keep model weights, audio,
transcripts and this private configuration out of the source repository.
