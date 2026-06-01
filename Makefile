COMPOSE := docker compose -f deploy/docker-compose.yml --env-file .env
COMPOSE_LLM   := docker compose -f deploy/docker-compose.llm.yaml --env-file .env
COMPOSE_IMAGE := docker compose -f deploy/docker-compose.image.yaml --env-file .env
COMPOSE_OCR   := docker compose -f deploy/docker-compose.ocr.yaml --env-file .env

.PHONY: build up down logs ps \
        up-llm up-image up-ocr \
        down-llm down-image down-ocr \
        download-flux download-flux-lora download-qwen-vl \
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

## Model downloads (requires HF_TOKEN in .env)
download-flux:
	env $$(grep -v '^#' .env | xargs) bash scripts/download_flux.sh

download-flux-lora:
	env $$(grep -v '^#' .env | xargs) bash scripts/download_flux_lora.sh

download-qwen-vl:
	env $$(grep -v '^#' .env | xargs) bash scripts/download_qwen_vl.sh

## Go gateway — local dev build and run
gateway-build:
	cd gateway && go build -o gateway ./cmd/gateway

gateway-run: gateway-build
	cd gateway && \
	  LITELLM_URL=http://localhost:4000 \
	  IMAGE_API_URL=http://localhost:8002 \
	  OCR_API_URL=http://localhost:8003 \
	  ./gateway
