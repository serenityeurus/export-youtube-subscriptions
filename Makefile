# `make ci` is what Forgejo CI runs (node:22-bookworm ships python3 3.11).
# Tests are stdlib-only (unittest) and fully offline; no installs needed.
PYTHON ?= python3
PY_SOURCES := export_youtube_subscriptions.py export-youtube-subscriptions tests/test_export.py

.PHONY: ci lint test smoke
ci: lint test smoke

lint:
	$(PYTHON) -m py_compile $(PY_SOURCES)
	@# ruff is optional: used when uv is installed (dev hosts), skipped in CI.
	@if command -v uvx >/dev/null 2>&1; then \
		echo "uvx ruff check ."; uvx ruff check . ; \
	else echo "ruff: skipped (uvx not installed)"; fi

test:
	$(PYTHON) -m unittest discover -s tests -v

smoke:
	$(PYTHON) export-youtube-subscriptions --help >/dev/null
