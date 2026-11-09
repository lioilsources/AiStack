# vLLM + audio dekodéry pro Whisper (/v1/audio/transcriptions). Obraz vllm-openai na SPARKu
# nemá librosa/soundfile/av, bez nich vrací každý soubor 400 "Invalid or unsupported audio file".
# numpy a spol. se pinují na verze z obrazu, ať instalace nerozbije vLLM.
ARG SWARM_VLLM_VERSION
FROM vllm/vllm-openai:${SWARM_VLLM_VERSION}
RUN pip freeze | grep -iE '^(numpy|scipy|numba|llvmlite)==' > /tmp/constraints.txt \
 && pip install --no-cache-dir -c /tmp/constraints.txt librosa soundfile av \
 && rm /tmp/constraints.txt
