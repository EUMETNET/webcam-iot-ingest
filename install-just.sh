#!/usr/bin/env bash

set -euo pipefail

just_version="1.57.0"
install_dir="${XDG_BIN_HOME:-${HOME}/.local/bin}"
installer="$(mktemp /tmp/just-install.XXXXXX)"
trap 'rm -f "$installer"' EXIT

mkdir -p "$install_dir"
curl --proto '=https' --tlsv1.2 -fsS \
    https://just.systems/install.sh -o "$installer"
bash "$installer" --tag "$just_version" --to "$install_dir"

echo "installed just ${just_version} in ${install_dir}"
echo "ensure ${install_dir} is present in PATH"
