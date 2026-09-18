# Jarvis — self-host image. Bundles the app, deps, and voice models so a VPS
# runs the full assistant (voice + coding) with one container.
#
#   docker build -f deploy/Dockerfile -t jarvis .
#   docker run -p 8000:8000 -p 7860:7860 --env-file prototype/.env jarvis
#
# Then open http://<server>:8000 (the WS bot is on :7860).
FROM python:3.12-slim

# espeak-ng: kokoro-onnx phonemizer.  libgomp1: onnxruntime.
RUN apt-get update && apt-get install -y --no-install-recommends \
      espeak-ng libgomp1 ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY prototype/requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir python-dotenv websockets

COPY prototype/ prototype/

# Bake the voice models so nothing downloads at runtime.
RUN mkdir -p models/kokoro models/moonshine \
    && K=https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0 \
    && curl -fL "$K/kokoro-v1.0.onnx" -o models/kokoro/kokoro-v1.0.onnx \
    && curl -fL "$K/voices-v1.0.bin" -o models/kokoro/voices-v1.0.bin \
    && MOONSHINE_VOICE_CACHE=/srv/models/moonshine \
       python -c "from moonshine_voice.download import get_model_for_language as g; g('en')"

ENV JARVIS_HOST=0.0.0.0 \
    WS_HOST=0.0.0.0 \
    KOKORO_MODEL_PATH=/srv/models/kokoro/kokoro-v1.0.onnx \
    KOKORO_VOICES_PATH=/srv/models/kokoro/voices-v1.0.bin \
    MOONSHINE_VOICE_CACHE=/srv/models/moonshine

EXPOSE 8000 7860
CMD ["python", "prototype/ui/launch.py", "--no-browser"]
