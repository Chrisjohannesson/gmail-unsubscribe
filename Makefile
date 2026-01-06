.PHONY: help build run dev stop logs clean sync shell

# Default target
help:
	@echo "Gmail Unsubscribe Manager"
	@echo ""
	@echo "Usage:"
	@echo "  make build    - Build Docker image"
	@echo "  make run      - Start the application (Docker)"
	@echo "  make dev      - Run locally without Docker"
	@echo "  make stop     - Stop the application"
	@echo "  make logs     - View container logs"
	@echo "  make shell    - Open shell in container"
	@echo "  make sync     - Trigger email sync"
	@echo "  make clean    - Remove containers and images"
	@echo ""

# Build the Docker image
build:
	docker-compose build

# Run with Docker
run: build
	@mkdir -p data
	docker-compose up -d
	@echo ""
	@echo "Application starting at http://localhost:8000"
	@echo "Run 'make logs' to view logs"

# Run locally (development mode)
dev:
	python -m uvicorn app:app --host 127.0.0.1 --port 8000 --reload

# Stop the application
stop:
	docker-compose down

# View logs
logs:
	docker-compose logs -f

# Open shell in container
shell:
	docker-compose exec app /bin/bash

# Trigger email sync via API
sync:
	@echo "Triggering email sync..."
	@curl -s http://localhost:8000/api/sync | python -m json.tool

# Clean up
clean:
	docker-compose down -v --rmi local
	rm -rf data/__pycache__

# Install dependencies locally
install:
	pip install -r requirements.txt

# Check health
health:
	@curl -s http://localhost:8000/health | python -m json.tool
