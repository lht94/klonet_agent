# Ops Sudo Helper Invocation Design

## Goal

Make every real execution of the root-owned Ops helper pass through non-interactive sudo, while keeping dry-run behavior unprivileged and preserving the existing OperationPlan confirmation gates.

## Current Problem

The server installation contract grants the dedicated `klonet-agent` account passwordless access only to allowlisted `/usr/local/bin/klonet-agent-op ... --execute ...` commands. Some recipes already invoke root-path operations through `sudo -n`, but restart, stop, start, and Nginx recipes currently execute the helper directly. A root-owned executable does not gain root privileges merely because root owns the file, so those calls bypass the intended sudoers boundary and run as the Agent user.

## Approach

Add one command-construction boundary to `ControlledRecipeRunner`:

- Dry-run helper commands remain `/usr/local/bin/klonet-agent-op <action> --dry-run ...`.
- Real helper commands become `sudo -n /usr/local/bin/klonet-agent-op <action> --execute ...`.
- Existing root-path archive extraction and install-script helper calls use the same constructor instead of maintaining separate sudo prefixes.
- Recipes that intentionally execute as the Agent user and do not call the helper remain unchanged.

Central construction is preferred over adding conditionals to every recipe because future helper-backed recipes inherit the same privilege behavior automatically.

## Covered Recipes

The shared helper boundary covers:

- `restart_screen_component`
- `stop_screen_component`
- `stop_platform_screens`
- `start_platform_screens`
- `reload_nginx`
- root-path `extract_archive`
- root-path `run_install_script`

## Safety Properties

- Use `sudo -n`, never interactive `sudo`, so missing or invalid sudoers policy fails immediately without asking for a password.
- Never pass a password through tool arguments, model context, OperationPlan data, traces, environment variables, or stdin.
- Continue relying on the root-owned helper for component, platform, screen, path, archive-member, script, and Nginx validation.
- Preserve exact `confirm <plan_id>` and `confirm-step <plan_id> <step_id>` authorization checks.
- Keep `KLONET_AGENT_OPS_REAL_EXECUTION` disabled by default.
- Do not add systemd deployment or run live `--execute` operations as part of this change.

## Failure Handling

If `sudo -n` rejects the command, cannot resolve the helper, or the helper returns nonzero, the existing `CalledProcessError` handling remains responsible for returning a failed or blocked recipe result. If helper output reports `environment_changed=unknown`, execution remains blocked pending runtime inspection.

## Testing

Update recipe tests to verify exact command arrays:

- every covered real helper command starts with `sudo`, `-n`, followed by the configured helper path;
- every covered dry-run preview starts with the helper path and contains no sudo prefix;
- root-path archive and install recipes retain their existing effective command contract;
- helper failures retain current failed/blocked semantics;
- installation-contract tests continue to reject direct sudoers access to shell, Python, screen, kill, and Nginx binaries.

Run the focused Ops operation, helper contract, and helper script suites before the complete test suite.

## Documentation

Update `docs/ops/klonet-agent-op-install.md` to state that real execution always invokes `sudo -n` and that the dedicated service account must be the sudoers principal. Keep systemd deployment explicitly out of scope.

## Acceptance Criteria

- No helper-backed real recipe invokes `/usr/local/bin/klonet-agent-op` directly.
- No dry-run recipe invokes sudo.
- No password prompt or password transport path is introduced.
- Existing plan and step confirmation behavior remains unchanged.
- Focused security and Ops tests pass without executing live server changes.
