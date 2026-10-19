# PLAN-image-api.md — veřejné API pro ComfyUI a WD14 na SPARKu

Stav 2026-10-19. Podnět: Webtoon backend má běžet mimo domácí síť (mobilní aplikace), ale dnes volá ComfyUI
(`192.168.88.66:8188`) a WD14 (`:8097`) přímo po LAN. `comfyui.ol1n.com` je syrové ComfyUI za CF Access — pro
aplikaci nevhodné (viz §1).

---

## 1. Proč ne rovnou `comfyui.ol1n.com`

| Problém | Dopad |
|---|---|
| Syrové ComfyUI API (`/prompt`, `/history`, `/view`, `/upload`) bez fronty | každý klient si hlídá „max 1 job“ sám; dva projekty naráz = dva joby v ComfyUI |
| Nezná okna plánovače | klient mimo comfy profil dostane connection refused nebo, hůř, render na CPU (incident 29. 9.) |
| `/interrupt`, `/free`, `/queue` DELETE jsou dostupné každému s tokenem | jeden klient může zrušit cizí job |
| Dlouhý `/history` polling přes CF | funguje, ale každý klient to implementuje znovu |
| WD14 (`:8097`) nemá veřejnou adresu vůbec | Webtoon mimo LAN nemůže kontrolovat panely |

LLM problém nemá: `llm.ol1n.com/v1` (gateway → LiteLLM, `openclaw-default`) už veřejně běží se service tokenem.

## 2. Návrh: `/v1/image/*` v gen-queue

Ne nová služba — **rozšířit `services/gen-queue`**. Ten už má ComfyUI klienta (`internal/ugc/comfy.go`: upload,
run, history, view), host bridge (`UGC_COMFY_URL=http://host.docker.internal:8188`), sériového workera
a Cloudflare routu. Přibude třetí backend vedle `flux-kontext` / `flux-schnell`.

Routa v `cloudflared/config.yml` (před obecným `llm.ol1n.com`): `path: /v1/image/.*` → `gen-queue:8091`.
Auth stejný jako dnes: CF Access service token (`CF-Access-Client-Id` / `-Secret`) + hlavička `X-Project`
(webtoon, storyteller, promoclown…) pro přehled a férovost fronty.

### Endpointy

| Metoda | Cesta | Co dělá |
|---|---|---|
| `POST` | `/v1/image/jobs` | ComfyUI workflow (API JSON) + vstupní obrázky (base64 nebo multipart) + `client_ref` (idempotence) → `202 {id, position, window}` |
| `GET` | `/v1/image/jobs/{id}` | `queued \| waiting_window \| running \| done \| error`, pozice, průběh (krok/kroků z WS), chyba ComfyUI |
| `GET` | `/v1/image/jobs/{id}/outputs/{n}` | PNG výstupu n |
| `DELETE` | `/v1/image/jobs/{id}` | zrušení **vlastního** jobu; běžící → `/interrupt` jen pokud je to on |
| `POST` | `/v1/image/tag` | WD14 synchronně (PNG → `{ratings, tags}`), stejný tvar jako dnešní `:8097/tag` |
| `GET` | `/v1/image/status` | okno (otevřeno / další start), délka fronty, ComfyUI nahoře, teploty |
| `GET` | `/v1/image/models` | checkpointy, LoRA a VAE z `/object_info` (klient ověří presety před submitem) |

### Chování

- **1 worker, globálně.** Pravidlo „max 1 ComfyUI job“ platí pro všechny projekty; fronta je FIFO s prioritou
  podle `X-Project` (pořadí z PLAN-spark-scheduler §1).
- **Okno místo chyby.** Mimo comfy profil job čeká ve stavu `waiting_window` (nevrací 503). Stav okna bere
  z plánovače (Director / `rag-schedule` — stav profilu), ne z natvrdo zapsaných 07–13.
- **Perzistence.** Joby v SQLite na volume (dnešní gen-queue je jen in-memory — restart ztratí frontu).
  Výstupy na disk `$IMAGE_OUT/<project>/<yyyy-mm-dd>/<id>_<n>.png`, **nic se automaticky nemaže**
  (pravidlo: výstupy na SPARKu nemazat). Status jobů si drží TTL 7 dní, soubory zůstávají.
- **Pojistky z benchmarku** ve workeru: pauza nad GPU 87 °C / zóna 98 °C, detekce CPU renderu
  (`/system_stats` bez CUDA zařízení → job zpět do fronty, ne render).
- **Retry:** 5xx a výpadek ComfyUI → zpět do fronty s odkladem; chyba validace workflow (4xx) → `error` hned.
- **Limity:** workflow ≤ 1 MB, vstupní obrázky ≤ 20 MB, výstup ≤ 64 MB; rozlišení > 2048 px odmítnout.

### WD14

Tagger dnes běží na hostu (`:8097`, mimo tento repo). Varianta A: gen-queue ho jen proxuje (`/v1/image/tag`
→ `host.docker.internal:8097/tag`). Varianta B: přesunout do kontejneru vedle gen-queue. Doporučuji **A** —
je malý, běží pořád a nevadí oknům.

