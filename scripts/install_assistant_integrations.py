#!/usr/bin/env python3
"""Install openbrain hooks + skills into user-level Claude Code, Codex, and Cursor configs.

Idempotent: re-running skips entries already present and only backs up
files it actually modifies (to <file>.bak).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INTEGRATIONS = REPO_ROOT / "integrations"
SKILLS_SRC = INTEGRATIONS / "skills"

CLAUDE_HOOKS = REPO_ROOT / "docs" / "hooks" / "claude-code-hooks.json"
CODEX_HOOKS = REPO_ROOT / "docs" / "hooks" / "codex-hooks.json"

HOME = Path.home()


def backup(path: Path) -> None:
    if path.exists():
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def hook_signature(hook: dict) -> str:
    """A value that identifies a hook entry regardless of wrapper key (prompt/command)."""
    if "prompt" in hook:
        prompt = hook["prompt"]
        text = "".join(prompt) if isinstance(prompt, list) else str(prompt)
        return text[:60]
    return json.dumps(hook, sort_keys=True)


def merge_claude_hooks() -> bool:
    settings_path = HOME / ".claude" / "settings.json"
    incoming = load_json(CLAUDE_HOOKS)
    settings = load_json(settings_path)
    settings.setdefault("hooks", {})

    changed = False
    for event, groups in incoming.items():
        existing_groups = settings["hooks"].setdefault(event, [])
        existing_sigs = {
            hook_signature(h) for g in existing_groups for h in g.get("hooks", [])
        }
        for group in groups:
            new_hooks = [
                h
                for h in group.get("hooks", [])
                if hook_signature(h) not in existing_sigs
            ]
            if not new_hooks:
                continue
            existing_groups.append({**group, "hooks": new_hooks})
            changed = True

    if changed:
        backup(settings_path)
        write_json(settings_path, settings)
    return changed


def is_openbrain_entry(entry: dict) -> bool:
    prompt = entry.get("prompt", [])
    text = "".join(prompt) if isinstance(prompt, list) else str(prompt)
    return "open brain" in text.lower() or "openbrain" in text.lower()


def merge_codex_hooks() -> bool:
    hooks_path = HOME / ".codex" / "hooks.json"
    target_path = hooks_path.resolve() if hooks_path.is_symlink() else hooks_path
    incoming = load_json(CODEX_HOOKS)
    existing = load_json(target_path)

    changed = False
    for event, entries in incoming.items():
        existing_entries = existing.setdefault(event, [])
        kept = [e for e in existing_entries if not is_openbrain_entry(e)]
        new_entries = kept + entries
        if new_entries != existing_entries:
            existing[event] = new_entries
            changed = True

    if changed:
        backup(target_path)
        write_json(target_path, existing)
    return changed


def copy_skills() -> list[str]:
    """Copy each integrations/skills/<name> dir into the user-level skills
    directory of each assistant. Returns list of skill names (re)installed."""
    targets = {
        "claude": HOME / ".claude" / "skills" / "custom-skills",
        "codex": HOME / ".codex" / "skills" / "custom-skills",
        "cursor": HOME / ".cursor" / "skills-cursor",
    }
    installed = []
    for skill_dir in sorted(SKILLS_SRC.iterdir()):
        if not skill_dir.is_dir():
            continue
        for target_root in targets.values():
            dest = target_root / skill_dir.name
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(skill_dir, dest)
        installed.append(skill_dir.name)
    return installed


def main() -> None:
    claude_changed = merge_claude_hooks()
    codex_changed = merge_codex_hooks()
    skills = copy_skills()

    print(f"Claude Code hooks: {'updated' if claude_changed else 'already up to date'}")
    print(f"Codex hooks:       {'updated' if codex_changed else 'already up to date'}")
    print(f"Skills installed/updated for claude, codex, cursor: {', '.join(skills)}")


if __name__ == "__main__":
    main()
