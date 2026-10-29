# droideum — developer tasks. `make check` is exactly what CI runs.
.PHONY: all check test smoke mcp selftest lint lint-install version help

SHELL  := /bin/bash
SCRIPTS := bin/droideum install.sh test/smoke-test.sh

all: check                # default target

check: lint test          # everything CI runs
	@echo
	@echo "all green"

test: smoke mcp selftest  # every test suite

smoke:                    # 139 CLI assertions, stubbed QEMU/adb, no VM needed
	@bash test/smoke-test.sh

mcp:                      # 75 MCP protocol assertions
	@STUB_ADB=1 python3 test/mcp-test.py

selftest:                 # agent server internal consistency
	@./bin/droideum-agentd selftest

lint:                     # shellcheck must be completely silent
	@if command -v shellcheck >/dev/null 2>&1; then \
	  shellcheck -S info $(SCRIPTS) && echo "shellcheck: clean"; \
	else \
	  echo "shellcheck not installed (pip install shellcheck-py) - skipped"; \
	fi

lint-install:             # install shellcheck via pip/pkgs
	@pip install --quiet shellcheck-py || pkg install -y shellcheck

version:                  # print component versions
	@./bin/droideum --version
	@./bin/droideum-agentd --version 2>/dev/null || true

help:                     # this list
	@echo "droideum developer tasks:"
	@sed -n 's/^\([a-z-]*\):.*# \(.*\)/  \1  — \2/p' Makefile
