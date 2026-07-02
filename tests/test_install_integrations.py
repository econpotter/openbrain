"""Offline tests for scripts/install_assistant_integrations.py (issue #017).

Covers the Codex adapter's installer surface: MCP block merge (add / self-heal a
stale bearer_token_env_var / idempotent), command-type hook merge (idempotent,
preserves an unrelated project's entries in a symlinked file), and the
openbrain-entry detector for both command- and prompt-type shapes. No network,
no real ~/.codex.
"""

import importlib.util
import json
import tomllib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

_SPEC = importlib.util.spec_from_file_location(
    "install_assistant_integrations",
    Path(__file__).resolve().parent.parent
    / "scripts"
    / "install_assistant_integrations.py",
)
installer = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(installer)


class IsOpenbrainEntryTests(unittest.TestCase):
    def test_command_type_entry_detected(self):
        entry = {
            "hooks": [
                {"type": "command", "command": "openbrain capture-transcript --source codex"}
            ]
        }
        self.assertTrue(installer.is_openbrain_entry(entry))

    def test_legacy_prompt_type_entry_detected(self):
        entry = {"prompt": ["openbrain recall reminder: ..."]}
        self.assertTrue(installer.is_openbrain_entry(entry))

    def test_unrelated_entry_not_detected(self):
        entry = {"hooks": [{"type": "command", "command": "./scripts/other.sh"}]}
        self.assertFalse(installer.is_openbrain_entry(entry))


class MergeCodexMcpTests(unittest.TestCase):
    def _run(self, tmp, toml_text=None):
        home = Path(tmp)
        (home / ".codex").mkdir(parents=True)
        cfg = home / ".codex" / "config.toml"
        if toml_text is not None:
            cfg.write_text(toml_text)
        with patch.object(installer, "HOME", home):
            with patch.dict("os.environ", {}, clear=False):
                result = installer.merge_codex_mcp()
        return cfg, result

    def test_adds_block_when_absent(self):
        with TemporaryDirectory() as tmp:
            cfg, result = self._run(tmp, toml_text='model = "gpt-5.5"\n')
            self.assertEqual(result, "added")
            parsed = tomllib.loads(cfg.read_text())
            self.assertEqual(
                parsed["mcp_servers"]["openbrain"]["bearer_token_env_var"],
                "OPENBRAIN_MCP_KEY",
            )
            self.assertEqual(parsed["model"], "gpt-5.5")  # untouched

    def test_creates_config_when_missing(self):
        with TemporaryDirectory() as tmp:
            cfg, result = self._run(tmp, toml_text=None)
            self.assertEqual(result, "added")
            self.assertTrue(cfg.exists())

    def test_heals_stale_bearer_env_var_in_place(self):
        text = (
            'model = "gpt-5.5"\n\n'
            "[mcp_servers.openbrain]\n"
            'url = "https://potter.example:8443/mcp"\n'
            'bearer_token_env_var = "MCP_ACCESS_KEY"\n\n'
            '[projects."/home/x"]\n'
            'trust_level = "trusted"\n'
        )
        with TemporaryDirectory() as tmp:
            cfg, result = self._run(tmp, toml_text=text)
            self.assertEqual(result, "fixed")
            parsed = tomllib.loads(cfg.read_text())
            ob = parsed["mcp_servers"]["openbrain"]
            self.assertEqual(ob["bearer_token_env_var"], "OPENBRAIN_MCP_KEY")
            # url and the unrelated table are preserved.
            self.assertEqual(ob["url"], "https://potter.example:8443/mcp")
            self.assertEqual(parsed["projects"]["/home/x"]["trust_level"], "trusted")
            self.assertTrue(cfg.with_suffix(".toml.bak").exists())

    def test_idempotent_when_already_correct(self):
        text = (
            "[mcp_servers.openbrain]\n"
            'url = "https://x:8443/mcp"\n'
            'bearer_token_env_var = "OPENBRAIN_MCP_KEY"\n'
        )
        with TemporaryDirectory() as tmp:
            cfg, result = self._run(tmp, toml_text=text)
            self.assertEqual(result, "unchanged")
            self.assertFalse(cfg.with_suffix(".toml.bak").exists())


class MergeCodexHooksTests(unittest.TestCase):
    def _merge(self, home):
        with patch.object(installer, "HOME", home):
            return installer.merge_codex_hooks()

    def test_merge_is_idempotent_and_command_type(self):
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".codex").mkdir(parents=True)
            self.assertTrue(self._merge(home))
            data = json.loads((home / ".codex" / "hooks.json").read_text())
            cmds = [
                h["command"]
                for entry in data["hooks"]["Stop"]
                for h in entry["hooks"]
            ]
            self.assertTrue(any("--source codex" in c for c in cmds))
            # Second run: no change, no duplicate openbrain entries.
            self.assertFalse(self._merge(home))
            data2 = json.loads((home / ".codex" / "hooks.json").read_text())
            self.assertEqual(len(data2["hooks"]["Stop"]), 1)

    def test_sweeps_out_own_legacy_prompt_entries(self):
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".codex").mkdir(parents=True)
            # An older install's dead prompt-type entries live at legacy
            # top-level keys, alongside an unrelated project's entry.
            legacy = {
                "session_start": [
                    {"prompt": ["openbrain recall reminder: ..."]},
                    {"prompt": ["their unrelated reminder"]},
                ],
                "session_end": [{"prompt": ["openbrain session-end reminder: ..."]}],
            }
            (home / ".codex" / "hooks.json").write_text(json.dumps(legacy))
            self.assertTrue(self._merge(home))
            data = json.loads((home / ".codex" / "hooks.json").read_text())
            # Our legacy prompt entries are gone; the unrelated one survives.
            self.assertEqual(data["session_start"], [{"prompt": ["their unrelated reminder"]}])
            self.assertNotIn("session_end", data)  # became empty -> dropped
            # And the live command-type hooks are now installed under "hooks".
            self.assertIn("SessionStart", data["hooks"])

    def test_preserves_unrelated_entries_in_symlinked_file(self):
        with TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            other = Path(tmp) / "other-project" / ".codex"
            other.mkdir(parents=True)
            (home / ".codex").mkdir(parents=True)
            # Unrelated project's own hooks — both a nested "hooks" entry and a
            # legacy top-level key — must survive untouched.
            unrelated = {
                "hooks": {
                    "Stop": [
                        {"hooks": [{"type": "command", "command": "./their_stop.sh"}]}
                    ]
                },
                "session_start": [{"prompt": ["their reminder"]}],
            }
            real = other / "hooks.json"
            real.write_text(json.dumps(unrelated))
            (home / ".codex" / "hooks.json").symlink_to(real)

            self.assertTrue(self._merge(home))
            data = json.loads(real.read_text())  # written through the symlink
            stop_cmds = [
                h["command"] for entry in data["hooks"]["Stop"] for h in entry["hooks"]
            ]
            self.assertIn("./their_stop.sh", stop_cmds)  # unrelated kept
            self.assertTrue(any("openbrain" in c for c in stop_cmds))  # ours added
            self.assertEqual(data["session_start"], [{"prompt": ["their reminder"]}])


if __name__ == "__main__":
    unittest.main()
