# webclient -- the developer gate and the deploy helpers (roadmap N14).
#
#   make check      the STABLE gate: stubs, mypy --strict, pyright, pytest, generated docs
#   make test       pytest only
#   make stubs      regenerate the typed surface after changing a Core field / backing op
#   make docs       regenerate the registry-driven docs (docs/errors.md, ...)
#   make demo       the offline end-to-end showcase
#   make profile    py-spy + tracemalloc run of a workload against the lab site
#   make image      build the container (podman or docker, whichever is present)
#   make serve      run the HTTP service locally

PY      ?= env/bin/python
ENGINE  ?= $(shell command -v podman >/dev/null 2>&1 && echo podman || echo docker)
IMAGE   ?= webclient:dev
PORT    ?= 8000

.PHONY: check test stubs stubs-check typecheck docs docs-check demo profile image serve clean

check: stubs-check typecheck docs-check test

test:
	$(PY) -m pytest -q

stubs:
	$(PY) scripts/gen_stubs.py

stubs-check:
	$(PY) scripts/gen_stubs.py --check

typecheck:
	$(PY) -m mypy --strict webclient
	env/bin/pyright webclient

docs:
	$(PY) scripts/gen_docs.py all

docs-check:
	$(PY) scripts/gen_docs.py all --check

demo:
	$(PY) demo.py

profile:
	$(PY) scripts/profile.py $(ARGS)

image:
	$(ENGINE) build -t $(IMAGE) -f Containerfile .

serve:
	$(PY) -m uvicorn --factory webclient.service:create_app --host 0.0.0.0 --port $(PORT)

clean:
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	rm -rf .mypy_cache .pytest_cache build *.egg-info
