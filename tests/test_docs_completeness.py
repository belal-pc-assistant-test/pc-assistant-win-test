"""Round 12: the docs must match the code.

* Every command in the bot's command registry is in the README and in the Arabic quick start.
* Every command the README documents exists in the registry (no invented commands).
* Every key in config.example.ini is documented in the README.
* docs/TROUBLESHOOTING.md exists, is linked from the README, has an English and an
  Arabic part, and covers the topics the user asked for.

The registry is read from the source with ``ast``, so no bot is built here.
"""

from __future__ import annotations

import ast
import configparser
import re
import unittest
from pathlib import Path

from _bootstrap import PROJECT_DIR

BOT_SOURCE = PROJECT_DIR / "assistant" / "bot.py"
README = PROJECT_DIR / "README.md"
CONFIG_EXAMPLE = PROJECT_DIR / "config.example.ini"
TROUBLESHOOTING = PROJECT_DIR / "docs" / "TROUBLESHOOTING.md"
CHANGELOG = PROJECT_DIR / "CHANGELOG.md"

ARABIC_HEADING = "## البدء السريع"
ENGLISH_HEADING = "## Quick start"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def registry_commands() -> set[str]:
    """Names (and aliases) of every CommandHandler registered in assistant/bot.py."""
    tree = ast.parse(read(BOT_SOURCE), filename=str(BOT_SOURCE))
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Name) and func.id == "CommandHandler" and node.args):
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            names.add(first.value)
        elif isinstance(first, ast.List):
            for item in first.elts:
                if isinstance(item, ast.Constant) and isinstance(item.value, str):
                    names.add(item.value)
    return names


def command_mentioned(text: str, name: str) -> bool:
    """``/name`` as a whole word: ``/no`` must not match ``/notes``."""
    return re.search(rf"(?<![\w/])/{re.escape(name)}(?![\w])", text) is not None


def section(text: str, start: str, end: str) -> str:
    """The text from the ``start`` heading up to (not including) the ``end`` heading."""
    begin = text.index(start)
    stop = text.index(end, begin + len(start))
    return text[begin:stop]


def arabic_quick_start() -> str:
    return section(read(README), ARABIC_HEADING, ENGLISH_HEADING)


def readme_commands_table() -> str:
    text = read(README)
    return section(text, "## Commands", "## How approval works")


class CommandRegistryTests(unittest.TestCase):

    def test_the_registry_is_found_and_not_empty(self):
        names = registry_commands()
        self.assertGreater(len(names), 25)
        self.assertIn("start", names)
        self.assertIn("whoami", names)  # an alias from a list argument

    def test_every_registered_command_is_in_the_english_readme(self):
        text = read(README)
        missing = sorted(n for n in registry_commands() if not command_mentioned(text, n))
        self.assertEqual(missing, [], f"commands missing from README.md: {missing}")

    def test_every_registered_command_is_in_the_arabic_quick_start(self):
        text = arabic_quick_start()
        missing = sorted(n for n in registry_commands() if not command_mentioned(text, n))
        self.assertEqual(missing, [], f"commands missing from the Arabic quick start: {missing}")

    def test_every_command_in_the_readme_commands_table_exists_in_code(self):
        table = readme_commands_table()
        documented = set(re.findall(r"(?<![\w/])/([a-z]+)(?![\w])", table))
        unknown = sorted(documented - registry_commands())
        self.assertEqual(unknown, [], f"README documents commands the bot does not register: {unknown}")

    def test_every_command_in_the_arabic_quick_start_exists_in_code(self):
        documented = set(re.findall(r"(?<![\w/])/([a-z]+)(?![\w])", arabic_quick_start()))
        unknown = sorted(documented - registry_commands())
        self.assertEqual(unknown, [], f"Arabic quick start names commands that do not exist: {unknown}")


class ConfigDocumentationTests(unittest.TestCase):

    def config_keys(self) -> list[tuple[str, str]]:
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string(read(CONFIG_EXAMPLE))
        return [(section_name, key) for section_name in parser.sections()
                for key in parser[section_name]]

    def test_the_example_config_has_keys(self):
        self.assertGreater(len(self.config_keys()), 30)

    def test_every_config_example_key_is_documented_in_the_readme(self):
        # A key counts as documented when it appears in backticks, either alone (`key`) or
        # with its section (`[section] key`), as the README settings table writes it.
        text = read(README)
        missing = []
        for section_name, key in self.config_keys():
            pattern = rf"`(?:\[{re.escape(section_name)}\] )?{re.escape(key)}`"
            if re.search(pattern, text) is None:
                missing.append(f"[{section_name}] {key}")
        self.assertEqual(missing, [], f"config keys not documented in README.md: {missing}")


class TroubleshootingDocTests(unittest.TestCase):

    def test_the_guide_exists_and_is_linked_from_the_readme(self):
        self.assertTrue(TROUBLESHOOTING.is_file(), "docs/TROUBLESHOOTING.md is missing")
        self.assertIn("docs/TROUBLESHOOTING.md", read(README))

    def test_the_guide_has_an_english_part_and_an_arabic_part(self):
        text = read(TROUBLESHOOTING)
        self.assertIn("# Troubleshooting", text)
        self.assertRegex(text, r"[\u0600-\u06FF]{4,}", "the Arabic part is missing")

    def test_the_guide_covers_every_requested_topic(self):
        text = read(TROUBLESHOOTING).lower()
        topics = {
            "does not answer": "bot does not answer",
            "unauthorized": "unauthorized",
            "wrong user id": "user id",
            "python not found": "python",
            "install.bat": "install.bat",
            "antivirus": "antivirus",
            "window closes": "window",
            "proxy": "proxy",
            "approvals expire": "15 minutes",
            "remote link": "/remote",
            "install fails": "/install",
            "uac": "uac",
            "revoke": "revoke",
            "set_token": "set_token.bat",
            "uninstall": "uninstall",
            "logs": "assistant.log",
        }
        missing = [topic for topic, needle in topics.items() if needle not in text]
        self.assertEqual(missing, [])

    def test_the_guide_says_logs_never_contain_the_token(self):
        self.assertIn("never", read(TROUBLESHOOTING).lower())


class ChangelogTests(unittest.TestCase):

    def test_the_changelog_covers_rounds_6_to_12(self):
        text = read(CHANGELOG)
        for round_number in range(6, 13):
            self.assertIn(f"Round {round_number}", text, f"CHANGELOG.md has no Round {round_number}")


if __name__ == "__main__":
    unittest.main()
