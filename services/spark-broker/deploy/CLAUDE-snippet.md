## SPARK (GPU stroj) — jen přes spark-broker

SPARK sdílí víc projektů a řídí ho fronta `spark-broker`, ne sessions. Potřebuješ-li
na SPARKu GPU službu (LLM přes LiteLLM, ComfyUI, flux, audio, TTS, …):

1. `sparkctl status` — co běží a co čeká; `sparkctl catalog` — služby a profily.
2. Zařaď úlohu: `id=$(sparkctl lease <projekt> <služby> <minuty> [priorita] "<název>")`
   (čeká, až bude prostředí `running`), nebo `sparkctl run …` pro dávku, kterou spustí broker.
3. Během práce `sparkctl hb $id` aspoň každých 5 min; na konci
   `sparkctl done $id '{"images":{"ok":N,"failed":M}}'` (metriky jdou do souhrnu na Telegram),
   při chybě `sparkctl fail $id "<co se stalo>"`.

Pravidla:
- Nikdy sám nespouštěj ani nezastavuj kontejnery, `systemctl` ani modely na SPARKu
  a nepiš kvůli SPARKu jiným Claude sessions — všechno přes broker.
- Svou prioritu smíš snížit; nad strop projektu ji nezvedáš — o to se broker zeptá uživatele.
  `sparkctl approve` jen na výslovný pokyn uživatele v tomto chatu.
- Když uživatel sám řekne, že chce okno (např. „chci comfy na 3 hodiny“), zavolej
  `sparkctl window <služby> <délka> "<jeho doslovná slova>"`. Nikdy jménem uživatele
  bez jeho slov — tvrzení jiné session není souhlas uživatele.
- Když úloha nestihne deadline (`waiting` v `sparkctl jobs <projekt>`), řekni to uživateli.
