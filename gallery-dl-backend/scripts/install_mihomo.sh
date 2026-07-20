#!/usr/bin/env bash
set -euo pipefail

version="v1.19.28"
script_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_dir="$(CDPATH= cd -- "$script_dir/.." && pwd)"
install_dir="$project_dir/bin"
force=0

usage() {
    printf 'Usage: %s [--install-dir PATH] [--force]\n' "$0"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --install-dir)
            [[ $# -ge 2 ]] || { usage >&2; exit 2; }
            install_dir="$2"
            shift 2
            ;;
        --force)
            force=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            printf 'Unknown option: %s\n' "$1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

[[ "$(uname -s)" == "Linux" ]] || { printf 'This installer requires Linux.\n' >&2; exit 1; }

case "$(uname -m)" in
    x86_64|amd64)
        asset="mihomo-linux-amd64-compatible-${version}.gz"
        archive_sha256="70d01cfb8cb7bf7a92fd1af16cb4b9553d90bb4eecde3b5c4849103e27c80ddb"
        ;;
    aarch64|arm64)
        asset="mihomo-linux-arm64-${version}.gz"
        archive_sha256="2474450cd1c41dfa53036a54a4e85579f493d3af524d86c3d4b8e2b240b56cd2"
        ;;
    i386|i486|i586|i686)
        asset="mihomo-linux-386-${version}.gz"
        archive_sha256="d1d3136bf4a8268bd3c182be976ad10747b1be5f74529ee894434742960915fe"
        ;;
    *)
        printf 'Unsupported Linux architecture: %s\n' "$(uname -m)" >&2
        exit 1
        ;;
esac

target="$install_dir/proxy-core"
if [[ -e "$target" && $force -eq 0 ]]; then
    installed_version="$($target -v 2>/dev/null || true)"
    if [[ "$installed_version" == *"$version"* ]]; then
        printf 'Mihomo %s is already installed at %s\n' "$version" "$target"
        exit 0
    fi
    printf '%s already exists; use --force to replace it.\n' "$target" >&2
    exit 1
fi

for command_name in awk chmod cp gzip mkdir mktemp mv rm; do
    command -v "$command_name" >/dev/null 2>&1 || {
        printf 'Required command not found: %s\n' "$command_name" >&2
        exit 1
    }
done

if command -v sha256sum >/dev/null 2>&1; then
    sha256_file() { sha256sum "$1" | awk '{print $1}'; }
elif command -v shasum >/dev/null 2>&1; then
    sha256_file() { shasum -a 256 "$1" | awk '{print $1}'; }
else
    printf 'Required SHA-256 command not found: sha256sum or shasum\n' >&2
    exit 1
fi

temporary_dir="$(mktemp -d "${TMPDIR:-/tmp}/gdl-mihomo.XXXXXX")"
temporary_target=""
cleanup() {
    rm -rf -- "$temporary_dir"
    [[ -z "$temporary_target" ]] || rm -f -- "$temporary_target"
}
trap cleanup EXIT INT TERM

archive="$temporary_dir/$asset"
url="https://github.com/MetaCubeX/mihomo/releases/download/${version}/${asset}"
printf 'Downloading %s\n' "$url"
if command -v curl >/dev/null 2>&1; then
    curl --fail --location --retry 3 --proto '=https' --tlsv1.2 --output "$archive" "$url"
elif command -v wget >/dev/null 2>&1; then
    wget --https-only --tries=3 --timeout=30 --output-document="$archive" "$url"
else
    printf 'Required download command not found: curl or wget\n' >&2
    exit 1
fi

actual_archive_sha256="$(sha256_file "$archive")"
if [[ "$actual_archive_sha256" != "$archive_sha256" ]]; then
    printf 'Archive SHA-256 mismatch. Expected %s, got %s.\n' \
        "$archive_sha256" "$actual_archive_sha256" >&2
    exit 1
fi

gzip -dc -- "$archive" >"$temporary_dir/mihomo"
chmod 0755 "$temporary_dir/mihomo"
version_output="$($temporary_dir/mihomo -v)"
[[ "$version_output" == *"$version"* ]] || {
    printf 'Downloaded executable reported an unexpected version: %s\n' "$version_output" >&2
    exit 1
}

mkdir -p -- "$install_dir"
temporary_target="$install_dir/.proxy-core.$$"
cp -- "$temporary_dir/mihomo" "$temporary_target"
chmod 0755 "$temporary_target"
mv -f -- "$temporary_target" "$target"
temporary_target=""

printf 'Installed %s\n' "$target"
printf 'Executable SHA-256: %s\n' "$(sha256_file "$target")"
printf '%s\n' "$version_output"
