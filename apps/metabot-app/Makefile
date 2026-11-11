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

# The desktop shell (D2): the std-only core crate's tests, plus the structural tests of the
# Tauri crate. The bundle itself (deb, AppImage) is CI's `bundle-linux` job, never this PC.
desktop-test:
	cargo test --manifest-path desktop/shell-core/Cargo.toml
	$(PYTHON) -m unittest tests.test_desktop_shell

# The Linux self-test (stream T): the runner against the real core in a loopback-only
# namespace. Needs METABOT_PYTHON = an interpreter with the core installed.
# Twice: once with tmux on PATH (desks), once as the phone runs it (METABOT_ON_DEVICE=1:
# no desks, the app runs the core itself). Each pass keeps its own report folder.
SELFTEST_OUT ?= /var/tmp/metabot-app/selftest
selftest-linux:
	SELFTEST_OUT=$(SELFTEST_OUT)/desks METABOT_PYTHON=$(METABOT_PYTHON) tools/selftest-linux.sh $(SELFTEST_ARGS)
	SELFTEST_OUT=$(SELFTEST_OUT)/on-device METABOT_ON_DEVICE=1 METABOT_PYTHON=$(METABOT_PYTHON) tools/selftest-linux.sh $(SELFTEST_ARGS)
