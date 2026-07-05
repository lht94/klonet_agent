#!/usr/bin/env bash
set -Eeuo pipefail

readonly agent_user="klonet-agent"
readonly ops_group="klonet-ops"
readonly default_agent_home="/home/klonet-agent"
install_root="${KLONET_INSTALL_ROOT:-}"
project_root=""
python_path=""
mode="ops"
user_id="default"
project_id="default"
service_name="klonet-agent"
env_file="/etc/klonet-agent/klonet-agent.env"
start_service=0
enable_ssh_login=0
set_password=0

usage() {
  cat <<'EOF'
Usage: install-klonet-agent-service.sh --project-root PATH --python PATH [options]

Options:
  --mode MODE              Agent mode (default: ops)
  --user-id ID             Agent user id (default: default)
  --project-id ID          Agent project id (default: default)
  --service-name NAME      systemd service name (default: klonet-agent)
  --env-file PATH          root-managed environment file
  --start                  Restart the service after installation
  --enable-ssh-login       Allow klonet-agent to log in with /bin/bash
  --set-password           Interactively run passwd for klonet-agent
  -h, --help               Show this help

The service is enabled but not started by default because the current Agent
entry point is an interactive CLI. This installer never enables real Ops
execution and only runs the privileged helper with reload-nginx --dry-run.
EOF
}

die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

require_value() {
  [[ $# -ge 2 && -n "$2" ]] || die "$1 requires a value"
}

while (($#)); do
  case "$1" in
    --project-root) require_value "$@"; project_root="$2"; shift 2 ;;
    --python) require_value "$@"; python_path="$2"; shift 2 ;;
    --mode) require_value "$@"; mode="$2"; shift 2 ;;
    --user-id) require_value "$@"; user_id="$2"; shift 2 ;;
    --project-id) require_value "$@"; project_id="$2"; shift 2 ;;
    --service-name) require_value "$@"; service_name="$2"; shift 2 ;;
    --env-file) require_value "$@"; env_file="$2"; shift 2 ;;
    --start) start_service=1; shift ;;
    --enable-ssh-login) enable_ssh_login=1; shift ;;
    --set-password) set_password=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

if ((set_password && !enable_ssh_login)); then
  die "--set-password requires --enable-ssh-login"
fi
[[ "$(id -u)" == "0" ]] || die "run this installer as root (sudo)"
[[ -n "$project_root" ]] || die "--project-root is required"
[[ -n "$python_path" ]] || die "--python is required"
project_root="$(cd "$project_root" && pwd -P)"
python_path="$(readlink -f "$python_path")"
[[ -f "$project_root/agent.py" ]] || die "project root must contain agent.py"
[[ -f "$project_root/scripts/klonet-agent-op" ]] || die "missing scripts/klonet-agent-op"
[[ -f "$project_root/scripts/klonet-agent-op.sudoers" ]] || die "missing sudoers template"
[[ -f "$project_root/scripts/klonet-agent.service.in" ]] || die "missing systemd template"
if ((enable_ssh_login)); then
  [[ -f "$project_root/scripts/klonet-agent-login-profile.sh.in" ]] \
    || die "missing login profile template"
  [[ -x /bin/bash ]] || die "/bin/bash is required for SSH login"
