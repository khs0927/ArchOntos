.PHONY: install test lint format compile up down

install:
	pip install -e '.[dev]'

test:
	pytest

lint:
	ruff check .

format:
	ruff format .

compile:
	python -m compileall -q src apps tests

up:
	docker compose up -d

down:
	docker compose down
