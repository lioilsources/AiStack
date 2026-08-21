# UGC smoke test — 5 kategorií end-to-end (2026-08-21)

Prompt → koncept (Illustrious-XL-v2.0) → RMBG cleanplate → TRELLIS.2 GLB →
NAS (ugc-api) → Blender konverze → verdikt. Backend jen **trellis** — SF3D
čeká na odklik licence na HF. Kategorie „boty" nahrazena „back" (křídla):
boty jsou layered clothing = v2 dle UGC_FORGE_PLAN.

## Výsledky

| Kategorie | Čas gen. | Koncept | Konverze | Poznámka |
|---|---|---|---|---|
| helmet | 248 s | ★★★ prázdná kabuto | PASS 3600 tris | vzorová kategorie |
| sword | 655 s | ★★★ katana | PASS 3600 tris | diagonální kompozice → po fitu do bbox [1.2×4.2×1.2] tenká; zvážit rotaci dlouhé osy na výšku v convertu |
| hair | 253 s | ✗ **kvádr, ne vlasy** | PASS 3600 tris | geometrie prošla, ale item je nepoužitelný — „wig, no humans" Illustrious neumí; vlasy bez hlavy nedávají trénovacím datům smysl |
| back | 137 s | ★★ mechanická křídla | PASS 3598 tris | v pořádku |
| front | 236 s | ★ ořezaný detail plátu | **FAIL 4248 tris** | ořez → 718 komponent po remeshi → decimate dno nad limitem; verdikt správný, oprava = šablona |

Časy jsou per-item (serializovaná fronta, model držený v paměti; první běh
+~2 min na load). GPU: špička 96 % SM, průměr 32 % včetně čekání fronty,
žádný power-spike pád — serializace jedním workerem funguje.

## Ponaučení

1. **PASS geometrie ≠ dobrý item.** Hair prošel validací s krásným kvádrem.
   Vizuální triage konceptů (appka) je nenahraditelná — automatika chytá
   jen měřitelné vady.
2. **Illustrious mluví danbooru tagy.** „no character" → busta; „empty item,
   floating" → celý nindža; `no humans, still life, object focus` → čistý
   item. Šablony promptů jsou danbooru, ne angličtina.
3. **Ořezaný koncept = fragmentovaný mesh.** Front byl close-up přes celý
   rám → TRELLIS vyrobil 718 komponent → collapse decimate má per-komponentu
   dno a limit nejde stihnout. Do šablony: „wide shot, entire object
   visible". FAIL验 = validace funguje.
4. Konverzní opravy vzešlé z testu (commitnuté v ugc-backend): triangulace
   před decimací (remesh vrací quady, limit je v trojúhelnících), mazání
   floaterů s eskalací prahu, endpoint `/jobs/{id}/reconvert`.

## Doporučení

- **Default backend: trellis** pro všechny kategorie (jediný ověřený).
  SF3D po odemknutí porovnat na helmetu (nejstabilnější kategorie).
- Šablony: front → přidat wide-shot framing; hair → v1 vyřadit nebo
  experiment s „wig on mannequin stand" (riziko: mannequin v meshi);
  sword → zvážit vertikální reorientaci při fitu.
- Kategorie hotové k produkci: helmet, back, sword. Podmíněně front
  (po šabloně). Hair nechat na iteraci s appkou.
