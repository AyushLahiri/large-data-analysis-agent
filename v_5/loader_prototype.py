"""Notebook entry points for the production loader; no second workflow copy."""
from pathlib import Path
from v_5.app.docker import run_code
from v_5.ingest.data_loader.checkpoint import (
    LoaderRunner as LoaderPrototype, SESSION_TOKENS, OUTPUT_TOKENS, fresh_loader)

LAB = Path(__file__).parent / 'app' / 'runtime' / 'loader_prototype_lab'


def docker_execute(code, workspace, current):
    """Run a toy script through the app sandbox without mounting user uploads."""
    return run_code(code, {}, workspace, current)
