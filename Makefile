COMPOSE := docker compose -f deploy/docker-compose.yml --env-file .env
COMPOSE_LLM   := docker compose -f deploy/docker-compose.llm.yaml --env-file .env
COMPOSE_IMAGE := docker compose -f deploy/docker-compose.image.yaml --env-file .env
COMPOSE_OCR   := docker compose -f deploy/docker-compose.ocr.yaml --env-file .env
COMPOSE_SWARM := docker compose -f deploy/docker-compose.swarm.yaml --env-file .env

.PHONY: build up down logs ps \
        up-llm up-image up-ocr \
        down-llm down-image down-ocr \
        up-swarm down-swarm up-swarm-director down-swarm-director \
        download-flux download-flux-lora download-qwen-vl \
        download-nemotron download-nemotron-coder \
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

download-pero-models:
	env $$(grep -v '^#' .env | xargs) bash scripts/download_pero_models.sh

## Go gateway — local dev build and run
gateway-build:
	cd gateway && go build -o gateway ./cmd/gateway

gateway-run: gateway-build
	cd gateway && \
	  LITELLM_URL=http://localhost:4000 \
	  IMAGE_API_URL=http://localhost:8002 \
	  OCR_API_URL=http://localhost:8003 \
	  ./gateway
