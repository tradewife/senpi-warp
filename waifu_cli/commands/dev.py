"""
dev.py — Development tools for skill management.

Usage:
    waifu dev list-skills     Show installable skill catalog
    waifu dev add-skill       Install a skill from the catalog
    waifu dev create-skill    Scaffold a new custom skill
    waifu dev show-skill      Display a skill's SKILL.md
"""

import sys
import json
import shutil
import subprocess
from pathlib import Path
from datetime import datetime, timezone

import click

PROJECT_ROOT = Path(__file__).parent.parent.parent
SKILLS_DIR = PROJECT_ROOT / "senpi-skills"
CONFIG_DIR = PROJECT_ROOT / "config"
CATALOG_PATH = SKILLS_DIR / "catalog.json"


def _load_catalog():
    """Load and return the skills catalog."""
    if not CATALOG_PATH.exists():
        click.echo("❌ Catalog not found at senpi-skills/catalog.json")
        click.echo("   Run: git submodule update --init")
        sys.exit(1)
    return json.loads(CATALOG_PATH.read_text())


def _find_skill(catalog, name):
    """Find a skill entry by id or name (case-insensitive)."""
    name_lower = name.lower()
    for skill in catalog["skills"]:
        if skill["id"] == name_lower or skill["name"].lower() == name_lower:
            return skill
    return None


def _is_configured(skill_id):
    """Check if a skill has a config file in the waifu config/ dir."""
    return (CONFIG_DIR / f"{skill_id}-config.json").exists()


def _is_installed(skill_id):
    """Check if the skill directory with SKILL.md exists locally."""
    return (SKILLS_DIR / skill_id / "SKILL.md").exists()


def _parse_frontmatter(skill_path):
    """Parse YAML-ish frontmatter from a SKILL.md file."""
    text = (skill_path / "SKILL.md").read_text()
    if not text.startswith("---"):
        return {}
    end = text.index("---", 3)
    block = text[3:end].strip()
    meta = {}
    current_key = None
    for line in block.splitlines():
        if line.startswith("  ") and current_key:
            # continuation of multi-line value
            meta[current_key] = meta[current_key] + " " + line.strip()
        elif ":" in line:
            key, _, val = line.partition(":")
            key = key.strip()
            val = val.strip()
            if val == ">-":
                meta[key] = ""
                current_key = key
            else:
                meta[key] = val.strip('"').strip("'")
                current_key = key
    return meta


RISK_COLORS = {
    "moderate": "yellow",
    "aggressive": "red",
    "conservative": "green",
}


@click.group()
def dev():
    """Development tools — manage skills and extensions."""


@dev.command("list-skills")
def list_skills():
    """Show the installable skill catalog, grouped by category."""
    catalog = _load_catalog()
    groups = {g["id"]: g for g in catalog["groups"]}

    # Group skills
    by_group = {}
    for skill in sorted(catalog["skills"], key=lambda s: s.get("sort_order", 99)):
        by_group.setdefault(skill["group"], []).append(skill)

    click.echo(f"\n{'=' * 60}")
    click.echo("  🐾 SENPI SKILL CATALOG")
    click.echo(f"{'=' * 60}")

    for group_id in [g["id"] for g in catalog["groups"]]:
        if group_id not in by_group:
            continue
        group_meta = groups[group_id]
        click.echo(f"\n{group_meta['emoji']}  {group_meta['label']}")
        click.echo(f"{'─' * 50}")

        for skill in by_group[group_id]:
            installed = _is_installed(skill["id"])
            configured = _is_configured(skill["id"])

            if configured:
                badge = "✅ configured"
            elif installed:
                badge = "📦 installed"
            else:
                badge = "  available"

            risk = skill.get("risk_level", "moderate")
            risk_str = click.style(risk, fg=RISK_COLORS.get(risk, "white"))

            click.echo(f"  {skill['emoji']}  {skill['name']:<16} [{badge}]")
            click.echo(f"     {skill['tagline']}")
            click.echo(
                f"     Risk: {risk_str}  |  "
                f"Min budget: ${skill['min_budget']}  |  "
                f"ID: {skill['id']}"
            )
            if skill.get("base_skill"):
                click.echo(f"     Variant of: {skill['base_skill']}")

    total = len(catalog["skills"])
    configured = sum(1 for s in catalog["skills"] if _is_configured(s["id"]))
    click.echo(f"\n{'─' * 50}")
    click.echo(f"  {total} skills total, {configured} configured")
    click.echo(f"{'=' * 60}\n")


