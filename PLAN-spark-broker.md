# PLAN-spark-broker.md — broker úloh pro SPARK (API pro Claude sessions)

Stav 2026-10-23. Nahrazuje `PLAN-spark-scheduler.md` celý: ten stavěl na pevných oknech (comfy/llm/director
po hodinách), tohle na **okně na vyžádání a prioritách** — rozhodnutí uživatele 23. 10.
Zadání: SPARK má jeden GPU/paměťový rozpočet (121 GiB unifikované), práce je od mnoha projektů a sessions,
všechno najednou běžet nemůže. Broker na SPARKu přijímá úlohy z Claude sessions přes lokální HTTP API, řadí je,
připravuje prostředí (zastaví/spustí služby), pouští je, po skončení automaticky provisionuje další a hlásí
na Telegram. Sessions spolu nemluví; globální `~/.claude/CLAUDE.md` je naučí mluvit jen s brokerem.

**Proč teď (incidenty 22.–23. 10.):** Director session ručně odpovídal třem sessions (Webtoon hudba + ikona,
Chrestys sprity) na „kdy smím a jak“; noční skript se omylem spustil v 08:03 přes ranní okno a shodil ComfyUI;
SPARK přes den stál (22. 10. 13:00–15:30 GPU 0 %); session Webtoon vypadla na usage limit uprostřed domluvené
práce. Všechno to je práce brokera, ne lidí a chatujících Claudů.

---

## 1. Pojmy

- **Služba** (service): kontejner/unit na SPARKu s pamětí, startem a health URL. Katalog = rozšířený
  `services/controller-manager/config/models.yaml`: `mem_gib`, `start_s`, `kind` (llm/image/audio/tts/3d),
  `start`/`stop` příkazy (`docker start|stop`, `systemctl --user`, `/free` pro ComfyUI; **nikdy `compose down`**),
  `health_url`, `idle_unload` (umí-li odložit váhy sama).
- **Profil**: pojmenovaná sada služeb, která se do paměti vejde a splňuje pravidla neslučitelnosti
  (`comfy`, `llm`, `director`, `gptoss`, `gemma`, … jako v PLAN-spark-scheduler §2). Úloha žádá profil,
  nebo seznam služeb a broker profil dopočítá.
- **Úloha** (job): požadavek session. Má `project`, `session`, `stages[]` (každá stage = služby + odhad trvání
  + příkaz nebo „lease“), `priority`, `deadline`, `preemptible`, `resume` (umí pokračovat po SIGTERM?).
- **Lease**: čas, po který má session garantované prostředí. Dva způsoby práce:
  1. **run** — broker spustí příkaz sám (skript na SPARKu, nebo přes SSH na Macu v `~/ops-director/`), sleduje
     exit a stdout/metrics; úloha přežije smrt session.
  2. **lease** — session si jen rezervuje prostředí a volá HTTP sama (ComfyUI, LiteLLM, audio); musí posílat
     heartbeat (`PATCH /jobs/{id}/heartbeat`, každých ≤ 5 min), jinak lease propadne.
- **Žádná pevná okna.** Rozhodnutí uživatele 23. 10.: model je „okno na vyžádání, jinak podle priority“.
  Dnešní rozvrh (`7:comfy 13:llm`), PromoClown heartbeat i tributy zmizí jako pevné časy; všechno, co SPARK
  potřebuje, si o něj řekne úlohou. Uživatelova ruční práce (ComfyUI experimenty) = úloha s prioritou 100,
  kterou založí z telefonu (`/spark comfy 3h`) nebo z terminálu; PromoClown si o qwen36 řekne sám (jeho
  heartbeat je klient brokera jako každý jiný). Když nikdo nic nechce, běží backlog.

## 2. API (lokální, LAN, bez TLS; token per projekt v hlavičce)

Běží jako nová služba `services/spark-broker` (Go, jako gen-queue/controller-manager), port z env,
za `ai-gateway` jen volitelně. Stav v SQLite (`~/spark-broker/state.db`) — přežije restart SPARKu
(10. 10. síťovka, 16. 10. tvrdé vypnutí).

