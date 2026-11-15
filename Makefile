.PHONY: install test lint fmt seed demo serve serve-offline check docker

install:
	python -m venv .venv && .venv/bin/pip install -e '.[dev]'

test:
	pytest

lint:
	ruff check src tests examples && ruff format --check src tests examples

fmt:
	ruff format src tests examples && ruff check --fix src tests examples

seed:
	order-agent seed

# Full pipeline without an API key: damaged item → parked refund → approval → reply
demo: seed
	order-agent ticket --offline --customer CUST-BOB \
	  --message "My grinder from ORD-1003 arrived broken (GRINDER-03). Money back please."

serve:
	order-agent serve

serve-offline:
	ORDER_AGENT_OFFLINE=1 ORDER_AGENT_SEED=1 order-agent serve

check:
	order-agent check-setup

docker:
	docker compose up --build
