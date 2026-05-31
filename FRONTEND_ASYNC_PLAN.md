# Frontend plán — async image generation (Flutter)

Handoff pro frontend Clauda. Backend (image-api) byl přepsán ze synchronního na
**asynchronní job + poll** model. Tenhle dokument popisuje, co se změnilo a co je
potřeba upravit ve Flutter appce.

## Proč se to mění

Generování obrázku trvá ~1–3 min. To je déle než 100s proxy timeout Cloudflare
(klient dostával **524**) a držet tak dlouhé spojení z mobilu je stejně křehké
(přepínání sítě, uspání appky na pozadí). Nově se request nezdržuje: `POST`
**založí job a hned vrátí `job_id`**, appka pak **polluje stav** dokud není hotovo.

> ⚠️ **Breaking change.** Starý `POST /v1/images/generations` vracel rovnou
> obrázek (`200` + `data:[{b64_json}]`). Nově vrací `202` + `job_id`. Staré buildy
> appky přestanou fungovat — je nutný nový build s polling logikou.

## Base URL

`https://llm.ol1n.com` (beze změny). Endpointy jsou pod `/v1/images/`.

## API kontrakt

### 1) Založení jobu — generování (text → obrázek)

```
POST /v1/images/generations
Content-Type: application/json

{
  "prompt": "a red fox in the snow",
  "n": 1,                      // 1–4, volitelné (default 1)
  "size": "1024x1024",         // "WxH", volitelné
  "quality": "standard",       // "standard" | "hd"  (hd = víc kroků, pomalejší)
  "num_inference_steps": null  // volitelné, přebije quality
}
```

Odpověď **`202 Accepted`**:

```json
{
  "id": "8f3c1a9b...",
  "status": "queued",
  "status_url": "/v1/images/jobs/8f3c1a9b...",
  "queue_position": 0
}
```

### 2) Založení jobu — úprava (obrázek → obrázek)

```
POST /v1/images/edits
{
  "image": "<base64 PNG/JPEG>",   // povinné
  "prompt": "make the sky purple",
  "n": 1,
  "size": "1024x1024"
}
```

Odpověď je stejná `202` jako u generování. Při neplatném base64 → `400`.

### 3) Polling stavu

```
GET /v1/images/jobs/{id}
```

Vrací `200` v jednom ze 4 stavů:

```jsonc
// fronta — čeká na GPU
{ "id": "...", "status": "queued",  "step": 0,  "total": 14, "created": 1748..., "queue_position": 1 }

// běží — step/total = průběh difúze (pro progress bar)
{ "id": "...", "status": "running", "step": 7,  "total": 14, "created": 1748... }

// hotovo — obrázky jako base64 PNG
{ "id": "...", "status": "done",    "step": 14, "total": 14, "created": 1748...,
  "data": [ { "b64_json": "iVBORw0KGgo..." } ] }

// chyba
{ "id": "...", "status": "error",   "step": 3,  "total": 14, "created": 1748...,
  "error": "RuntimeError: CUDA out of memory" }
```

`404` = neznámé nebo expirované `id` (hotové joby se drží **1 h**, pak se uklidí —
viz `JOB_TTL`). Hotový obrázek je tedy potřeba **vyzvednout do hodiny**.

## Co implementovat ve Flutteru

### Tok

1. **Submit:** `POST` → z odpovědi ulož `id` (resp. `status_url`).
2. **Persistuj `id`** (např. `shared_preferences` / lokální DB) hned, **než**
   začneš pollovat — aby generování přežilo zavření/uspání appky. Po návratu do
   appky obnov polling podle uloženého `id`.
3. **Poll** `GET /v1/images/jobs/{id}` po ~**2 s** (klidně backoff na 3–5 s):
   - `queued` → ukaž „ve frontě (pozice N)".
   - `running` → progress `step / total` (např. „krok 7/14" nebo progress bar
     `step/total`).
   - `done` → dekóduj `data[0].b64_json` (base64 → bytes → `Image.memory`),
     zobraz, smaž uložené `id`.
   - `error` → ukaž `error`, smaž uložené `id`.
   - `404` → job expiroval/zmizel → nabídni „vygenerovat znovu".
