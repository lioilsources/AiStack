# spark-broker — fronta úloh SPARKu

SPARK má 121 GiB unifikované paměti a víc práce, než se do ní vejde naráz. Broker
přijímá úlohy od Claude sessions (a od uživatele přes Clauda), řadí je podle
priority, sám přepíná profily (co je nahoře) a po skončení úlohy hned připraví
prostředí pro další. **Pevná okna nejsou**: když uživatel chce ComfyUI, řekne si
o okno; jinak jede podle priority a prázdný čas vyplní backlog.

Proč (říjen 2026): Director session ručně odpovídala ostatním sessions „kdy smím
a jak“; noční skript se 23. 10. v 08:03 spustil přes ranní okno a shodil ComfyUI;
SPARK přes den hodiny stál; session, které došel usage limit, by SPARK blokovala
navždy. Plán a rozhodnutí: `PLAN-spark-broker.md` v kořeni repa.

## Pojmy

| | |
|---|---|
| **profil** | sada služeb, které se vejdou spolu: `comfy` (ComfyUI + flux + TTS), `llm` (qwen36 + flux + TTS), `gemma`, `director`, `gptoss`. Přepnutí = příkaz `up` profilu (většinou `rag-schedule.sh <režim>`, který zná pravidla z incidentů). |
| **doplněk** | služba přidaná k profilu na vyžádání (`audio-music`, `audio-sfx` ke comfy); broker ji shodí, když ji nikdo nechce. |
| **úloha** | `project`, `priority`, `stages[]`. Stage = služby, které musí běžet spolu + `mode`. |
| **lease** | broker jen připraví prostředí, session volá HTTP sama a posílá heartbeat; bez heartbeatu 10 min = sirotek, SPARK se uvolní. |
| **run** | příkaz spouští broker na SPARKu (`bash -lc`); log v `~/spark-broker/logs/`. Poslední JSON řádek výstupu = metriky do souhrnu. |
| **okno uživatele** | `on_behalf_of_user: true` + `user_quote` (doslovná slova uživatele): priorita 100, neodsouvá se, bez heartbeatu; ComfyUI okno se po 30 min nečinnosti připomene a po dalších 15 zavře. Každé se ohlásí na Telegramu i s citací, takže je vidět, kdo o něj jménem uživatele požádal. |

**Priorita**: 0 backlog · strop projektu (`projects` v konfiguraci, default 20) ·
90 PromoClown · 100 uživatel. Víc, než je strop projektu, si úloha sama nedá:
broker se zeptá na Telegramu a do schválení (`POST /v1/jobs/{id}/approve`, jen
na výslovný pokyn uživatele) jede se stropem.

**Plánovač** (každých 15 s a po každé události): nejvyšší priorita → při stejné
ta, která nepotřebuje přepnout (přepnutí stojí 2–10 min) → deadline → pořadí.
Úloha, která potřebuje jiný profil, přepne jen tehdy, když všechno, co běží, má
nižší prioritu a je `preemptible`: lease dostane 5 min na úklid a vrátí se do
fronty, run dostane SIGTERM (proto preemptible run musí mít `resume: true`).
Selhané přepnutí broker **pozastaví** a pošle ❌ — nic dalšího nezkouší, dokud
uživatel nepustí `POST /v1/resume`. Teplota GPU ≥ 87 °C = nic nového nestartuje.

**Telegram** (přes `notify.sh`, PromoClown bot): ✅ souhrn po každé úloze s
metrikami (backlog jednou denně v 07:00), ❌ hned při chybě s tím, co je potřeba
udělat, ❓ dotaz na prioritu nad strop, 🪟 okno jménem uživatele, 💤 nečinné okno.

## Režimy

- `BROKER_MODE=advise` (výchozí): nic nespouští ani nevypíná. Profil zjišťuje
  ze stroje (`detect`), lease je `running`, když potřebné služby zrovna běží.
  Běží vedle `rag-schedule.timer`; sessions si zvykají na API.
- `BROKER_MODE=control`: broker SPARK řídí. Odmítne start, dokud běží
  `rag-schedule.timer` — dva přepínače naráz jsou přesně chyba z 23. 10.

## API (`:8100`, LAN)

| | |
|---|---|
| `GET /v1/status` | profil, doplňky, paměť/teplota, běžící úlohy, fronta s důvodem čekání |
| `GET /v1/catalog` | profily, služby, doplňky, stropy priorit, pravidla |
| `POST /v1/jobs` | zařadit úlohu → `201` s `id` |
| `GET /v1/jobs?project=&all=1` · `GET /v1/jobs/{id}` | stav, `waiting` (proč ještě neběží), události |
| `PATCH /v1/jobs/{id}` | `{"priority":N}`, `{"deadline":…}`, `{"cancel":true}` |
| `POST /v1/jobs/{id}/heartbeat` · `/done` · `/extend?minutes=` · `/approve` | lease; `/done` bere `{"metrics":{…}}` nebo `{"error":"…"}` |
| `POST /v1/pause` · `/v1/resume` | nic nového nestartuje / zase jede |

```json
{"project": "storyteller", "session": "Director", "title": "noc p1", "priority": 30, "resume": true,
 "stages": [
   {"name": "vote", "services": ["director"], "mode": "run", "command": "~/jobs/vote.sh", "estimate_min": 90},
   {"name": "gen",  "services": ["gptoss"],   "mode": "run", "command": "~/jobs/gen.sh",  "estimate_min": 300}
 ]}
```

