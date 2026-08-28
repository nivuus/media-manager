# Package Nivuus media-manager — cible de test.
#
# Les suites sont des scripts Python autonomes, pas du pytest : c'est le style
# du depot installer, et il ne demande rien d'autre que python3 + PyYAML.
#
# NIVUUS_INSTALLER_DIR fait valider le manifeste par le VRAI parseur du moteur
# (installer/packages/manifest.py) au lieu de la reverification locale. C'est
# la verification qui fait autorite ; la locale existe pour que le depot reste
# testable seul.
#   make test NIVUUS_INSTALLER_DIR=$$HOME/Projects/Nivuus/packages/installer

PACKAGE_DIR := $(CURDIR)
PYTHON ?= python3

.PHONY: test help

help:
	@grep -E '^[a-zA-Z_-]+:.*' $(MAKEFILE_LIST) | sed 's/:.*//' | sort

test:
	@for t in test_compose_portable test_manifest_contract \
	          test_maintenance_units test_install_hook test_activate_hook; do \
	    echo "--- $$t"; \
	    $(PYTHON) $(PACKAGE_DIR)/tests/$$t.py || exit 1; \
	done
