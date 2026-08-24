.DEFAULT_GOAL := help

UV ?= uv
PYTHON ?= 3.12

.PHONY: help setup setup-tooling check test test-fast format lint validate compile pre-commit

help:
	@printf '%s\n' \
		'make setup       Install the locked Python environment' \
		'make check       Run all deterministic repository gates' \
		'make test        Run the full strict pytest suite' \
		'make test-fast   Run pytest and stop at the first failure' \
		'make format      Apply Ruff formatting' \
		'make lint        Run Ruff lint checks'

setup setup-tooling:
	$(UV) sync --python $(PYTHON)

format:
	$(UV) run --python $(PYTHON) ruff format src tests scripts

lint:
	$(UV) run --python $(PYTHON) ruff check src tests scripts

test:
	$(UV) run --python $(PYTHON) pytest

test-fast:
	$(UV) run --python $(PYTHON) pytest -x

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