Klient pro sessions: `sparkctl` (Go, `cmd/sparkctl`, sdílí typy s brokerem), návod `sparkctl help`.
Id úlohy jde na stdout, průběh na stderr; chyba API = exit 1 (skript tak pozná třeba osiřelý lease),
špatné argumenty = exit 2. Přehled fronty pro lidi: `sparkctl ls [projekt]`.

```bash
sparkctl window comfyui 3h "chci comfy na 3 hodiny"     # okno jménem uživatele
id=$(sparkctl lease webtoon comfyui,audio-music 30 25 "hudba pod shorts")   # čeká na running
sparkctl hb $id                                          # aspoň každých 5 min
sparkctl done $id '{"sounds":{"ok":15,"failed":0}}'

# dávka na Macu bez obalového skriptu: lease, heartbeat, prodlužování, po odsunutí příkaz
# zastaví a po návratu pustí znovu; done/fail podle exit kódu, metriky z posledního řádku stdout
sparkctl exec storyteller qwen36 180 20 "zvuky p2" --dir ~/st/rag -- python -m rag.sounds cues
# navazování: ve frontě hned (vidět v ls jako „po 1024-15“), startuje až po skončení té úlohy
sparkctl run library director 10 "<příkaz>" "knihovna" --after 1024-15
# jednorázová akce na čas: ve frontě hned (v ls „naplánovaná na 19:05“), startuje v 19:05
sparkctl exec storyteller gptoss 600 20 "korektura" --at 19:05 -- ./gptoss-fix.sh
# lease pro jinou session: heartbeat drží hold, uvolní ho, až lokální skript doběhne
sparkctl hold 1024-15 --proc local_strip.sh --id-file ~/ops-director/webtoon/comfy.id
```

## Nasazení (SPARK, user systemd)

```bash
make -C services/spark-broker build            # bin/spark-broker + bin/sparkctl-{linux,darwin}-arm64
# binárky jen atomicky (kopie vedle + mv): bash čte skript za běhu a přepsání na místě
# 23. 10. shodilo čekající `sparkctl lease` (úloha osiřela)
cp bin/sparkctl-darwin-arm64 ~/.local/bin/sparkctl.new && mv ~/.local/bin/sparkctl.new ~/.local/bin/sparkctl
scp bin/sparkctl-linux-arm64 spark:spark-broker/sparkctl.new && ssh spark mv spark-broker/sparkctl.new spark-broker/sparkctl  # SPARK: symlink v ~/.local/bin
scp bin/spark-broker spark:spark-broker/        # + deploy/config.example.yaml → ~/spark-broker/config.yaml
scp deploy/profiles/gptoss-up.sh spark:spark-broker/profiles/
scp deploy/spark-broker.service spark:.config/systemd/user/
# ~/spark-broker/broker.env: BROKER_MODE=advise|control, BROKER_ADDR=:8100, volitelně BROKER_TOKEN
systemctl --user daemon-reload && systemctl --user enable --now spark-broker
```

Stav je v `~/spark-broker/state.json` (atomický zápis, přežije restart; běžící run
úlohy s `resume` jdou po restartu zpět do fronty, ostatní se hlásí jako chyba).

**Přechod na control**: `systemctl --user disable --now rag-schedule.timer`, v
`broker.env` `BROKER_MODE=control`, restart brokera. Zpět: obráceně. `rag-schedule.sh`
zůstává — broker ho volá jako provisioner profilů.

## Pasti

- `detect` příkazy musí být navzájem výlučné (pořadí `profile_order`); jinak
  advise hlásí špatný profil.
- Doplněk se startuje jen s rezervou `mem_gib + mem_reserve_gib` v MemAvailable.
- `recurring` je jen přechod pro PromoClown, dokud jeho heartbeat neumí `POST /v1/jobs`
  (od 26. 10. 2026 prázdné). Denně se opakující pevné okno z configu, priorita z configu,
  neodsouvá se; config se čte jen při startu, změna chce restart brokera.

## Akce na čas (`not_before`, `sparkctl --at`)

Jednorázová akce má přednost před dlouhými úlohami, které se jen na chvíli přeruší:

- Do `not_before` úloha jen čeká ve frontě („naplánovaná na 19:05“) a nic nepřepíná.
- Když čas nastane, jde ve frontě před všechno bez času a odsune každou **dlouhou** úlohu
  bez ohledu na prioritu. Dlouhá = `preemptible` a `resume`: `run` s resume a `exec`
  (sparkctl příkaz po odsunutí zastaví a po návratu pustí znovu). Po akci se vrátí sama.
- Lease bez resume (tribute, okno uživatele, ruční `lease`) by odsunutí zabilo, ne
  přerušilo — ty chrání dál priorita, resp. okno uživatele se neodsouvá vůbec.
- Běžící akci na čas dlouhá úloha zpátky neodsune, jinak by se přetahovaly.
- `--at`: `19:05` (dnes, už-li minulo, tak zítra), `"2026-10-27 06:30"`, RFC3339, `+90m`.
- Telegram příkazy (`/spark …`) nejsou: bot čte OpenClaw a `getUpdates` smí mít
  jen jednoho čtenáře. Uživatel řídí broker přes Clauda (`sparkctl`).