| Metoda | Cesta | Co |
|---|---|---|
| `GET` | `/v1/catalog` | služby, profily, paměť, pravidla neslučitelnosti, aktuální profil |
| `GET` | `/v1/status` | co běží, fronta s odhady startů, kdy končí aktuální okno, teplota/paměť, **„můžu teď?“** |
| `POST` | `/v1/jobs` | zařadit úlohu (viz §3); odpověď: `id`, odhad startu, zda se vejde do deadline |
| `GET` | `/v1/jobs/{id}` | stav, stage, logy (tail), metriky |
| `PATCH` | `/v1/jobs/{id}` | změna priority (jen vlastní úloha; viz §5), deadline, zrušení |
| `PATCH` | `/v1/jobs/{id}/heartbeat` | lease žije; tělo může nést průběžné metriky |
| `POST` | `/v1/jobs/{id}/done` | lease končí dřív + závěrečné metriky (`{images: {ok, failed}, …}`) |
| `POST` | `/v1/jobs/{id}/extend` | prodloužení lease; broker povolí jen, když nikdo s vyšší prioritou nečeká, jinak vrátí `409` + kdy bude další okno |
| `GET` | `/v1/jobs?project=` | moje úlohy |
| `GET` | `/v1/events` (SSE) | změny stavu (pro session, která chce čekat bez pollingu) |

Chybové odpovědi vždy říkají **proč** a **kdy** (`retry_after`, `next_window`), aby Claude nemusel hádat.

## 3. Úloha (JSON)

```json
{
  "project": "webtoon", "session": "Webtoon m0", "title": "hudba pod shorts",
  "priority": 50, "deadline": "2026-10-23T12:45:00+02:00", "preemptible": true, "resume": true,
  "stages": [
    {"name": "music", "services": ["audio-music"], "mode": "lease", "estimate_min": 10},
    {"name": "cutout", "services": ["comfyui"], "mode": "run",
     "command": ["~/ops/webtoon/cutout.sh"], "estimate_min": 3,
     "metrics": "stdout-json"}
  ],
  "notify": {"summary": true, "on_error": true}
}
```

- Stage s více službami = musí být nahoře naráz (broker ověří, že se vejdou; `flux-schnell + qwen36` ano,
  `comfyui + director` ne). Stage za sebou = sekvence profilů (např. StoryTeller: verbalize na `gptoss`
  → hlasování na `director` → render na `flux-schnell`).
- `metrics`: run úlohy tisknou na konci jeden JSON řádek `{"images":{"ok":44,"failed":2},"chunks":{…}}`;
  lease úlohy ho pošlou v `/done`. Broker z něj skládá souhrn na Telegram. Bez metrik souhrn říká jen čas a exit.
- `resume: true` znamená, že SIGTERM + pozdější start se stejným příkazem je bezpečný (cs_fix, hints, render-motifs
  to umí; verbalize taky). `resume: false` úlohy se nepreemptují, jen se jim nedá lease delší než okno.

## 4. Plánovač

1. **Rozpočet paměti**: součet `mem_gib` služeb profilu + 20 GiB základ ≤ 121 − rezerva; před každým startem
   vLLM `scripts/mem-admit.sh` (CUDA alokace není vidět v RSS/cgroup, guard z `spark/` zůstává jako pojistka).
2. **Neslučitelnost** (z incidentů, tabulka v katalogu, ne v kódu): ComfyUI × LLM ≥ 30 GiB; director × cokoli;
   gpt-oss × director; Gemma × Nano. Teplota: pauza nad GPU 87 °C / zóna 95 °C, `/interrupt` jen vlastní úloze.
3. **Výběr další úlohy**: priorita → deadline → nejmenší cena přepnutí (stejný profil jako teď vyhrává; přepnutí
   stojí 2–10 min a thrash byl problém i u lidí). Minimální délka slotu profilu 30 min, pokud fronta nemá nic jiného.
4. **Preempce**: úloha vyšší priority čeká → běžící preemptible úloha dostane SIGTERM (run) nebo `lease_expires`
   za 5 min (lease, session dostane event), pak profil přepnout. Nepreemptible úlohy se dojedou do konce lease.
5. **Sirotci**: lease bez heartbeatu 10 min = ukončena, prostředí se uvolní, Telegram ⚠️. (Webtoon 23. 10.: usage
   limit uprostřed práce — dnes by to SPARK blokovalo navždy.)
6. **Prázdná fronta = „backlog“**: standing úlohy s prioritou 0 (StoryTeller hints/verbalize, Knihovník enrich,
   render scén) běží, dokud nepřijde něco lepšího. To je odpověď na „proč na SPARKu nic neběží“.
