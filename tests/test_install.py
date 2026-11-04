"""install.sh and uninstall.sh, without Zellij: where the plugin goes, and
what they do (and refuse to do) to config.kdl.

    python3 -m unittest test_install"""

import os
import subprocess
import unittest

from helpers import ROOT, Sandbox

LINE = '    "file:~/.config/zellij/plugins/zellij-ai-resume.wasm"'
CONFIG = """// my settings
keybinds clear-defaults=true {
    normal {
        bind "Alt n" { NewPane; }
    }
}

// Plugins to load in the background when a new session starts
load_plugins {
}
theme "dracula"
"""


class InstallTest(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.close)
        self.wasm = os.path.join(self.box.dir, "plugin.wasm")
        with open(self.wasm, "wb") as handle:
            handle.write(b"\0asm\x01\0\0\0")
        self.config_dir = os.path.join(self.box.home, ".config", "zellij")
        self.config = os.path.join(self.config_dir, "config.kdl")
        self.installed = os.path.join(self.config_dir, "plugins", "zellij-ai-resume.wasm")

    def run_script(self, name, *args, ok=True):
        # No zellij on PATH: the scripts fall back to ~/.config/zellij.
        result = subprocess.run(["/bin/sh", os.path.join(ROOT, name), *args], env=self.box.env, capture_output=True,
                                text=True, cwd=self.box.work)
        self.assertEqual(result.returncode == 0, ok, result.stdout + result.stderr)
        return result.stdout + result.stderr

    def install(self, *args, ok=True):
        return self.run_script("install.sh", "--wasm", self.wasm, *args, ok=ok)

    def write_config(self, text):
        os.makedirs(self.config_dir, exist_ok=True)
        with open(self.config, "w") as handle:
            handle.write(text)

    def read_config(self):
        with open(self.config) as handle:
            return handle.read()

    def test_by_default_it_prints_the_lines_and_leaves_the_config_alone(self):
        self.write_config(CONFIG)
        output = self.install()
        with open(self.installed, "rb") as handle:
            self.assertEqual(handle.read(), b"\0asm\x01\0\0\0")
        self.assertEqual(oct(os.stat(self.installed).st_mode & 0o777), "0o644")
        self.assertEqual(self.read_config(), CONFIG)
        self.assertIn(LINE, output)
        self.assertIn("zellij plugin -- 'file:~/.config/zellij/plugins/zellij-ai-resume.wasm'", output)

    def test_edit_config_fills_the_existing_block_once(self):
        self.write_config(CONFIG)
        self.install("--edit-config")
        self.assertEqual(self.read_config(), CONFIG.replace("load_plugins {\n", "load_plugins {\n" + LINE + "\n"))
        with open(self.config + ".before-zellij-ai-resume") as handle:
            self.assertEqual(handle.read(), CONFIG)
        edited = self.read_config()
        self.assertIn("already loads it", self.install("--edit-config"))
        self.assertEqual(self.read_config(), edited)

    def test_edit_config_adds_a_block_or_a_file(self):
        self.write_config('theme "dracula"\n')
        self.install("--edit-config")
        self.assertEqual(self.read_config(), f'theme "dracula"\n\nload_plugins {{\n{LINE}\n}}\n')
        os.unlink(self.config)
        self.install("--edit-config")
        self.assertEqual(self.read_config(), f"load_plugins {{\n{LINE}\n}}\n")

    def test_configs_it_cannot_edit_safely_are_left_alone(self):
        for text in ('load_plugins { "file:/x.wasm"; }\n', "load_plugins {\n}\nload_plugins {\n}\n"):
            self.write_config(text)
            output = self.install("--edit-config")
            self.assertEqual(self.read_config(), text)
            self.assertIn(LINE, output)
        os.unlink(self.config)
        source = os.path.join(self.box.dir, "dotfiles-config.kdl")
        with open(source, "w") as handle:
            handle.write(CONFIG)
        os.symlink(source, self.config)
        self.assertIn("symbolic link", self.install("--edit-config"))
        self.assertEqual(self.read_config(), CONFIG)

    def test_paths_outside_home_are_absolute(self):
        elsewhere = os.path.join(self.box.dir, "zellij")
        output = self.install("--config-dir", elsewhere)
        self.assertIn(f'"file:{elsewhere}/plugins/zellij-ai-resume.wasm"', output)

    def test_bad_input_is_refused(self):
        self.assertIn("not a WebAssembly module", self.install("--wasm", os.path.join(ROOT, "README.md"), ok=False))
        self.assertIn("may contain only", self.install("--config-dir", os.path.join(self.box.dir, 'a"b'), ok=False))
        self.assertIn("absolute", self.install("--config-dir", "relative/dir", ok=False))
        self.assertFalse(os.path.exists(self.installed))

    def test_uninstall(self):
        self.write_config(CONFIG)
        self.install("--edit-config")
        output = self.run_script("uninstall.sh")
        self.assertFalse(os.path.exists(self.installed))
        self.assertIn(LINE, output)  # says which line to remove
        self.assertIn(LINE, self.read_config())
        self.run_script("uninstall.sh", "--edit-config")
        self.assertEqual(self.read_config(), CONFIG)


if __name__ == "__main__":
    unittest.main()