4. **Timeout/limit:** pokud `running` nepostoupí např. 5 min, nabídni cancel
   (klientsky — viz pozn. níže).

### Náčrt (Dart)

```dart
const base = 'https://llm.ol1n.com';

Future<String> submitGeneration(String prompt) async {
  final r = await http.post(
    Uri.parse('$base/v1/images/generations'),
    headers: {'Content-Type': 'application/json'},
    body: jsonEncode({'prompt': prompt, 'n': 1, 'size': '1024x1024'}),
  );
  if (r.statusCode != 202) throw Exception('submit failed: ${r.statusCode}');
  final id = jsonDecode(r.body)['id'] as String;
  await prefs.setString('pending_job', id);   // persist pro resume
  return id;
}

Stream<JobState> pollJob(String id) async* {
  while (true) {
    final r = await http.get(Uri.parse('$base/v1/images/jobs/$id'));
    if (r.statusCode == 404) { yield JobState.expired(); return; }
    final j = jsonDecode(r.body);
    switch (j['status']) {
      case 'queued':
        yield JobState.queued(j['queue_position'] ?? 0);
      case 'running':
        yield JobState.running(j['step'] ?? 0, j['total'] ?? 1);
      case 'done':
        final b64 = j['data'][0]['b64_json'] as String;
        await prefs.remove('pending_job');
        yield JobState.done(base64Decode(b64));   // Uint8List → Image.memory
        return;
      case 'error':
        await prefs.remove('pending_job');
        yield JobState.error(j['error'] ?? 'unknown');
        return;
    }
    await Future.delayed(const Duration(seconds: 2));
  }
}
```

(Uprav podle vašeho state managementu — Bloc/Riverpod/atd. Klidně vyměň ručně
psaný stream za `Timer.periodic`.)

### UI stavy

| stav backendu | UI |
|---|---|
| `queued`, `queue_position > 0` | „Ve frontě – pozice N" |
| `queued`, pozice 0 / `running` step 0 | „Spouštím…" |
| `running` | progress bar `step/total` + „krok 7/14" |
| `done` | zobraz obrázek z `b64_json` |
| `error` | zobraz `error`, tlačítko „Zkusit znovu" |
| `404` | „Výsledek vypršel" → znovu |

### Hrany, na které myslet

- **App na pozadí:** poll se zastaví, ale backend generuje dál. Po návratu do
  appky načti uložené `pending_job` a polluj dál — výsledek tam bude (do 1 h).
- **Vyzvednout do 1 h** (JOB_TTL). Pro delší historii si appka musí obrázek uložit
  lokálně, jakmile dorazí `done`.
- **Cancel:** backend zatím **nemá** cancel endpoint (job doběhne na GPU i když
  appka přestane pollovat). Pokud chcete cancel z UI, je to klientské „přestaň
  pollovat a zahoď" — řekni mi a doplním na backendu skutečné zrušení.
- **Síťové chyby při pollu** (timeout, 5xx): neukončuj — zopakuj poll s backoffem.
- **Více obrázků (`n>1`):** `data` je pole, projdi všechny prvky.

## Volitelné vylepšení (později)

- **Push místo pollingu (FCM):** backend pingne appku, až je `done` — lepší na
  baterku, uživatel může appku zavřít. Vyžaduje doplnění na backendu (registrace
  device tokenu + odeslání push z workeru). Polling rozjeď první, push doplň pak.

## Shrnutí změn pro frontend

- [ ] `POST` čte `202` + `id` (ne rovnou obrázek).
- [ ] Persistovat `id`, polovat `GET /v1/images/jobs/{id}`.
- [ ] Progress UI z `step/total`, fronta z `queue_position`.
- [ ] Zpracovat `done` (base64 → obrázek), `error`, `404`/expiraci.
- [ ] Resume pollingu po návratu z pozadí.
- [ ] (Volitelně) cancel, push.
