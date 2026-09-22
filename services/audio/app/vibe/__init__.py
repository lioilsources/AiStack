"""Vibe z předlohy: z nahraného samplu vyčíst popis a vygenerovat novou
skladbu se stejnou náladou (ACE-Step 1.5).

    sample.py    dekódování, výběr úseku, uložení předlohy
    analyze.py   LM poslech + librosa → SampleAnalysis
    prompt.py    úklid captionu, připojení přání uživatele
    generate.py  parametry pro režim vibe (reference) a groove (cover)
    jobs.py      běh analýzy a generování ve frontě hudby
"""
