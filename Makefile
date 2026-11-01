# droideum — developer tasks. `make check` is exactly what CI runs.
.PHONY: all check test smoke mcp selftest lint lint-install version help

SHELL  := /bin/bash
SCRIPTS := bin/droideum install.sh test/smoke-test.sh

all: check                # default target

check: lint test          # everything CI runs
	@echo
	@echo "all green"

test: smoke mcp selftest  # every test suite

smoke:                    # CLI assertions, stubbed QEMU/adb, no VM needed
	@bash test/smoke-test.sh

mcp:                      # MCP protocol + 34-tool + resource assertions
	@STUB_ADB=1 python3 test/mcp-test.py

selftest:                 # agent server internal consistency
	@./bin/droideum-agentd selftest

lint:                     # shellcheck + python syntax + version sync
	@if command -v shellcheck >/dev/null 2>&1; then \
	  shellcheck -S info $(SCRIPTS) && echo "shellcheck: clean"; \
	else \
	  echo "shellcheck not installed (pip install shellcheck-py) - skipped"; \
	fi
	@python3 -m py_compile bin/droideum-agentd examples/agent_loop.py test/mcp-test.py
	@v1=$$(./bin/droideum --version | awk '{print $$2}'); \
	 v2=$$(./bin/droideum-agentd --version | awk '{print $$2}'); \
	 [ "$$v1" = "$$v2" ] || { echo "version mismatch: droideum=$$v1 agentd=$$v2" >&2; exit 1; }

lint-install:             # install shellcheck via pip/pkgs
	@pip install --quiet shellcheck-py || pkg install -y shellcheck

version:                  # print component versions
	@./bin/droideum --version
	@./bin/droideum-agentd --version

help:                     # this list
	@echo "droideum developer tasks:"
	@sed -n 's/^\([a-z-]*\):.*# \(.*\)/  \1  — \2/p' Makefile
