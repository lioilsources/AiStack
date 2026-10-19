# Image API — `https://llm.ol1n.com/v1/image/*`

Fronta ComfyUI jobů pro všechny projekty (PLAN-image-api.md). Auth: CF Access service token
(`CF-Access-Client-Id`, `CF-Access-Client-Secret`) jako u LLM. `X-Project` (a–z, 0–9, `-`) je povinná u POST a DELETE.

| Metoda | Cesta | |
|---|---|---|
| POST | `/v1/image/jobs` | `{"workflow": {…API formát…}, "inputs": {"ref.png": "<base64>"}, "client_ref": "panel-17"}` → `202` (nový) nebo `200` (stejný `client_ref` téhož projektu) |
| GET | `/v1/image/jobs/{id}` | `status`: `queued` / `waiting_window` / `running` / `done` / `error` / `cancelled`, `position`, `n_outputs`, `error`, `window` |
| GET | `/v1/image/jobs/{id}/outputs/{n}` | soubor n (PNG, MP4…), pořadí podle id uzlu |
| DELETE | `/v1/image/jobs/{id}` | zruší vlastní job; běžící přeruší v ComfyUI, jen pokud je to on |
| POST | `/v1/image/tag` | WD14, stejné tělo i odpověď jako hostovský `:8097/tag` |
| GET | `/v1/image/status` | `window` (`open` / `closed` / `busy` / `hot` / `cpu`, `reason`, `next_open`, `closes`), fronta po projektech |
| GET | `/v1/image/models` | `checkpoints`, `loras`, `vae`, `unet` z `/object_info`; mimo okno poslední známý seznam se `stale: true` |

- Vstupní obrázky se nahrají do ComfyUI `input/` pod svým jménem; workflow na ně odkazuje v `LoadImage.image`.
- Výstupy jsou jen uzly, které **ukládají** (`SaveImage`, VHS s `save_output`); `PreviewImage` se nevrací.
- Limity: workflow 1 MB, vstupy 20 MB, `width`/`height` nad 2048 px se odmítne.
- Job zadaný mimo okno čeká (`waiting_window`) a dokreslí se v dalším okně comfy. Klient nemusí opakovat.
- Výstupy leží na SPARKu v `~/image-api/out/<projekt>/<datum>/<id>_<n>.<ext>`, nic se nemaže.

```bash
curl -s https://llm.ol1n.com/v1/image/jobs -H "CF-Access-Client-Id: $ID" -H "CF-Access-Client-Secret: $SECRET" \
  -H 'X-Project: webtoon' -H 'Content-Type: application/json' -d @job.json
```