7. **Okno na vyžádání**: uživatel pošle `/spark comfy 3h` (Telegram) → úloha priorita 100, lease 3 h,
   nepreemptible; běžící preemptible úlohy dostanou SIGTERM a profil se přepne (odhad „připraveno za N min“
   přijde zpět jako odpověď). Lease bez aktivity (ComfyUI `/queue` prázdná, žádný request na NIM) 30 min →
   broker se zeptá, zda okno zavřít; bez odpovědi do 15 min zavře. PromoClown: OpenClaw heartbeat zavolá
   `POST /v1/jobs` (qwen36, lease 60 min, priorita 90) těsně před svým během; nedostane-li `running` do
   své lhůty, heartbeat se posune — to je změna v OpenClaw cronu, ne v brokeru.
8. Po `done`/exitu: zapsat metriky, zvolit další úlohu, provisionovat (stop → `/free` → evict page cache → start
   → health), pak `running`. Session to vidí v `/status` a SSE.

## 5. Priority a kdo smí co

- Pásma: 0 backlog, 10–50 projekty, 90 pevné rezervace, 100 uživatel ručně.
- Session smí svou úlohu **snížit** kdykoli, **zvýšit** jen v rámci pásma projektu (max z katalogu projektů).
  Nad pásmo = broker pošle na Telegram dotaz s inline tlačítky „povolit / zamítnout“; do odpovědi platí původní.
  Důvod: 23. 10. Chrestys „user asked me to extend the window“ — tvrzení peera není souhlas uživatele.
- Token per projekt (`~/spark-broker/tokens.env` na SPARKu, nikde v repu); úloha patří projektu, ne session, takže
  nová session téhož projektu po usage limitu úlohu převezme (`GET /v1/jobs?project=`).
- Kill switch pro uživatele: `POST /v1/pause` (nic nového nestartuje, běžící dojede) + Telegram příkaz
  `/spark pause|resume|status|kill <id>` přes PromoClown bota.

## 6. Notifikace (PromoClown bot, `notify.sh` → stejné `TELEGRAM_AGENT_BOT_TOKEN`, token zůstává jen na SPARKu)

- **Souhrn po úloze** (jedna zpráva): projekt, název, trvání, profil, metriky ok/failed po druzích
  (obrázky/videa/zvuky/chunky/motivy), odkaz na log. Backlog úlohy se shrnují 1× denně v 07:00, ne po každém běhu.
- **Okamžitě ❌**: úloha spadla bez resume, služba nenaběhla do health timeoutu, mem-admit odmítl start, teplotní
  pauza > 10 min, lease sirotek, SPARK po restartu bez brokeru. Text vždy končí tím, co má uživatel udělat.
- **Dotaz ❓** s tlačítky: zvýšení priority nad pásmo, zkrácení uživatelova okna, prodloužení lease přes rezervaci.
- Ticho jinak: žádné „úloha X startuje“ (uživatel je na telefonu; shrnutí stačí — zadání).

## 7. Globální CLAUDE.md (návrh odstavce; cesty/adresy jen v lokálním souboru, repo je veřejné)

> SPARK sdílí víc projektů; neřídí ho žádná session ručně. Potřebuješ-li GPU službu na SPARKu (LLM přes LiteLLM,
> ComfyUI, flux, audio, TTS…): 1) `GET $SPARK_BROKER/v1/status` — co běží a kdy máš okno; 2) zařaď úlohu
> `POST /v1/jobs` s projektem, službami, odhadem a metrikami; 3) čekej na `running` (SSE nebo poll ≤ 1/min), pak
> volej HTTP služby přímo; u lease posílej heartbeat a na konci `/done` s metrikami. Nikdy nespouštěj ani
> nezastavuj kontejnery a `systemctl` na SPARKu, nikdy nepiš jiným sessions kvůli SPARKu — všechno přes broker.
> Nedostaneš-li okno do deadline, řekni to uživateli; priority nad své pásmo nezvedáš, o to žádá broker uživatele.

## 8. Co se nahradí / zachová