## 3. Fáze

| Fáze | Co | Kde | Riziko |
|---|---|---|---|
| 0 | Odsouhlasit s Directorem: zdroj stavu okna, priority projektů | Director | — |
| 1 | `internal/comfyq` v gen-queue: SQLite fronta, worker, endpointy jobs + outputs, testy s fake ComfyUI | AiStack | nízké, jen přidává |
| 2 | `/v1/image/tag` proxy, `/status`, `/models` | AiStack | nízké |
| 3 | cloudflared routa `/v1/image/.*`, `make up` restart cloudflared + gen-queue, smoke z M2 | AiStack | střední: restart tunelu krátce shodí `llm.ol1n.com` |
| 4 | Webtoon: klient `imageq` se stejným rozhraním jako `internal/comfy` + `internal/wd14`, přepínač `WEBTOON_IMAGE_API`; CF Access hlavičky i pro LLM klienta | Webtoon | nízké, LAN cesta zůstane |
| 5 | Ostatní (StoryTeller, PromoClown tributy, UGC) mohou přejít postupně; `comfyui.ol1n.com` zůstává pro interaktivní UI | — | — |

Odhad: fáze 1–3 zhruba den práce, fáze 4 půl dne.

## 4. Co tím Webtoon získá

Backend pak potřebuje jen `https://llm.ol1n.com` (LLM i obrázky) a service token — může běžet kdekoli
(`webtoon.ol1n.com`, VPS), mobilní aplikace míří na něj. Joby zadané mimo okno se samy dokreslí,
až se comfy profil otevře; dnešní backoff 5 min ve Webtoonu zůstane jen jako pojistka.

## 5. Rozhodnutí pro uživatele

1. Rozšířit gen-queue (doporučeno), nebo samostatná služba `image-queue`?
2. Kdo implementuje AiStack část: Director session, nebo tahle session s výjimkou z pravidla „necommitovat do AiStack“?
3. Fronta mimo okno čeká (doporučeno), nebo vrací 503 s `Retry-After`?
4. Volný workflow JSON (doporučeno, Webtoon ho už staví), nebo jen pojmenované šablony na serveru?
5. WD14: proxy na hostovský `:8097` (doporučeno), nebo přesun do kontejneru?

## 6. Rozhodnutí (Director, 2026-10-19)

1. **Rozšířit gen-queue** (`internal/comfyq`), ne nová služba.
2. AiStack část staví Director session, Webtoon jen klienta (fáze 4).
3. **Mimo okno se čeká** (`waiting_window`), žádná 503.
4. **Volný workflow JSON** v API formátu ComfyUI. UI export (`nodes`/`links`) se odmítne s hláškou.
5. **WD14 jen proxy** na hostovský `:8097`.

**Zdroj stavu okna (fáze 0):** běžící ComfyUI. `rag-schedule.sh` spouští unit `comfyui` jen v profilu comfy a v ostatních ho
zastavuje (i při ručním přepnutí), takže „ComfyUI odpovídá a vidí CUDA“ = okno otevřené. `IMAGE_SCHEDULE` (stejný řetězec jako
`SCHEDULE` v drop-inu) slouží jen k uzávěrce 5 min před plánovaným přepnutím a k údaji `next_open`. Žádný stavový soubor
plánovače není potřeba.

**Brána před každým jobem:** ComfyUI nahoře → CUDA zařízení (jinak `cpu`) → uzávěrka → teploty z `~/ops/thermal.csv`
(GPU ≥ 87 °C nebo zóna ≥ 98 °C = `hot`; řádek starší 2 min se ignoruje) → prázdná fronta ComfyUI (LAN klienti dál chodí
napřímo, takže cizí práce = `busy` a čeká se). Tím platí „max 1 ComfyUI job“ i vůči přímým klientům.

**Priority projektů:** `IMAGE_PRIORITY=storyteller,webtoon,promoclown,kirian,ugc` (StoryTeller má podle uživatele přednost),
neznámý projekt na konci, uvnitř priority FIFO.

**Odchylky od §2:** fronta je JSON soubor na job (`/data/jobs/<id>.json`), ne SQLite: image se staví s `CGO_ENABLED=0`
a čistě gocový SQLite by přidal velkou závislost. Restart frontu neztratí (běžící job jde zpět do fronty). Vstupy jen jako
base64 v JSON, multipart zatím ne. Průběh po krocích (WS) zatím ne, stav je `running`. Status jobu se nemaže vůbec.

**Retry:** 4xx / `node_errors` / execution error → `error` hned. Výpadek ComfyUI během běhu (> 90 s), přerušení cizím
`/interrupt` (pojistka plánovače při přepnutí) a 5xx → zpět do fronty s odkladem 30 s / 2 min / 5 min, max 5 pokusů.
Timeout jobu 60 min.

**Fáze 3** (restart tunelu): cloudflared běží ve dvou replikách, restartují se po jedné, `llm.ol1n.com` tak nemusí spadnout.
