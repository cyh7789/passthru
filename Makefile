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
swarm:
	$(PY) -m passthru.swarm -n 20
T ?= tenant_us_acme
hero:
	$(PY) -m passthru.hero
replay:
	$(PY) -m passthru.replay --seed $(SEED)
evalmem:
	$(PY) -m passthru.evalmem --seed $(SEED)
test:
	$(PY) -m pytest -q tests
mcp:
	PASSTHRU_TENANT=$(T) $(PY) -m passthru.mcp_server
dash:
	$(PY) -m passthru.dash   # http://localhost:8000
