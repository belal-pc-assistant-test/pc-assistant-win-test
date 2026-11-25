"""Build a release ZIP from an explicit source allowlist; never include secrets."""
import os
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parent
EXCLUDED_NAMES = {"token.txt", "token.json", "config.ini", ".env"}
TOP_LEVEL = (
    "README.md", "requirements.txt", "requirements-voice.txt", "config.example.ini", "install.bat",
    "start_assistant.bat", "set_token.bat",
    "check_setup.bat", "main.py", "build_zip.py", ".gitignore",
)


def build_zip(root: Path, output: Path) -> None:
    root = root.resolve()
    sources = [root / name for name in TOP_LEVEL]
    sources += sorted((root / "assistant").rglob("*.py"))
    sources += sorted((root / "tests").glob("test_*.py"))
    # Guard against accidentally pasting the current local bot token into a
    # source file. Never print the token, even if packaging is refused.
    secrets = []
    token_path = root / "token.txt"
    if token_path.is_file():
        try:
            for line in token_path.read_text(encoding="utf-8-sig").splitlines():
                secret = line.strip().lstrip("\ufeff").strip()
                if secret:
                    secrets.append(secret.encode("utf-8"))
                    break
        except (OSError, UnicodeError) as exc:
            raise ValueError("Cannot safely inspect local token.txt") from exc
    env = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if env:
        secrets.append(env.encode("utf-8"))
    for source in sources:
        if source.is_symlink() or not source.resolve().is_relative_to(root):
            raise ValueError("Unsafe release source path")
        data = source.read_bytes()
        if any(secret in data for secret in secrets):
            raise ValueError("Release source contains a locally configured bot token; archive not built.")
    temp = output.with_name(output.name + ".part")
    with ZipFile(temp, "w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for source in sources:
            relative = source.relative_to(root)
            if any(part.casefold() in EXCLUDED_NAMES for part in relative.parts):
                continue
            if any(part in {"__pycache__", ".venv", "data", "models", "logs", "downloads", "screenshots"} for part in relative.parts):
                continue
            # Do not follow links into private data outside the release tree.
            if source.is_symlink() or not source.resolve().is_relative_to(root):
                raise ValueError(f"Unsafe release path: {relative}")
            archive.write(source, Path(root.name) / relative)
    with ZipFile(temp) as archive:
        if archive.testzip() is not None:
            raise ValueError("Archive integrity check failed")
        if any(Path(name).name.casefold() == "token.txt" for name in archive.namelist()):
            raise ValueError("A token file was found in the release archive")
    os.replace(temp, output)


if __name__ == "__main__":
    destination = ROOT.parent / "telegram-pc-assistant.zip"
    build_zip(ROOT, destination)
    print(f"Built {destination}")
