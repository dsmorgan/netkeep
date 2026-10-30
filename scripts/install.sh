#!/bin/sh
# netkeep's one-line installer (ADR 0011 rule 5; #686). Each GitHub release carries a
# copy with its version stamped in:
#
#   curl -fsSL https://github.com/dsmorgan/netkeep/releases/download/vX.Y.Z/install.sh | sh
#
# It installs uv into ~/.local/bin if it's missing (uv's own installer, pinned by
# version and SHA-256), downloads this release's constraints file and checks it against
# the release's SHA256SUMS, installs netkeep==X.Y.Z as a uv tool with those constraints
# and uv's own Python 3.12, then runs `netkeep setup`. It never uses sudo and never
# writes outside your home directory. It prints each step before it runs it;
# `sh install.sh --dry-run` prints the steps and runs none of them.
#
# The whole script is one function, called on the last line, so a download cut short
# defines nothing it can run and does nothing.
#
# In the repository, the version below is a placeholder: the release workflow stamps it
# (.github/workflows/release.yml), and an unstamped copy refuses to run.

netkeep_install() {
    set -eu

    # --- Pins ---------------------------------------------------------------------------
    netkeep_version='@NETKEEP_VERSION@'
    netkeep_repo='dsmorgan/netkeep'
    uv_version='0.12.23'
    uv_installer_sha256='b8e6c43099ee9f9a550984d3ad56948457c689e7a99c090b35377234ac241491'
    docs_url="https://github.com/$netkeep_repo#installing"

    release_url="https://github.com/$netkeep_repo/releases/download/v$netkeep_version"
    uv_installer_url="https://github.com/astral-sh/uv/releases/download/$uv_version/uv-installer.sh"

    say() { printf '%s\n' "netkeep install: $*"; }
    step() { printf '\n%s\n' "==> $*"; }
    die() {
        printf '%s\n' "netkeep install: error: $*" >&2
        exit 1
    }
    fetch() {
        # HTTPS only, including on redirects; fail on an HTTP error instead of saving it.
        curl --proto '=https' --tlsv1.2 -fsSL -o "$2" "$1" || die "could not download $1"
    }
    sha256_of() {
        shasum -a 256 "$1" | awk '{ print $1 }'
    }

    # --- Arguments ----------------------------------------------------------------------
    dry_run=0
    for arg in "$@"; do
        case $arg in
            --dry-run) dry_run=1 ;;
            -h | --help)
                printf '%s\n' "Usage: sh install.sh [--dry-run]" \
                    "Installs netkeep $netkeep_version, and uv if it's missing, then runs 'netkeep setup'." \
                    "  --dry-run  print the steps without running them"
                return 0
                ;;
            *) die "unknown option: $arg (try --help)" ;;
        esac
    done

    # --- Checks that need nothing installed ---------------------------------------------
    case $netkeep_version in
        '' | *[!0-9.]* | .* | *. | *..*)
            die "this copy of install.sh has no release version stamped in. Download it from a release: https://github.com/$netkeep_repo/releases"
            ;;
    esac
    os=$(uname -s)
    if [ "$os" != Darwin ]; then
        die "this installer supports macOS only, and this is $os. See $docs_url for other ways to install."
    fi
    if [ "$(id -u)" = 0 ]; then
        die "don't run this installer as root or with sudo: it installs into your own home directory."
    fi
    if [ -z "${HOME:-}" ] || [ "$HOME" = / ] || [ ! -d "$HOME" ]; then
        die "HOME must be your home directory, and it is '${HOME:-}'."
    fi

    # The caller's PATH, unchanged: uv's installer adds its folder to your shell profile
    # only when that folder isn't on PATH, and the hint at the end checks the same PATH.
    # Commands this script installs are called by their full path instead.
    caller_path=$PATH
    local_bin="$HOME/.local/bin"

    if [ "$dry_run" = 1 ]; then
        say "dry run: these are the steps; nothing is downloaded, installed or run."
    fi
    say "installing netkeep $netkeep_version from $release_url"

    # Downloads go to a folder under HOME, removed on exit.
    work=''
    if [ "$dry_run" = 0 ]; then
        work=$(mktemp -d "$HOME/.netkeep-install.XXXXXX") || die "could not create a temporary folder in $HOME"
        trap 'rm -rf "$work"' EXIT
        trap 'exit 1' HUP INT TERM
    fi

    # --- 1. uv ----------------------------------------------------------------------------
    uv=$(command -v uv 2>/dev/null) || uv=''
    if [ -z "$uv" ] && [ -x "$local_bin/uv" ]; then
        uv="$local_bin/uv"
    fi
    if [ -n "$uv" ]; then
        step "1. uv is already installed: $uv"
    else
        step "1. Install uv $uv_version into $local_bin (uv's installer, checked against its pinned SHA-256)"
        say "download $uv_installer_url"
        say "check its SHA-256 is $uv_installer_sha256"
        say "run it with UV_INSTALL_DIR=$local_bin"
        if [ "$dry_run" = 0 ]; then
            fetch "$uv_installer_url" "$work/uv-installer.sh"
            actual=$(sha256_of "$work/uv-installer.sh")
            if [ "$actual" != "$uv_installer_sha256" ]; then
                die "uv's installer has SHA-256 $actual, not the pinned $uv_installer_sha256. Nothing was installed."
            fi
            # Only the pinned download location and the install folder: environment
            # variables that redirect uv's installer elsewhere, send it a token, or have
            # it write to a file outside HOME are cleared for it.
            (
                unset UV_DOWNLOAD_URL INSTALLER_DOWNLOAD_URL UV_INSTALLER_GHE_BASE_URL \
                    UV_INSTALLER_GITHUB_BASE_URL CARGO_DIST_FORCE_INSTALL_DIR \
                    UV_UNMANAGED_INSTALL UV_GITHUB_TOKEN GITHUB_PATH
                PATH=$caller_path UV_INSTALL_DIR="$local_bin" sh "$work/uv-installer.sh"
            ) || die "uv's installer failed."
            [ -x "$local_bin/uv" ] || die "uv's installer finished, but there is no uv in $local_bin."
        fi
        uv="$local_bin/uv"
    fi

    # --- 2. The release's constraints file ------------------------------------------------
    step "2. Download this release's constraints.txt and SHA256SUMS, and check the checksum"
    say "download $release_url/constraints.txt"
    say "download $release_url/SHA256SUMS"
    if [ "$dry_run" = 0 ]; then
        fetch "$release_url/constraints.txt" "$work/constraints.txt"
        fetch "$release_url/SHA256SUMS" "$work/SHA256SUMS"
        expected=$(awk '$2 == "constraints.txt" || $2 == "*constraints.txt" { print $1 }' "$work/SHA256SUMS")
        case $expected in
            '' | *[!0-9a-f]*) die "SHA256SUMS has no single checksum for constraints.txt. Nothing was installed." ;;
        esac
        [ "${#expected}" = 64 ] || die "SHA256SUMS has no valid checksum for constraints.txt. Nothing was installed."
        actual=$(sha256_of "$work/constraints.txt")
        if [ "$actual" != "$expected" ]; then
            die "constraints.txt has SHA-256 $actual, but SHA256SUMS says $expected. Nothing was installed."
        fi
        say "constraints.txt matches SHA256SUMS ($actual)"
    fi

    # --- 3. netkeep -----------------------------------------------------------------------
    constraints="${work:-<download folder>}/constraints.txt"
    step "3. Install netkeep $netkeep_version as a uv tool, with uv's own Python 3.12"
    say "uv tool install --python 3.12 --managed-python -c $constraints netkeep==$netkeep_version"
    if [ "$dry_run" = 0 ]; then
        "$uv" tool install --python 3.12 --managed-python -c "$constraints" "netkeep==$netkeep_version" ||
            die "uv tool install failed. If your uv is old, update it, then run this again: \"$uv\" self update"
    fi

    # --- 4. netkeep setup -----------------------------------------------------------------
    step "4. Run netkeep setup"
    if [ "$dry_run" = 1 ]; then
        say "netkeep setup"
        say "dry run: done. Run it again without --dry-run to install."
        return 0
    fi
    bin_dir=$("$uv" tool dir --bin) || die "could not find uv's tool folder ('uv tool dir --bin' failed)."
    netkeep="$bin_dir/netkeep"
    [ -x "$netkeep" ] || die "netkeep was installed, but $netkeep isn't there."
    if "$netkeep" setup --help >/dev/null 2>&1; then
        say "$netkeep setup"
        "$netkeep" setup || die "netkeep setup failed. netkeep is installed; run '$netkeep setup' again when you've fixed the problem above."
    else
        say "netkeep $netkeep_version has no 'netkeep setup' command, so the installer skips it."
        say "Start netkeep with '$netkeep serve' and open http://127.0.0.1:8000 in Chrome."
    fi

    step "Done: netkeep $netkeep_version is installed at $netkeep"
    case ":$caller_path:" in
        *":$bin_dir:"*) ;;
        *)
            say "$bin_dir isn't on this terminal's PATH. Open a new terminal; if 'netkeep' still isn't found, run:"
            say "  '$uv' tool update-shell"
            say "or add this line to your shell profile (~/.zshrc for zsh):"
            say "  export PATH=\"$bin_dir:\$PATH\""
            ;;
    esac
}

{ netkeep_install "$@"; }
