# web -- the layered web.* packages under webclient/. Install helpers + the developer gate.
#
#   make install    create env/ (if missing) and editable-install every package + its optional
#                   runtime deps (browser / impersonate / stealth / search) + the dev tools
#   make browsers   download the Chromium the browser tier drives (playwright / patchright)
#   make check      the full gate across EVERY package: fmt-check, mypy --strict, pyright, pytest
#   make test       pytest across every package
#   make mypy       mypy --strict across every package
#   make pyright    pyright across every package
#   make fmt        isort + black over webclient/   (fmt-check = the same, --check only)
#   make demo       the offline end-to-end showcase
#   make clean      drop caches

UV       ?= uv
PY       ?= $(CURDIR)/env/bin/python
PYRIGHT  ?= $(CURDIR)/env/bin/pyright
PKGS     := fetch parse resolve crawl dsl onboard

.PHONY: install venv browsers check test mypy pyright fmt fmt-check demo clean

venv:
	@test -d env || $(UV) venv --python 3.12 env

# Editable installs in dependency order, each in one resolve so the sibling path-deps link up.
# fetch pulls its browser/TLS/stealth extras; onboard pulls its search backend.
install: venv
	$(UV) pip install --python $(PY) \
	  -e ./webclient/parse \
	  -e "./webclient/fetch[browser,impersonate,stealth]" \
	  -e ./webclient/resolve \
	  -e ./webclient/crawl \
	  -e ./webclient/dsl \
	  -e "./webclient/onboard[search]"
	$(UV) pip install --python $(PY) pytest pytest-httpserver mypy pyright isort black

browsers:
	$(PY) -m playwright install chromium

check: fmt-check mypy pyright test

test:
	@set -e; for p in $(PKGS); do echo "== pytest $$p =="; (cd webclient/$$p && $(PY) -m pytest -q); done

mypy:
	@set -e; for p in $(PKGS); do echo "== mypy $$p =="; (cd webclient/$$p && $(PY) -m mypy --strict web); done

pyright:
	@set -e; for p in $(PKGS); do echo "== pyright $$p =="; (cd webclient/$$p && $(PYRIGHT) web); done

fmt:
	$(PY) -m isort webclient/
	$(PY) -m black webclient/

fmt-check:
	$(PY) -m isort --check-only webclient/
	$(PY) -m black --check webclient/

demo:
	$(PY) webclient/demo.py

clean:
	find . -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf .mypy_cache .pytest_cache webclient/*/.pytest_cache webclient/*/.mypy_cache
