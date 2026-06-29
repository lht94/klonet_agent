# Virtual Environment Migration Design

## Goal

Replace the project-local Python 3.8 virtual environment named `agent/` with the conventional ignored path `.venv/`, while preserving the exact installed package versions and keeping a working rollback until verification succeeds.

## Why Rebuild Instead of Rename

The current environment contains absolute references to `/home/adminis/lht/agent/klonet_agent/agent` in activation scripts and console-script shebangs such as `pytest`, `pip`, and `openai`. A filesystem rename would leave those entry points broken. The environment must therefore be recreated at its final path.

## Migration Procedure

1. Export the old environment with `agent/bin/python -m pip freeze --all` to a temporary lock file outside the repository.
2. Record a sorted package-and-version snapshot for later comparison.
3. Create `.venv/` with the same base interpreter, `/usr/local/python3/bin/python3.8`.
4. Install the exact exported versions into `.venv/` from the configured Python package source.
5. Compare the new sorted package snapshot with the old snapshot. Package names and versions must match exactly.
6. Verify the new interpreter, CLI startup, and focused CLI tests.
7. Delete `agent/` only after every required verification succeeds.

The existing `.gitignore` already ignores `.venv/`, so this migration does not require a tracked ignore-rule change.

## Safety and Rollback

- Keep `agent/` untouched while `.venv/` is created, populated, and tested.
- If environment creation, package installation, comparison, or testing fails, stop and retain `agent/` as the working environment.
- Do not modify application source code or `requirements.txt` during this migration.
- Do not commit the temporary exact-version lock file; store it under `/tmp`.
- Removing `agent/` is the final action and is authorized only after successful verification.

## Verification

The new environment must satisfy all of the following before cleanup:

- `.venv/bin/python --version` reports Python 3.8.0.
- `.venv/bin/python -m pip freeze --all` matches the old environment's normalized package-and-version list.
- `.venv/bin/python -m klonet_agent.agent --help` exits successfully.
- `.venv/bin/python -m pytest tests/test_cli_entry.py -q` passes all CLI tests.
- `git status --short` does not list `.venv/`.

The full test suite currently has two known Python 3.8 collection errors outside this migration's scope. The migration must not introduce additional failures.

## User Follow-Up

After cleanup, already-open shells that activated `agent/` must run `deactivate` and then `source .venv/bin/activate`. VS Code should select `/home/adminis/lht/agent/klonet_agent/.venv/bin/python` as the interpreter.
