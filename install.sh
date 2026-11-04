#!/bin/sh
# install.sh: install the zellij-ai-resume plugin for the current user.
#
# Builds the plugin (or takes a built one with --wasm) and copies it into
# Zellij's configuration directory, under plugins/. Zellij's config.kdl is
# changed only with --edit-config; otherwise the lines to add are printed.
# Re-run to upgrade; uninstall.sh removes it.
set -eu

NAME=zellij-ai-resume.wasm

usage() {
    cat <<'EOF'
Usage: ./install.sh [--wasm FILE] [--config-dir DIR] [--edit-config]

  --wasm FILE       install this build of the plugin instead of building one
                    (needs Rust through rustup: https://rustup.rs)
  --config-dir DIR  Zellij's configuration directory (default: the one Zellij
                    uses, as `zellij setup --check` reports it)
  --edit-config     add the plugin to load_plugins in config.kdl, keeping a
                    copy of the file as it was (default: print what to add)
EOF
}

die() {
    printf 'install.sh: %s\n' "$*" >&2
    exit 1
}

wasm=
config_dir=
edit=false
while [ $# -gt 0 ]; do
    case $1 in
        --wasm) [ $# -ge 2 ] || die "--wasm needs a file"; wasm=$2; shift 2 ;;
        --wasm=*) wasm=${1#--wasm=}; shift ;;
        --config-dir) [ $# -ge 2 ] || die "--config-dir needs a directory"; config_dir=$2; shift 2 ;;
        --config-dir=*) config_dir=${1#--config-dir=}; shift ;;
        --edit-config) edit=true; shift ;;
        -h | --help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done
: "${HOME:?HOME is not set}"
here=$(cd -P -- "$(dirname -- "$0")" && pwd -P)

# Where Zellij reads its configuration, as Zellij itself works it out
# (ZELLIJ_CONFIG_DIR, ~/.config/zellij, the platform's folder, ...).
config_file=
if command -v zellij >/dev/null 2>&1; then
    version=$(zellij --version 2>/dev/null | awk '{ print $2 }')
    case $version in
        0.45.*) ;;
        *) echo "install.sh: note: the plugin is built for Zellij 0.45, this is Zellij ${version:-(unknown)}" >&2 ;;
    esac
    if [ -n "$config_dir" ]; then
        check=$(zellij --config-dir "$config_dir" setup --check 2>/dev/null || true)
    else
        check=$(zellij setup --check 2>/dev/null || true)
        config_dir=$(printf '%s\n' "$check" | sed -n 's/^\[CONFIG DIR\]: "\(.*\)"$/\1/p' | head -n 1)
    fi
    config_file=$(printf '%s\n' "$check" | sed -n 's/^\[LOOKING FOR CONFIG FILE FROM\]: "\(.*\)"$/\1/p' | head -n 1)
else
    echo "install.sh: note: zellij is not on PATH" >&2
fi
config_dir=${config_dir:-$HOME/.config/zellij}
config_dir=${config_dir%/}
config_file=${config_file:-$config_dir/config.kdl}
case $config_dir in
    /*) ;;
    *) die "--config-dir must be an absolute path: $config_dir" ;;
esac
# The path goes into a KDL string and a file: URL.
case $config_dir$config_file in
    *[!A-Za-z0-9._/+@\ -]*) die "the configuration path may contain only letters, digits, spaces and . _ / + @ -: $config_dir" ;;
esac

if [ -z "$wasm" ]; then
    [ -f "$here/plugin/Cargo.toml" ] || die "plugin/Cargo.toml not found: run this from a checkout, or pass --wasm FILE"
    command -v cargo >/dev/null 2>&1 || die "building the plugin needs Rust through rustup (https://rustup.rs); or pass --wasm FILE"
    echo "Building the plugin (rustup fetches the Rust version in plugin/rust-toolchain.toml the first time)"
    (cd "$here/plugin" && cargo build --release --target wasm32-wasip1 --quiet) || die "the build failed"
    wasm=$here/plugin/target/wasm32-wasip1/release/$NAME
fi
[ -f "$wasm" ] || die "no such file: $wasm"
[ "$(head -c 4 "$wasm" | od -An -c | tr -d ' \n')" = '\0asm' ] || die "not a WebAssembly module: $wasm"

# (a configuration directory also makes Zellij resurrect sessions at all)
mkdir -p -- "$config_dir/plugins"
target=$config_dir/plugins/$NAME
tmp=$config_dir/plugins/.$NAME.$$
trap 'rm -f -- "$tmp"' EXIT
cp -- "$wasm" "$tmp"
chmod 644 "$tmp"
mv -f -- "$tmp" "$target"
echo "Installed $target"

case $target in
    "$HOME"/*) url="file:~/${target#"$HOME"/}" ;;
    *) url="file:$target" ;;
esac
entry="    \"$url\""

show_lines() {
    printf '\n    load_plugins {\n    %s\n    }\n\n' "$entry"
    echo "(put the line inside the load_plugins block if there is one already; Zellij reads only one)"
}

loaded=$( [ -f "$config_file" ] && grep -E '^[[:space:]]*"file:[^"]*/zellij-ai-resume\.wasm"' "$config_file" | head -n 1 || true)
if [ -n "$loaded" ]; then
    if printf '%s\n' "$loaded" | grep -qF -e "\"$url\"" -e "\"file:$target\""; then
        echo "$config_file already loads it"
    else
        echo "$config_file loads another copy:$loaded"
        echo "change that line to:"
        echo "$entry"
    fi
elif ! $edit; then
    echo "To load it in every session, add to $config_file:"
    show_lines
    echo "(or run ./install.sh --edit-config to have it added)"
elif [ -L "$config_file" ]; then
    echo "$config_file is a symbolic link (managed dotfiles?), so it is left alone. Add to its source:"
    show_lines
elif [ ! -e "$config_file" ]; then
    mkdir -p -- "$(dirname -- "$config_file")"
    printf 'load_plugins {\n%s\n}\n' "$entry" >"$config_file"
    echo "Created $config_file, loading the plugin"
else
    blocks=$(grep -cE '^load_plugins([[:space:]{]|$)' "$config_file" || true)
    opening=$(grep -cE '^load_plugins[[:space:]]*[{][[:space:]]*$' "$config_file" || true)
    if [ "$blocks" -gt 1 ] || [ "$blocks" != "$opening" ]; then
        echo "$config_file has a load_plugins block this script does not edit. Add, inside it:"
        echo "$entry"
    else
        backup=$config_file.before-zellij-ai-resume
        tmp=$config_file.zellij-ai-resume.$$
        cp -p -- "$config_file" "$backup"
        cp -p -- "$config_file" "$tmp"
        if [ "$blocks" -eq 1 ]; then
            awk -v entry="$entry" '{ print } !done && /^load_plugins[ \t]*[{][ \t]*$/ { print entry; done = 1 }' "$config_file" >"$tmp"
        else
            { cat -- "$config_file"; printf '\nload_plugins {\n%s\n}\n' "$entry"; } >"$tmp"
        fi
        mv -f -- "$tmp" "$config_file"
        echo "Added it to load_plugins in $config_file (the file as it was: $backup)"
    fi
fi

cat <<EOF

Then, once, inside Zellij, grant the plugin its permissions:

    zellij plugin -- '$url'

and press y in the pane that opens (then close it). Sessions started or
resurrected from then on bring each Claude Code and Codex pane back on its
conversation; Alt r restarts the focused one in place.
EOF
