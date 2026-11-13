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
Usage: ./install.sh [--wasm FILE] [--config-dir DIR] [--edit-config] [--claude-hooks]

  --wasm FILE       install this build of the plugin instead of building one
                    (needs Rust through rustup: https://rustup.rs)
  --config-dir DIR  Zellij's configuration directory (default: the one Zellij
                    uses, as `zellij setup --check` reports it)
  --claude-hooks    also add hooks to Claude Code's settings.json that tell the
                    plugin at once when Claude needs you or finishes (it reads
                    the screen otherwise), keeping a copy of the file as it was
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
hooks=false
while [ $# -gt 0 ]; do
    case $1 in
        --wasm) [ $# -ge 2 ] || die "--wasm needs a file"; wasm=$2; shift 2 ;;
        --wasm=*) wasm=${1#--wasm=}; shift ;;
        --config-dir) [ $# -ge 2 ] || die "--config-dir needs a directory"; config_dir=$2; shift 2 ;;
        --config-dir=*) config_dir=${1#--config-dir=}; shift ;;
        --edit-config) edit=true; shift ;;
        --claude-hooks) hooks=true; shift ;;
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
trap 'rm -f -- "$tmp" "$tmp.keys"' EXIT
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

# The plugin's keys, for every client: a plugin can bind keys only for one
# client (the one it started with), so they belong in config.kdl.
KEYS_MARK='// zellij-ai-resume: its keys (uninstall.sh --edit-config removes them)'
keys() {
    cat <<KEYS
    $KEYS_MARK
    shared_except "locked" {
        bind "Alt r" { MessagePlugin { name "zjr-reload"; payload "reload"; }; }
        bind "Alt u" { MessagePlugin { name "zjr-next"; payload "next"; }; }
        bind "Alt a" { LaunchOrFocusPlugin "$url" { floating true; move_to_focused_tab true; mode "inbox"; }; }
    }
KEYS
}

# A copy of config.kdl as it was before this run changed it (never over an
# older copy).
backup=
keep_copy() {
    [ -z "$backup" ] || return 0
    backup=$config_file.before-zellij-ai-resume
    n=1
    while [ -e "$backup" ]; do
        n=$((n + 1))
        backup=$config_file.before-zellij-ai-resume.$n
    done
    cp -p -- "$config_file" "$backup"
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
        keep_copy
        tmp=$config_file.zellij-ai-resume.$$
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

if [ -f "$config_file" ] && grep -qF "$KEYS_MARK" "$config_file"; then
    echo "$config_file already has the plugin's keys"
elif ! $edit || [ -L "$config_file" ] || [ ! -f "$config_file" ]; then
    echo "For its keys (Alt r reload, Alt u next agent, Alt a agents) in every Zellij client, add inside"
    echo "the keybinds block of $config_file:"
    echo
    keys
    echo
else
    opening=$(grep -cE '^keybinds([[:space:]].*)?[{][[:space:]]*$' "$config_file" || true)
    blocks=$(grep -cE '^keybinds([[:space:]{]|$)' "$config_file" || true)
    tmp=$config_file.zellij-ai-resume.$$
    if [ "$blocks" -eq 0 ]; then
        keep_copy
        cp -p -- "$config_file" "$tmp"
        { cat -- "$config_file"; printf '\nkeybinds {\n'; keys; printf '}\n'; } >"$tmp"
    elif [ "$blocks" -eq 1 ] && [ "$opening" -eq 1 ]; then
        keep_copy
        cp -p -- "$config_file" "$tmp"
        keys >"$tmp.keys"
        awk -v block="$tmp.keys" '{ print } !done && /^keybinds([ \t].*)?[{][ \t]*$/ {
            while ((getline line < block) > 0) print line
            done = 1
        }' "$config_file" >"$tmp"
        rm -f -- "$tmp.keys"
    fi
    if [ -f "$tmp" ]; then
        mv -f -- "$tmp" "$config_file"
        echo "Added the plugin's keys (Alt r, Alt u, Alt a) to the keybinds in $config_file (the file as it was: $backup)"
    else
        echo "$config_file has a keybinds block this script does not edit. Add, inside it:"
        keys
    fi
fi

if $hooks; then
    # Claude Code hooks that report to the plugin through `zellij pipe`.
    # Each does nothing outside Zellij, prints nothing (a UserPromptSubmit
    # hook's output would reach Claude) and always succeeds.
    settings=${CLAUDE_CONFIG_DIR:-$HOME/.claude}/settings.json
    command -v python3 >/dev/null 2>&1 || die "--claude-hooks needs python3 to edit $settings"
    [ ! -L "$settings" ] || die "$settings is a symbolic link (managed dotfiles?): add the hooks from the README to its source"
    python3 - "$settings" <<'PY' || die "could not add the hooks to $settings"
import json, os, shutil, sys

path = sys.argv[1]
def report(event):
    return ('[ -n "$ZELLIJ_PANE_ID" ] && zellij pipe --name zjr-status -- "$ZELLIJ_PANE_ID ' + event
            + '" </dev/null >/dev/null 2>&1; exit 0')
wanted = {
    # Only permission prompts and questions, not "still waiting" reminders.
    "Notification": 'case "$(cat)" in *permission_prompt*|*elicitation_dialog*) ;; *) exit 0 ;; esac; ' + report("blocked"),
    "Stop": report("done"),
    "UserPromptSubmit": report("working"),
}
settings = {}
if os.path.exists(path):
    with open(path) as handle:
        text = handle.read()
    settings = json.loads(text) if text.strip() else {}
    if not isinstance(settings, dict):
        sys.exit(f"{path} is not a JSON object")
hooks = settings.setdefault("hooks", {})
added = []
for event, command in wanted.items():
    groups = hooks.setdefault(event, [])
    if any("zjr-status" in hook.get("command", "") for group in groups for hook in group.get("hooks", [])):
        continue
    groups.append({"hooks": [{"type": "command", "command": command}]})
    added.append(event)
if not added:
    print(f"{path} already has the hooks")
    sys.exit(0)
os.makedirs(os.path.dirname(path), exist_ok=True)
if os.path.exists(path):
    shutil.copy2(path, path + ".before-zellij-ai-resume")
temporary = path + ".zellij-ai-resume"
with open(temporary, "w") as handle:
    json.dump(settings, handle, indent=2)
    handle.write("\n")
if os.path.exists(path):
    shutil.copymode(path, temporary)
os.replace(temporary, path)
print(f"Added hooks ({', '.join(added)}) to {path}" + (f" (the file as it was: {path}.before-zellij-ai-resume)" if os.path.exists(path + ".before-zellij-ai-resume") else ""))
PY
fi

cat <<EOF

Then, once, inside Zellij, grant the plugin its permissions:

    zellij plugin -c grant=true -- '$url'

and press y in the pane that opens (then close it). Sessions started or
resurrected from then on bring each Claude Code and Codex pane back on its
conversation, and name each one's tab after what it does. Alt r restarts the
focused one in place, Alt u goes to the one waiting longest for you, Alt a
lists them all.
EOF
