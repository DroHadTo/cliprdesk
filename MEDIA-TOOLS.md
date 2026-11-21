# GTA6 Clip Desk media workflow

Capture a stream highlight, find the creator facecam, render the vertical clip with timed captions, and let Umbrel check the opening and payoff before Buffer receives it. Clip Desk keeps the full PC render to one style by default. The other styles can still be rendered from Review.

## Import a VOD cut

Use a Twitch VOD or YouTube video you have permission to edit. Choose a start in seconds and a length of 45, 60, or 75 seconds. The import makes a local review clip. Use an approved creator name when the creator filter is active. YouTube extraction may require Node.js 22 or newer on PATH, or Deno 2.3 or newer.

```powershell
.venv\Scripts\python.exe -m pip install --only-binary :all: --require-hashes --no-deps -r requirements-media.txt
.venv\Scripts\python.exe media_tools.py vod "https://www.twitch.tv/videos/VOD_ID" --creator "CREATOR" --title "Describe the moment" --start 120 --length 60
```

For an OBS recording already saved on the PC, use the existing local entry point.

```powershell
.venv\Scripts\python.exe cliptool.py test-local "C:\Videos\recording.mp4" "CREATOR" "Describe the moment" 120 60
```

## Edit and export

Download the edit package in Review and extract it into one folder. Use `clip.mp4` for the selected vertical edit, or `source.mp4` and the trim points in `clip.json` when the source is included. The selected MP4 has its subtitle styling baked in. Use `captions.ass`, `captions.srt` or `captions.vtt` if rebuilding subtitles from the source. Run `editly editly.json5` from the extracted folder if Editly is installed.

## Repository compatibility

| Repository | Clip Desk use |
| --- | --- |
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | Optional VOD section import. |
| [OpenCut](https://github.com/OpenCut-app/OpenCut) | Manual editing handoff via MP4. Separate application. |
| [OpenCV](https://github.com/opencv/opencv) | Optional headless preprocessing of facecam samples before detection. |
| [OBS Studio](https://github.com/obsproject/obs-studio) | Local recordings enter through the existing file input. Separate application. |
| [AI YouTube Shorts Generator](https://github.com/Anil-matcha/AI-Youtube-Shorts-Generator) | Separate application. Clip Desk uses its own audience-spike, highlight and Umbrel review pipeline. |
| [Mediabunny](https://github.com/Vanilagy/mediabunny) | Separate browser SDK. Ready-made MP4 is exported by Clip Desk. |
| [Whisper](https://github.com/openai/whisper) | The worker uses the Whisper model through faster-whisper and CTranslate2. |
| [whisper.cpp](https://github.com/ggml-org/whisper.cpp) | Alternate runtime. Clip Desk currently uses faster-whisper. |
| [stable-ts](https://github.com/jianfch/stable-ts) | Optional future alignment stage. Word timestamps are available from faster-whisper. |
| [pyannote.audio](https://github.com/pyannote/pyannote-audio) | Standalone diarization. Clip Desk does not use a speaker model or label speakers automatically. |
| [Supervision](https://github.com/roboflow/supervision) | Standalone detection tooling. The worker uses UltraFace and a stable crop. |
| [Norfair](https://github.com/tryolabs/norfair) | Its current release requires NumPy 1.x. Clip Desk uses NumPy 2.x. |
| [DeepFilterNet](https://github.com/Rikorose/DeepFilterNet) | Its current Python package requires NumPy 1.x. The worker runs a high-pass filter, compressor and limiter in FFmpeg. |
| [Demucs](https://github.com/facebookresearch/demucs) | Separate stem separation with PyTorch. Clip Desk preserves the source mix. |
| [pysubs2](https://github.com/tkarabela/pysubs2) | Optional SRT and VTT export from the generated ASS track. |
| [libass](https://github.com/libass/libass) | FFmpeg's ASS filter renders styled captions. |
| [Editly](https://github.com/mifi/editly) | Clip Desk exports a JSON5 project that references its MP4. |
| [Kdenlive](https://github.com/KDE/kdenlive) | Manual timeline editor. Import the MP4 and any needed subtitle track. |
| [Shotcut](https://github.com/mltframework/shotcut) | Manual timeline editor. Import the MP4 and any needed subtitle track. |
| [Real-ESRGAN](https://github.com/xinntao/Real-ESRGAN) | Standalone upscaling. Each automated edit exports directly at 1080 × 1920. |
| [RIFE](https://github.com/hzwer/ECCV2022-RIFE) | Standalone interpolation. Each automated edit keeps the 30 fps export. |
| [PyAV](https://github.com/PyAV-Org/PyAV) | Facecam samples share one decoder session. FFmpeg handles unsupported files. |
