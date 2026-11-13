#!/bin/sh
# uninstall.sh: remove what install.sh installed. --edit-config also takes
# the plugin's line out of config.kdl and its hooks out of Claude Code's
# settings.json (otherwise it says what to remove); --purge also deletes the
# saved pane maps.
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

# The keys install.sh --edit-config added to config.kdl, from their mark to
# the end of their block.
KEYS_MARK='// zellij-ai-resume: its keys (uninstall.sh --edit-config removes them)'
if [ -f "$config_file" ] && grep -qF "$KEYS_MARK" "$config_file"; then
    if $edit && [ ! -L "$config_file" ]; then
        backup=$config_file.before-zellij-ai-resume-uninstall
        [ -e "$backup" ] || cp -p -- "$config_file" "$backup"
        tmp=$config_file.zellij-ai-resume.$$
        trap 'rm -f -- "$tmp"' EXIT
        cp -p -- "$config_file" "$tmp"
        awk -v mark="$KEYS_MARK" '$0 == "    " mark { skipping = 1; next }
            skipping { if ($0 ~ /^    }[ \t]*$/) skipping = 0; next }
            { print }' "$config_file" >"$tmp"
        mv -f -- "$tmp" "$config_file"
        echo "removed the plugin's keys from $config_file"
    else
        echo "remove the plugin's keys from the keybinds in $config_file (the block after \"$KEYS_MARK\")"
    fi
fi

# Claude Code hooks added by install.sh --claude-hooks (they report to the
# plugin, and do nothing without it).
settings=${CLAUDE_CONFIG_DIR:-$HOME/.claude}/settings.json
if [ -f "$settings" ] && grep -q 'zjr-status' "$settings"; then
    if $edit && [ ! -L "$settings" ] && command -v python3 >/dev/null 2>&1; then
        python3 - "$settings" <<'PY' && echo "removed the zellij-ai-resume hooks from $settings (the file as it was: $settings.before-zellij-ai-resume-uninstall)"
import json, os, shutil, sys

path = sys.argv[1]
with open(path) as handle:
    settings = json.load(handle)
hooks = settings.get("hooks", {})
for event in list(hooks):
    groups = []
    for group in hooks[event]:
        kept = [hook for hook in group.get("hooks", []) if "zjr-status" not in hook.get("command", "")]
        if kept:
            groups.append(dict(group, hooks=kept))
    if groups:
        hooks[event] = groups
    else:
        del hooks[event]
if not hooks:
    settings.pop("hooks", None)
shutil.copy2(path, path + ".before-zellij-ai-resume-uninstall")
temporary = path + ".zellij-ai-resume"
with open(temporary, "w") as handle:
    json.dump(settings, handle, indent=2)
    handle.write("\n")
shutil.copymode(path, temporary)
os.replace(temporary, path)
PY
    else
        echo "$settings has zellij-ai-resume hooks (commands with zjr-status): --edit-config removes them"
    fi
fi

if $purge && [ -n "$cache_dir" ] && [ -d "$cache_dir" ]; then
    # The plugin's own cache folder, where it keeps one map per session.
    find "$cache_dir" -type d -name plugin_cache -path "*$NAME*" 2>/dev/null | while IFS= read -r dir; do
        rm -f -- "$dir"/session-*.json "$dir"/agents-*.json
        echo "removed the saved pane maps in $dir"
    done
fi