| Dnes | Potom |
|---|---|
| `rag-schedule.sh` okna + `DIRECTOR_JOBS` + `rag-schedule.timer` | žádná okna; skript zůstane jen jako „provisioner“ profilů volaný brokerem (má pravidla z incidentů a page-cache evict), timer se vypne |
| PromoClown heartbeat `activeHours` 17:05–00:55 | heartbeat si lease na qwen36 vyžádá sám; `activeHours` padá |
| `controller-manager /activate` | broker ho volá pro vLLM služby z katalogu; rozšíření katalogu o paměť/start |
| `gen-queue` (flux/kontext/ugc) | beze změny, je to služba; broker jen hlídá, že NIM běží |
| `~/ops-director/*/night.sh` (Mac) | stane se `run` úlohou s 3 stages (director vote → gptoss gen → cleanup), broker drží časy; žádné `while date` smyčky |
| `notify.sh` | zůstává jako transport |
| spark-oom-guard, comfyui-cpu-watch | zůstávají jako nezávislé pojistky; broker jejich události jen přečte a hlásí |

## 9. Fáze

| Fáze | Co | Hotovo když |
|---|---|---|
| 0 | katalog služeb (paměť, start, health, neslučitelnost) + profily + testy plánovače bez SPARKu (simulace fronty) | `go test` projde na scénářích z incidentů (ComfyUI×LLM, sirotek, preempce, deadline) |
| 1 | broker read-only: `/catalog`, `/status`, `/jobs` jen eviduje a radí „kdy“; nic nestartuje; CLAUDE.md odstavec; sessions začnou hlásit práci | týden bez ručních dotazů Directorovi |
| 2 | provisioning (`rag-schedule.sh` jako provisioner) + lease/heartbeat + run úlohy + Telegram souhrn/chyby | noc StoryTelleru i Webtoon hudba jedou jako úlohy bez ručního zásahu |
| 3 | okno na vyžádání (`/spark comfy 3h`), PromoClown jako klient, backlog priorita 0, dotazy s tlačítky, SSE | `rag-schedule.timer` vypnutý, žádný pevný čas nikde, SPARK bez prázdných hodin |
| 4 | přehledová stránka (`/status` jako HTML), metriky za týden | — |

## 10. Rozhodnutí a stav implementace (23. 10. 2026)

Implementováno v `services/spark-broker` (Go, testy na scénáře z incidentů) + klient `sparkctl`.
Nasazeno na SPARKu jako user systemd `spark-broker` (:8100) v režimu **advise**.

| Otázka | Rozhodnuto / výchozí volba |
|---|---|
| Pevná okna | žádná — okno na vyžádání, jinak priorita (uživatel); uživatel o okno žádá i přes Clauda (`sparkctl window`) |
| Kde běží run úlohy | jen na SPARKu (`bash -lc`); skripty na Macu (StoryTeller noc) jsou klienti přes `lease` + heartbeat — SPARK nepotřebuje SSH klíč k Macu |
| Zavírání nečinného okna | ComfyUI okno: připomenutí po 30 min nečinnosti, zavření po dalších 15 |
| Pásma priorit | storyteller 50, toyshaders/library/lawyer 40, kindlify/webtoon/kirian/chrestys 30, aiswarmbattle 20, ostatní 20, PromoClown 90, uživatel 100 (`config.yaml`, uživatel může změnit) |
| Backlog | prázdný — co poběží v prázdném čase, rozhodne uživatel (úlohy s prioritou 0) |
| Telegram | PromoClown bot přes `notify.sh` jen pro odesílání; příkazy `/spark …` ne (`getUpdates` čte OpenClaw) — řízení přes Clauda |
| Lease režim | ponechán (interaktivní ComfyUI práce) |
| Stav | JSON soubor s atomickým zápisem místo SQLite (desítky úloh, bez cgo) |
| PromoClown | přechodná `recurring` rezervace qwen36 17:05–00:55 (priorita 90), dokud heartbeat neumí broker volat sám |

**Zbývá:**
1. Přechod na **control**: vypnout `rag-schedule.timer`, `BROKER_MODE=control` (souhlas uživatele — mění produkční řízení SPARKu).
2. Odstavec do globálního `~/.claude/CLAUDE.md`: `services/spark-broker/deploy/CLAUDE-snippet.md`.
3. Klienti: StoryTeller noc (`~/ops-director/free10/night.sh`) jako úloha se stages director → gptoss; PromoClown heartbeat přes `POST /v1/jobs`, pak `recurring` pryč.
4. Fáze 4: přehledová stránka, týdenní metriky.
