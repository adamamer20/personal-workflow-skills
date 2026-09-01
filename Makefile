.DEFAULT_GOAL := help

UV ?= uv
PYTHON ?= 3.12

.PHONY: help setup setup-tooling install-personal-workflow-skills check test test-fast test-contracts test-controller test-workers test-integrations test-workflow-assets format lint validate compile pre-commit

help:
	@printf '%s\n' \
		'make setup       Install the locked Python environment' \
		'make install-personal-workflow-skills Install matching shared-user plugin and codex-flow tool' \
		'make check       Run all deterministic repository gates' \
		'make test        Run the full strict pytest suite' \
		'make test-contracts       Run the contracts semantic partition' \
		'make test-controller      Run the controller semantic partition' \
		'make test-workers         Run the workers semantic partition' \
		'make test-integrations    Run the integrations semantic partition' \
		'make test-workflow-assets Run the workflow-assets semantic partition' \
		'make test-fast   Run pytest and stop at the first failure' \
		'make format      Apply Ruff formatting' \
		'make lint        Run Ruff lint checks'

setup setup-tooling:
	$(UV) sync --python $(PYTHON)

install-personal-workflow-skills:
	$(UV) run --python $(PYTHON) python scripts/install_personal_workflow_skills.py

format:
	$(UV) run --python $(PYTHON) ruff format src tests scripts

lint:
	$(UV) run --python $(PYTHON) ruff check src tests scripts

test:
	$(UV) run --python $(PYTHON) pytest

test-fast:
	$(UV) run --python $(PYTHON) pytest -x

test-contracts:
	@paths="$$( $(UV) run --python $(PYTHON) python scripts/validate.py --partition contracts )" && $(UV) run --python $(PYTHON) pytest $$paths

test-controller:
	@paths="$$( $(UV) run --python $(PYTHON) python scripts/validate.py --partition controller )" && $(UV) run --python $(PYTHON) pytest $$paths

test-workers:
	@paths="$$( $(UV) run --python $(PYTHON) python scripts/validate.py --partition workers )" && $(UV) run --python $(PYTHON) pytest $$paths

test-integrations:
	@paths="$$( $(UV) run --python $(PYTHON) python scripts/validate.py --partition integrations )" && $(UV) run --python $(PYTHON) pytest $$paths

test-workflow-assets:
	@paths="$$( $(UV) run --python $(PYTHON) python scripts/validate.py --partition workflow-assets )" && $(UV) run --python $(PYTHON) pytest $$paths

validate:
	$(UV) run --python $(PYTHON) python scripts/validate.py

compile:
	$(UV) run --python $(PYTHON) python -m compileall -q src tests scripts

pre-commit:
	$(UV) run --python $(PYTHON) pre-commit run --all-files

check:
	$(UV) run --python $(PYTHON) ruff format --check src tests scripts
	$(UV) run --python $(PYTHON) ruff check src tests scripts
	$(UV) run --python $(PYTHON) pytest
	$(UV) run --python $(PYTHON) python scripts/validate.py
	$(UV) run --python $(PYTHON) python -m compileall -q src tests scripts
	$(UV) run --python $(PYTHON) pre-commit run --all-files
