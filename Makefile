SEED ?= 42
PY ?= .venv/bin/python

setup:
	python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
up:
	docker compose up -d
data:
	$(PY) -m passthru.gen_data --seed $(SEED)
load:
	$(PY) -m passthru.load --reset
run:
	$(PY) -m passthru.flow
test:
	$(PY) -m pytest -q tests
