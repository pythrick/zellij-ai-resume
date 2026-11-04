#!/bin/sh
# uninstall.sh: remove what install.sh installed. --edit-config also takes
# the plugin's line out of config.kdl (otherwise it says which line to
# remove); --purge also deletes the saved pane maps.
#
# Sessions resurrected afterwards still come back, with each pane's command
# waiting for Enter as Zellij does by default.
set -eu

NAME=zellij-ai-resume.wasm

die() {
    printf 'uninstall.sh: %s\n' "$*" >&2
    exit 1
}

usage() {
    echo "Usage: ./uninstall.sh [--config-dir DIR] [--edit-config] [--purge]"
}

config_dir=
edit=false
purge=false
while [ $# -gt 0 ]; do
    case $1 in
        --config-dir) [ $# -ge 2 ] || die "--config-dir needs a directory"; config_dir=$2; shift 2 ;;
        --config-dir=*) config_dir=${1#--config-dir=}; shift ;;
        --edit-config) edit=true; shift ;;
        --purge) purge=true; shift ;;
        -h | --help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done
: "${HOME:?HOME is not set}"

config_file=
cache_dir=
if command -v zellij >/dev/null 2>&1; then
    if [ -n "$config_dir" ]; then
        check=$(zellij --config-dir "$config_dir" setup --check 2>/dev/null || true)
    else
        check=$(zellij setup --check 2>/dev/null || true)
        config_dir=$(printf '%s\n' "$check" | sed -n 's/^\[CONFIG DIR\]: "\(.*\)"$/\1/p' | head -n 1)
    fi
    config_file=$(printf '%s\n' "$check" | sed -n 's/^\[LOOKING FOR CONFIG FILE FROM\]: "\(.*\)"$/\1/p' | head -n 1)
    cache_dir=$(printf '%s\n' "$check" | sed -n 's/^\[CACHE DIR\]: \(.*\)$/\1/p' | head -n 1)
fi
config_dir=${config_dir:-$HOME/.config/zellij}
config_dir=${config_dir%/}
config_file=${config_file:-$config_dir/config.kdl}

target=$config_dir/plugins/$NAME
if [ -f "$target" ]; then
    rm -f -- "$target"
    echo "removed $target"
    rmdir -- "$config_dir/plugins" 2>/dev/null || true
fi

pattern='^[[:space:]]*"file:[^"]*/zellij-ai-resume\.wasm"[[:space:]]*$'
if [ -f "$config_file" ] && grep -qE "$pattern" "$config_file"; then
    if $edit && [ ! -L "$config_file" ]; then
        backup=$config_file.before-zellij-ai-resume-uninstall
        tmp=$config_file.zellij-ai-resume.$$
        trap 'rm -f -- "$tmp"' EXIT
        cp -p -- "$config_file" "$backup"
        cp -p -- "$config_file" "$tmp"
        grep -vE "$pattern" "$config_file" >"$tmp" || true
        mv -f -- "$tmp" "$config_file"
        echo "removed the plugin from $config_file (the file as it was: $backup)"
    else
        echo "remove this line from $config_file:"
        grep -E "$pattern" "$config_file"
    fi
elif [ -f "$config_file" ] && grep -q "$NAME" "$config_file"; then
    echo "$config_file still mentions $NAME:"
    grep -n "$NAME" "$config_file"
fi

if $purge && [ -n "$cache_dir" ] && [ -d "$cache_dir" ]; then
    # The plugin's own cache folder, where it keeps one map per session.
    find "$cache_dir" -type d -name plugin_cache -path "*$NAME*" 2>/dev/null | while IFS= read -r dir; do
        rm -f -- "$dir"/session-*.json
        echo "removed the saved pane maps in $dir"
    done
fi
