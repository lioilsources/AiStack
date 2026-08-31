COMPOSE := docker compose -f deploy/docker-compose.yml --env-file .env
COMPOSE_LLM   := docker compose -f deploy/docker-compose.llm.yaml --env-file .env
COMPOSE_IMAGE     := docker compose -f deploy/docker-compose.image.yaml --env-file .env
COMPOSE_IMAGE_NIM := docker compose -f deploy/docker-compose.image-nim.yaml --env-file .env
COMPOSE_OCR   := docker compose -f deploy/docker-compose.ocr.yaml --env-file .env
COMPOSE_SWARM := docker compose -f deploy/docker-compose.swarm.yaml --env-file .env
COMPOSE_TUNE      := docker compose -f deploy/docker-compose.tune-image.yaml --env-file .env
COMPOSE_TRANSLATE := docker compose -f deploy/docker-compose.translate.yaml --env-file .env

.PHONY: build up down logs ps \
        up-llm up-image up-ocr \
        down-llm down-image down-ocr \
        up-translate up-translate-lean down-translate \
        up-swarm down-swarm up-swarm-director down-swarm-director \
        up-tune-image down-tune-image \
        up-dev down-dev logs-dev \
        up-image-schnell up-image-kontext up-image-dev down-image-nim logs-kontext \
        download-flux download-flux-lora download-qwen-vl \
        download-nemotron download-nemotron-coder download-ocr-models \
        download-scout \
        gateway-build gateway-run

## Full stack
build:
	$(COMPOSE) build

up:
	$(COMPOSE) up -d

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f

ps:
	$(COMPOSE) ps

## dev NIM container
up-dev:
	$(COMPOSE_LLM) up -d --no-deps dev

down-dev:
	$(COMPOSE_LLM) stop dev && $(COMPOSE_LLM) rm -f dev

logs-dev:
	docker exec dev tail -f /opt/nim/nginx/error.log

## Individual modules
up-llm:
	$(COMPOSE_LLM) up -d

up-image:
	$(COMPOSE_IMAGE) up -d --build

up-ocr:
	$(COMPOSE_OCR) up -d --build

down-llm:
	$(COMPOSE_LLM) down

down-image:
	$(COMPOSE_IMAGE) down

down-ocr:
	$(COMPOSE_OCR) down

## Translate module
# up-translate       plný profil (57 GiB) — nejrychlejší obohacení korpusu
# up-translate-lean  úsporný (~36 GiB) — vejde se vedle ComfyUI, o 49 % pomalejší
up-translate:
	$(COMPOSE_TRANSLATE) up -d --force-recreate translate

up-translate-lean:
	TRANSLATE_MAX_BATCH=8 TRANSLATE_KV_FRACTION=0.2 $(COMPOSE_TRANSLATE) up -d --force-recreate translate

down-translate:
	$(COMPOSE_TRANSLATE) down

## SwarmBattle LLM module
up-swarm:
	$(COMPOSE_SWARM) up -d

down-swarm:
	$(COMPOSE_SWARM) down

up-swarm-director:
	$(COMPOSE_SWARM) --profile director up -d swarm-director

down-swarm-director:
	$(COMPOSE_SWARM) --profile director stop swarm-director
	$(COMPOSE_SWARM) --profile director rm -f swarm-director

## Image NIM stack — přepínatelné FLUX modely (port 8030)
# Každý target stopne ostatní dva před startem nového.
up-image-schnell:
	$(COMPOSE_IMAGE_NIM) stop flux-kontext flux-dev 2>/dev/null || true
	$(COMPOSE_IMAGE_NIM) rm -f flux-kontext flux-dev 2>/dev/null || true
	$(COMPOSE_IMAGE_NIM) up -d flux-schnell

up-image-kontext:
	$(COMPOSE_IMAGE_NIM) stop flux-schnell flux-dev 2>/dev/null || true
	$(COMPOSE_IMAGE_NIM) rm -f flux-schnell flux-dev 2>/dev/null || true
	$(COMPOSE_IMAGE_NIM) up -d flux-kontext

up-image-dev:
	$(COMPOSE_IMAGE_NIM) stop flux-schnell flux-kontext 2>/dev/null || true
	$(COMPOSE_IMAGE_NIM) rm -f flux-schnell flux-kontext 2>/dev/null || true
	$(COMPOSE_IMAGE_NIM) up -d flux-dev

down-image-nim:
	$(COMPOSE_IMAGE_NIM) down

logs-image:
	$(COMPOSE_IMAGE_NIM) logs -f --tail=50

## Monitoring — flux-kontext flow (CF tunnel → gen-queue → flux-kontext NIM)
# Prefix: [cf] cloudflared, [queue] gen-queue, [nim] flux-kontext NIM
# Filtruje health checky a JWT hlavičky z CF debug logu.
logs-kontext:
	@trap 'kill 0' EXIT; \
	docker logs -f --timestamps --tail=10 ai-gen-queue 2>&1 \
		| grep --line-buffered -v '"msg":"health"' \
		| sed 's/^/\x1b[36m[queue]\x1b[0m /' & \
	docker logs -f --timestamps --tail=10 flux-kontext 2>&1 \
		| grep --line-buffered -v "readiness check" \
		| sed 's/^/\x1b[33m[nim]  \x1b[0m /' & \
	docker logs -f --timestamps --tail=5 ai-cloudflared-1 2>&1 \
		| grep --line-buffered "flux-kontext\|infer\|/jobs\|[45][0-9][0-9] " \
		| sed 's/ headers={[^}]*}//g; s/^/\x1b[35m[cf]   \x1b[0m /' & \
	wait

## Card Forge image-tune module
# fáze train: stopne 'dev' (uvolní ~28 GB) a spustí builder+validator
up-tune-image:
	$(COMPOSE_LLM) stop dev
	$(COMPOSE_TUNE) up -d

# fáze bulk: shodí builder+validator — paměť zůstane hostovskému ComfyUI na batching
# (dev zpět: make up-llm)
down-tune-image:
	$(COMPOSE_TUNE) down

## Model downloads (requires HF_TOKEN in .env)
download-nemotron:
	env $$(grep -v '^#' .env | xargs) bash scripts/download_nemotron.sh

download-nemotron-coder:
	env $$(grep -v '^#' .env | xargs) bash scripts/download_nemotron_coder.sh

download-flux:
	env $$(grep -v '^#' .env | xargs) bash scripts/download_flux.sh

download-flux-lora:
	env $$(grep -v '^#' .env | xargs) bash scripts/download_flux_lora.sh

download-qwen-vl:
	env $$(grep -v '^#' .env | xargs) bash scripts/download_qwen_vl.sh

download-ocr-models:
	env $$(grep -v '^#' .env | xargs) bash scripts/download_ocr_models.sh

download-scout:
	env $$(grep -v '^#' .env | xargs) bash scripts/download_scout.sh

## Go gateway — local dev build and run
gateway-build:
	cd gateway && go build -o gateway ./cmd/gateway

gateway-run: gateway-build
	cd gateway && \
	  LITELLM_URL=http://localhost:4000 \
	  IMAGE_API_URL=http://localhost:8002 \
	  OCR_API_URL=http://localhost:8003 \
	  ./gateway
