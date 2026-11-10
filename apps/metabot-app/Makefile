SHELL := /bin/bash
# MetaBot app: the one place test commands live. CI and the contributing guides
# call these targets and nothing else (tests/test_contributing.py keeps them in step).
PYTHON ?= python3
NODE ?= node
REPO := $(abspath ../..)
export METABOT_SRC ?= $(REPO)/apps/metabot/src

.PHONY: check unit ui verifier guard test-linux test-macos selftest-linux

check: unit ui verifier guard

# Clean testing: tools/run_unit.py fails on leaked processes, sockets, files or temp leftovers.
unit:
	$(PYTHON) tools/run_unit.py

ui:
	$(NODE) tests/ui-smoke.mjs

verifier:
	$(NODE) --test tests/mbverify.test.mjs

guard:
	$(PYTHON) tools/devguard.py --procs

# What CI's linux and macos jobs run, and what the OS guides tell a contributor to run.
# Both are `check` plus the leftover-process guard; nothing in either is OS-only yet.
test-linux: check guard

test-macos: check guard

# The Linux self-test (stream T): the runner against the real core in a loopback-only
# namespace. Needs METABOT_PYTHON = an interpreter with the core installed.
selftest-linux:
	METABOT_PYTHON=$(METABOT_PYTHON) tools/selftest-linux.sh $(SELFTEST_ARGS)
