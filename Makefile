# Polymarket Sentinel Bot - Makefile
# Convenient commands for development and deployment

.PHONY: help install dev run run-live docker-build docker-run docker-stop test lint clean

# Default target
help:
	@echo "Polymarket Sentinel Bot"
	@echo ""
	@echo "Usage:"
	@echo "  make install      Install dependencies"
	@echo "  make dev          Install with dev dependencies"
	@echo "  make run          Run bot in dry-run mode"
	@echo "  make run-live     Run bot in live mode (CAUTION)"
	@echo "  make docker-build Build Docker image"
	@echo "  make docker-run   Run in Docker (dry-run)"
	@echo "  make docker-live  Run in Docker (live mode)"
	@echo "  make docker-stop  Stop Docker containers"
	@echo "  make test         Run tests"
	@echo "  make lint         Run linters"
	@echo "  make clean        Clean build artifacts"

# ============ Installation ============

install:
	pip install -e .

dev:
	pip install -e ".[dev]"

# ============ Running ============

run:
	python -m src.cli run --dry-run

run-live:
	@echo "WARNING: This will execute real trades!"
	@read -p "Are you sure? (y/N) " confirm && [ "$$confirm" = "y" ]
	python -m src.cli run --live

status:
	python -m src.cli status

markets:
	python -m src.cli markets --limit 30

# ============ Docker ============

docker-build:
	docker build -t polymarket-sentinel:latest .

docker-run:
	docker-compose up -d polysentinel
	docker-compose logs -f polysentinel

docker-live:
	@echo "WARNING: This will execute real trades!"
	@read -p "Are you sure? (y/N) " confirm && [ "$$confirm" = "y" ]
	DRY_RUN=false docker-compose up -d polysentinel
	docker-compose logs -f polysentinel

docker-dev:
	docker-compose --profile dev up polysentinel-dev

docker-stop:
	docker-compose down

docker-logs:
	docker-compose logs -f

docker-shell:
	docker-compose exec polysentinel /bin/bash

# ============ Testing ============

test:
	pytest tests/ -v --cov=src --cov-report=term-missing

test-fast:
	pytest tests/ -v -x --tb=short

# ============ Code Quality ============

lint:
	ruff check src/
	black --check src/
	mypy src/

format:
	black src/
	ruff check --fix src/

# ============ Cleanup ============

clean:
	rm -rf build/
	rm -rf dist/
	rm -rf *.egg-info
	rm -rf .pytest_cache
	rm -rf .coverage
	rm -rf htmlcov
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