fi
[[ -x "$python_path" ]] || die "Python is not executable: $python_path"
[[ "$service_name" =~ ^[A-Za-z0-9_.@-]+$ ]] || die "invalid service name"
[[ "$env_file" == /* ]] || die "--env-file must be absolute"

for value in "$mode" "$user_id" "$project_id"; do
  [[ "$value" != *$'\n'* && "$value" != *'|'* ]] || die "arguments may not contain newlines or |"
done
for command in getent groupadd useradd usermod install visudo systemctl sudo chown; do
  command -v "$command" >/dev/null || die "required command not found: $command"
done
if ((set_password)); then
  command -v passwd >/dev/null || die "required command not found: passwd"
fi

prefix_path() {
  printf '%s%s' "$install_root" "$1"
}

install_with_mode() {
  local owner="$1" group="$2" mode_bits="$3" source="$4" target="$5"
  if [[ -n "$install_root" ]]; then
    install -m "$mode_bits" "$source" "$target"
  else
    install -o "$owner" -g "$group" -m "$mode_bits" "$source" "$target"
  fi
}

if ! getent group "$ops_group" >/dev/null; then
  groupadd --system "$ops_group"
fi

if id "$agent_user" >/dev/null 2>&1; then
  passwd_entry="$(getent passwd "$agent_user")"
  [[ -n "$passwd_entry" ]] || die "cannot inspect existing $agent_user account"
  IFS=: read -r _ _ existing_uid _ _ existing_home existing_shell <<<"$passwd_entry"
  [[ "$existing_uid" =~ ^[0-9]+$ && "$existing_uid" -lt 1000 ]] \
    || die "existing $agent_user is not a system account"
  [[ "$existing_shell" == "/usr/sbin/nologin" \
    || "$existing_shell" == "/bin/false" \
    || "$existing_shell" == "/bin/bash" ]] \
    || die "existing $agent_user has an unsupported shell"
  if ((enable_ssh_login)) && [[ "$existing_shell" != "/bin/bash" ]]; then
    usermod --shell /bin/bash "$agent_user"
  fi
else
  account_shell="/usr/sbin/nologin"
  if ((enable_ssh_login)); then
    account_shell="/bin/bash"
  fi
  existing_home="$default_agent_home"
  useradd --system --user-group --create-home \
    --home-dir "$existing_home" \
    --shell "$account_shell" \
    "$agent_user"
fi
usermod -aG "$ops_group" "$agent_user"

helper_path="$(prefix_path /usr/local/bin/klonet-agent-op)"
sudoers_path="$(prefix_path /etc/sudoers.d/klonet-agent-op)"
unit_path="$(prefix_path "/etc/systemd/system/${service_name}.service")"
env_target="$(prefix_path "$env_file")"
agent_tmpdir="${existing_home:-$default_agent_home}/.cache/tmp"
tmpdir_target="$(prefix_path "$agent_tmpdir")"
mkdir -p "$(dirname "$helper_path")" "$(dirname "$sudoers_path")" \
  "$(dirname "$unit_path")" "$(dirname "$env_target")" "$tmpdir_target"
if [[ -n "$install_root" ]]; then
  chmod 0700 "$tmpdir_target"
else
  chown "$agent_user:$agent_user" "$tmpdir_target"
  chmod 0700 "$tmpdir_target"
fi

install_with_mode root root 0755 "$project_root/scripts/klonet-agent-op" "$helper_path"

sudoers_tmp="${sudoers_path}.tmp.$$"
trap 'rm -f "${sudoers_tmp:-}" "${unit_tmp:-}"' EXIT
install_with_mode root root 0440 "$project_root/scripts/klonet-agent-op.sudoers" "$sudoers_tmp"
visudo -cf "$sudoers_tmp"
mv -f "$sudoers_tmp" "$sudoers_path"

if [[ ! -e "$env_target" ]]; then
  env_tmp="${env_target}.tmp.$$"
  cat >"$env_tmp" <<'EOF'
# Root-managed Klonet Agent environment.
# Add OPENAI_API_KEY and provider configuration here.
# Keep real Ops execution disabled until the server safety checks pass.
EOF
  printf 'TMPDIR=%s\n' "$agent_tmpdir" >>"$env_tmp"
  install_with_mode root "$agent_user" 0640 "$env_tmp" "$env_target"
  rm -f "$env_tmp"
elif ! grep -q '^TMPDIR=' "$env_target"; then
  printf '\nTMPDIR=%s\n' "$agent_tmpdir" >>"$env_target"
fi

escape_sed() {
  printf '%s' "$1" | sed 's/[&|\\]/\\&/g'
}

shell_quote() {
  printf '%q' "$1"
}

package_parent="$(dirname "$project_root")"
unit_tmp="${unit_path}.tmp.$$"
sed \
  -e "s|@PYTHON@|$(escape_sed "$python_path")|g" \
  -e "s|@PACKAGE_PARENT@|$(escape_sed "$package_parent")|g" \
  -e "s|@ENV_FILE@|$(escape_sed "$env_file")|g" \
  -e "s|@MODE@|$(escape_sed "$mode")|g" \
  -e "s|@USER_ID@|$(escape_sed "$user_id")|g" \
  -e "s|@PROJECT_ID@|$(escape_sed "$project_id")|g" \
  "$project_root/scripts/klonet-agent.service.in" >"$unit_tmp"
if [[ -n "$install_root" ]]; then
  chmod 0644 "$unit_tmp"
else
  chown root:root "$unit_tmp"
  chmod 0644 "$unit_tmp"
fi
mv -f "$unit_tmp" "$unit_path"

if ((enable_ssh_login)); then
  profile_path="$(prefix_path /etc/profile.d/klonet-agent.sh)"
  mkdir -p "$(dirname "$profile_path")"
  profile_tmp="${profile_path}.tmp.$$"
  venv_bin="$(dirname "$python_path")"
  sed \
    -e "s|@VENV_BIN@|$(escape_sed "$(shell_quote "$venv_bin")")|g" \
    -e "s|@ENV_FILE@|$(escape_sed "$(shell_quote "$env_file")")|g" \
    -e "s|@PACKAGE_PARENT@|$(escape_sed "$(shell_quote "$package_parent")")|g" \
    "$project_root/scripts/klonet-agent-login-profile.sh.in" >"$profile_tmp"
  install_with_mode root root 0644 "$profile_tmp" "$profile_path"
  rm -f "$profile_tmp"

  for runtime_dir in memory journals workspaces tracing; do
    runtime_path="$project_root/$runtime_dir"
    mkdir -p "$runtime_path"
    chgrp -R "$agent_user" "$runtime_path"
    chmod -R g+rwX "$runtime_path"
    find "$runtime_path" -type d -exec chmod g+s {} +
  done
fi

systemctl daemon-reload
systemctl enable "${service_name}.service"
if ((start_service)); then
  systemctl restart "${service_name}.service"
fi

visudo -cf "$sudoers_path"
sudo -l -U "$agent_user" >/dev/null
sudo -u "$agent_user" "$helper_path" reload-nginx --dry-run >/dev/null

if ((set_password)); then
  passwd "$agent_user"
fi

printf 'Klonet Agent deployment complete.\n'
printf 'service=%s.service\n' "$service_name"
printf 'account=%s\n' "$agent_user"
printf 'environment_file=%s\n' "$env_file"
if ((!start_service)); then
  printf 'The service was enabled but not started. Configure the environment, then run:\n'
  printf '  systemctl start %s.service\n' "$service_name"
fi
if ((enable_ssh_login)); then
  printf 'SSH account login enabled for %s. Server sshd password policy still applies.\n' "$agent_user"
fi