@dev.command("add-skill")
@click.argument("name")
def add_skill(name):
    """Install a skill from the catalog."""
    catalog = _load_catalog()
    skill = _find_skill(catalog, name)
    if not skill:
        click.echo(f"❌ Skill '{name}' not found in catalog.")
        click.echo("   Run: waifu dev list-skills")
        sys.exit(1)

    skill_id = skill["id"]
    skill_dir = SKILLS_DIR / skill_id

    click.echo(f"\n{skill['emoji']}  Installing {skill['name']} ({skill_id})...")

    # Update senpi-skills repo
    click.echo("   Pulling latest senpi-skills...")
    result = subprocess.run(
        ["git", "-C", str(SKILLS_DIR), "pull"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        click.echo(f"   ⚠️  git pull failed: {result.stderr.strip()}")

    # Check branch
    branch = skill.get("branch", "main")
    if branch and branch != "main":
        click.echo(f"   Checking out branch '{branch}'...")
        subprocess.run(
            ["git", "-C", str(SKILLS_DIR), "checkout", branch],
            capture_output=True,
            text=True,
        )

    # Verify SKILL.md exists
    if not (skill_dir / "SKILL.md").exists():
        click.echo(f"❌ SKILL.md not found at {skill_dir}/SKILL.md")
        click.echo("   The skill directory may not exist on this branch.")
        sys.exit(1)

    # Copy config if available and not already present
    skill_config = skill_dir / "config" / f"{skill_id}-config.json"
    if not skill_config.exists():
        # Try without the id prefix
        configs = (
            list((skill_dir / "config").glob("*-config.json"))
            if (skill_dir / "config").exists()
            else []
        )
        if configs:
            skill_config = configs[0]

    dest_config = CONFIG_DIR / f"{skill_id}-config.json"
    if skill_config.exists() and not dest_config.exists():
        shutil.copy2(skill_config, dest_config)
        click.echo(f"   📋 Copied config → config/{skill_id}-config.json")
    elif dest_config.exists():
        click.echo(f"   📋 Config already exists at config/{skill_id}-config.json")
    else:
        click.echo(f"   ℹ️  No default config found in skill directory")

    # Read and display frontmatter
    meta = _parse_frontmatter(skill_dir)
    if meta:
        click.echo(f"\n   Skill metadata:")
        for key in ("name", "description", "version"):
            val = meta.get(key) or meta.get("metadata", {})
            if isinstance(val, str) and val:
                display = val[:100] + "..." if len(val) > 100 else val
                click.echo(f"     {key}: {display}")
        # Check nested metadata for version
        if "metadata" in meta and not meta.get("version"):
            click.echo(f"     version: {meta.get('metadata', '')}")

    click.echo(f"\n✅ {skill['name']} installed successfully!")
    click.echo(f"\n   Next steps:")
    click.echo(f"   1. Review the skill:  waifu dev show-skill {skill_id}")
    click.echo(f"   2. Edit config:       $EDITOR config/{skill_id}-config.json")
    click.echo(f"   3. Register in wolf-strategies.json if needed")
    click.echo()


@dev.command("create-skill")
@click.argument("name")
def create_skill(name):
    """Scaffold a new custom skill under senpi-skills/."""
    skill_dir = SKILLS_DIR / name

    if skill_dir.exists():
        click.echo(f"❌ Directory senpi-skills/{name}/ already exists.")
        sys.exit(1)

    click.echo(f"\n🔨 Scaffolding new skill: {name}")

    # Create directories
    skill_dir.mkdir(parents=True)
    (skill_dir / "scripts").mkdir()
    (skill_dir / "config").mkdir()

    # Create SKILL.md with frontmatter template
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    skill_md = f"""---
name: {name}
description: >-
  TODO: Describe what this skill does, its edge type, and key parameters.
license: MIT
metadata:
  author: waifu
  version: "1.0"
  platform: senpi
  exchange: hyperliquid
  created: "{now}"
---

# {name.upper()} — Custom Skill

TODO: Describe the strategy, entry logic, and exit geometry.

---

## Entry Logic

TODO: Define scanner conditions, score thresholds, and filters.

## Exit Logic

TODO: Define DSL mode, trailing stop tiers, and stagnation TP rules.

## Configuration

See `config/{name}-config.json` for tunable parameters.

## Risk Constraints

- Leverage: 7-10x only
- Max positions: per wolf-strategies.json
- 4H trend alignment: HARD gate
"""

    (skill_dir / "SKILL.md").write_text(skill_md)

    # Create empty scanner script
    scanner_py = f"""\"\"\"
{name}_scanner.py — Scanner for {name} skill.

TODO: Implement scan logic.
\"\"\"


def scan():
    \"\"\"Run one scan cycle. Returns list of signal dicts or empty list.\"\"\"
    return []


if __name__ == "__main__":
    signals = scan()
    print(f"{{len(signals)}} signals found")
"""
    (skill_dir / "scripts" / f"{name}_scanner.py").write_text(scanner_py)

    # Create default config
    default_config = {
        "skillId": name,
        "enabled": False,
        "entryScoreMin": 7,
        "leverage": {"min": 7, "max": 10},
        "notes": "Scaffolded by waifu dev create-skill",
    }
    (skill_dir / "config" / f"{name}-config.json").write_text(
        json.dumps(default_config, indent=2) + "\n"
    )

    click.echo(f"   Created senpi-skills/{name}/SKILL.md")
    click.echo(f"   Created senpi-skills/{name}/scripts/{name}_scanner.py")
    click.echo(f"   Created senpi-skills/{name}/config/{name}-config.json")

    click.echo(f"\n✅ Skill '{name}' scaffolded!")
    click.echo(f"\n   Next steps:")
    click.echo(f"   1. Edit SKILL.md with your strategy spec")
    click.echo(f"   2. Implement scripts/{name}_scanner.py")
    click.echo(f"   3. Add to catalog.json when ready")
    click.echo(f"   4. Install config: waifu dev add-skill {name}")
    click.echo()


@dev.command("show-skill")
@click.argument("name")
def show_skill(name):
    """Display a skill's SKILL.md content."""
    catalog = _load_catalog()
    skill = _find_skill(catalog, name)

    # Allow showing skills not in catalog (custom skills)
    if skill:
        skill_id = skill["id"]
    else:
        skill_id = name

    skill_dir = SKILLS_DIR / skill_id

    if not (skill_dir / "SKILL.md").exists():
        click.echo(f"❌ No SKILL.md found at senpi-skills/{skill_id}/")
        if not skill:
            click.echo("   Skill not found in catalog either.")
            click.echo("   Run: waifu dev list-skills")
        sys.exit(1)

    # Parse and display frontmatter
    meta = _parse_frontmatter(skill_dir)
    text = (skill_dir / "SKILL.md").read_text()

    click.echo(f"\n{'=' * 60}")
    if skill:
        click.echo(f"  {skill['emoji']}  {skill['name']} — {skill['tagline']}")
        configured = _is_configured(skill_id)
        status = "✅ configured" if configured else "📦 installed"
        click.echo(f"  Status: {status}  |  Group: {skill.get('group', '?')}")
    elif meta.get("name"):
        click.echo(f"  {meta['name']}")
    click.echo(f"{'=' * 60}")

    if meta:
        click.echo(f"\n  Frontmatter:")
        for key, val in meta.items():
            if isinstance(val, str) and val:
                display = val[:80] + "..." if len(val) > 80 else val
                click.echo(f"    {key}: {display}")

    # Show content after frontmatter (first section)
    content = text
    if text.startswith("---"):
        end_idx = text.index("---", 3)
        content = text[end_idx + 3 :].strip()

    # Show first section (up to second ## heading or 60 lines)
    lines = content.splitlines()
    section_lines = []
    heading_count = 0
    for line in lines:
        if line.startswith("## ") and heading_count > 0:
            break
        if line.startswith("## "):
            heading_count += 1
        section_lines.append(line)
        if len(section_lines) >= 60:
            section_lines.append("... (truncated, see full SKILL.md)")
            break

    click.echo(f"\n{'─' * 60}")
    click.echo("\n".join(section_lines))
    click.echo(f"\n{'─' * 60}")

    total_lines = len(lines)
    shown = len(section_lines)
    if shown < total_lines:
        click.echo(
            f"\n  Showing {shown}/{total_lines} lines. "
            f"Full file: senpi-skills/{skill_id}/SKILL.md"
        )
    click.echo()


@dev.command("brain-check")
def brain_check():
    """Verify Strategic Brain (Hermes) health — LLM key and binary status."""
    import os

    click.echo(f"\n{'=' * 60}")
    click.echo("  🧠 STRATEGIC BRAIN HEALTH CHECK")
    click.echo(f"{'=' * 60}\n")

    checks_passed = 0
    checks_total = 0

    hermes_bin = shutil.which("hermes")
    checks_total += 1
    if hermes_bin:
        click.echo(f"  ✅ Hermes binary: {hermes_bin}")
        checks_passed += 1
    else:
        click.echo("  ❌ Hermes binary: not found on PATH")

    checks_total += 1
    or_key = os.environ.get("OPENROUTER_API_KEY", "")
    if or_key:
        click.echo(f"  ✅ OPENROUTER_API_KEY: {or_key[:12]}...{or_key[-6:]}")
        checks_passed += 1
    else:
        click.echo("  ⚠️  OPENROUTER_API_KEY: not set")

    checks_total += 1
    oai_key = os.environ.get("OPENAI_API_KEY", "")
    if oai_key:
        click.echo(f"  ✅ OPENAI_API_KEY: {oai_key[:12]}...{oai_key[-6:]}")
        checks_passed += 1
    else:
        click.echo("  ⚠️  OPENAI_API_KEY: not set")

    checks_total += 1
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if anthropic_key:
        click.echo(
            f"  ✅ ANTHROPIC_API_KEY: {anthropic_key[:12]}...{anthropic_key[-6:]}"
        )
        checks_passed += 1
    else:
        click.echo("  ⚠️  ANTHROPIC_API_KEY: not set")

    checks_total += 1
    hermes_home = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
    soul_path = os.path.join(hermes_home, "SOUL.md")
    if os.path.isfile(soul_path):
        click.echo(f"  ✅ SOUL.md: {soul_path}")
        checks_passed += 1
    else:
        click.echo(f"  ⚠️  SOUL.md: not found at {soul_path}")

    checks_total += 1
    project_soul = PROJECT_ROOT / "config" / "hermes-soul.md"
    if project_soul.exists():
        click.echo(f"  ✅ hermes-soul.md (project): {project_soul}")
        checks_passed += 1
    else:
        click.echo(f"  ⚠️  hermes-soul.md: not found at {project_soul}")

    click.echo(f"\n{'─' * 60}")
    if checks_passed >= 3:
        click.echo(
            f"  Result: {checks_passed}/{checks_total} checks passed — "
            + click.style("BRAIN READY", fg="green")
        )
    elif checks_passed >= 1:
        click.echo(
            f"  Result: {checks_passed}/{checks_total} checks passed — "
            + click.style("PARTIAL (check LLM keys)", fg="yellow")
        )
    else:
        click.echo(
            f"  Result: {checks_passed}/{checks_total} checks passed — "
            + click.style("BRAIN OFFLINE", fg="red")
        )
    click.echo(f"{'=' * 60}\n")
