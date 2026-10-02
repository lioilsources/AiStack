"""TTS — převod textu na řeč (Kokoro, Piper, XTTS-v2, Chatterbox).

Orchestrátor jen vybírá engine a hlas, frontuje a dělá post-processing;
modely běží v kontejnerech audio-tts (CPU), audio-tts-xtts a
audio-tts-chatterbox (GPU).
"""
