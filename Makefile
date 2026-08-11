COMPOSE=docker compose

.PHONY: up down logs migrate test lint format

up:
	$(COMPOSE) up -d --build

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f --tail=100

migrate:
	$(COMPOSE) run --rm chitti alembic upgrade head

test:
	cd app && CHITTI_PROVIDER=fake pytest -q

lint:
	cd app && ruff check . && mypy chitti

format:
	cd app && ruff format .
