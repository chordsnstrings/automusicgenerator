.PHONY: install test test-fast lint preflight run serve clean

install:
	pip install -e ".[dev]"

test:
	pytest -q

test-fast:            ## skip tests that hit live third-party feeds
	pytest -q -m "not network"

preflight:
	dailyfive preflight

signals:
	dailyfive signals

run:
	dailyfive run

serve:
	dailyfive serve --port 8080

clean:
	find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache work/*.db
