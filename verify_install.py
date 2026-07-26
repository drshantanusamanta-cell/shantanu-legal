#!/usr/bin/env python3
"""
Pre-deployment check.

Run this BEFORE pushing, and again after cloning your repo fresh, to catch the
most common Streamlit Cloud failure: nested folders or empty __init__.py files
that never reached GitHub.

    python verify_install.py

Exit code 0 = safe to deploy.

(c) 2026 Dr Shantanu Samanta. All rights reserved.
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

GREEN, RED, YELLOW, DIM, RESET = "\033[92m", "\033[91m", "\033[93m", "\033[2m", "\033[0m"
if os.name == "nt" and not os.environ.get("WT_SESSION"):
    GREEN = RED = YELLOW = DIM = RESET = ""

OK, BAD, WARN = f"{GREEN}PASS{RESET}", f"{RED}FAIL{RESET}", f"{YELLOW}WARN{RESET}"

REQUIRED_TREE = {
    ".": ["app.py", "auth.py", "config.py", "engine.py", "exporters.py",
          "requirements.txt"],
    "legal": ["__init__.py", "citations.py", "knowledge_bases.py", "verifier.py",
              "prompts.py"],
    "llm": ["__init__.py", "base.py", "anthropic_client.py", "gemini_client.py"],
    "ingest": ["__init__.py", "documents.py"],
}

failures: list[str] = []
warnings: list[str] = []


def check_files() -> None:
    print(f"\n{DIM}--- file tree ---{RESET}")
    for folder, files in REQUIRED_TREE.items():
        path = HERE if folder == "." else os.path.join(HERE, folder)
        if not os.path.isdir(path):
            print(f"  {BAD}  {folder}/ — FOLDER MISSING")
            failures.append(f"{folder}/ folder is missing entirely")
            continue
        for f in files:
            fp = os.path.join(path, f)
            label = f if folder == "." else f"{folder}/{f}"
            if not os.path.exists(fp):
                print(f"  {BAD}  {label} — missing")
                failures.append(f"{label} is missing")
            elif os.path.getsize(fp) == 0:
                print(f"  {BAD}  {label} — EMPTY (0 bytes)")
                failures.append(
                    f"{label} is empty; GitHub web upload drops zero-byte files"
                )
            else:
                print(f"  {OK}  {label} {DIM}({os.path.getsize(fp)} bytes){RESET}")


def check_imports() -> None:
    print(f"\n{DIM}--- imports ---{RESET}")
    modules = [
        "config", "auth", "exporters", "engine",
        "legal.citations", "legal.knowledge_bases", "legal.verifier", "legal.prompts",
        "llm.base", "llm.anthropic_client", "llm.gemini_client",
        "ingest.documents",
    ]
    for m in modules:
        try:
            __import__(m)
            print(f"  {OK}  import {m}")
        except ImportError as exc:
            print(f"  {BAD}  import {m} — {exc}")
            failures.append(f"cannot import {m}: {exc}")
        except Exception as exc:  # noqa: BLE001
            print(f"  {WARN}  import {m} — {type(exc).__name__}: {exc}")
            warnings.append(f"{m}: {exc}")


def check_dependencies() -> None:
    print(f"\n{DIM}--- dependencies ---{RESET}")
    required = {
        "streamlit": "streamlit", "requests": "requests", "anthropic": "anthropic",
        "pdfplumber": "pdfplumber", "docx": "python-docx", "reportlab": "reportlab",
    }
    optional = {"sklearn": "scikit-learn", "pytesseract": "pytesseract",
                "dotenv": "python-dotenv", "PIL": "Pillow", "pypdf": "pypdf"}

    for mod, pkg in required.items():
        try:
            __import__(mod)
            print(f"  {OK}  {pkg}")
        except ImportError:
            print(f"  {BAD}  {pkg} — pip install {pkg}")
            failures.append(f"missing dependency {pkg}")

    for mod, pkg in optional.items():
        try:
            __import__(mod)
            print(f"  {OK}  {pkg} {DIM}(optional){RESET}")
        except ImportError:
            print(f"  {WARN}  {pkg} {DIM}(optional — some features degrade){RESET}")
            warnings.append(f"optional dependency {pkg} not installed")


def check_secrets() -> None:
    print(f"\n{DIM}--- configuration ---{RESET}")
    try:
        from config import get_secret
    except Exception as exc:  # noqa: BLE001
        print(f"  {BAD}  cannot load config: {exc}")
        return

    if get_secret("APP_PASSWORD_SHA256") or get_secret("APP_PASSWORD") or get_secret("APP_USERS"):
        print(f"  {OK}  access password configured")
    else:
        print(f"  {WARN}  no access password — the app will refuse to start")
        warnings.append("APP_PASSWORD_SHA256 not set")

    if get_secret("ANTHROPIC_API_KEY") or get_secret("GEMINI_API_KEY"):
        which = []
        if get_secret("ANTHROPIC_API_KEY"):
            which.append("Anthropic")
        if get_secret("GEMINI_API_KEY"):
            which.append("Gemini")
        print(f"  {OK}  LLM provider configured: {', '.join(which)}")
    else:
        print(f"  {WARN}  no LLM key — set ANTHROPIC_API_KEY or GEMINI_API_KEY")
        warnings.append("no LLM API key configured")

    if get_secret("INDIAN_KANOON_API_TOKEN"):
        print(f"  {OK}  Indian Kanoon token configured")
    else:
        print(f"  {WARN}  no Indian Kanoon token — citation verification will be weaker")
        warnings.append("INDIAN_KANOON_API_TOKEN not set")


def check_gitignore() -> None:
    print(f"\n{DIM}--- safety ---{RESET}")
    gi = os.path.join(HERE, ".gitignore")
    if os.path.exists(gi):
        content = open(gi, encoding="utf-8").read()
        if "secrets.toml" in content:
            print(f"  {OK}  .gitignore excludes secrets.toml")
        else:
            print(f"  {BAD}  .gitignore does NOT exclude secrets.toml")
            failures.append("secrets.toml is not gitignored — you may leak API keys")
    else:
        print(f"  {BAD}  .gitignore missing")
        failures.append(".gitignore missing")

    sec = os.path.join(HERE, ".streamlit", "secrets.toml")
    if os.path.exists(sec):
        body = open(sec, encoding="utf-8").read()
        if "sk-ant-" in body or "AIza" in body:
            print(f"  {WARN}  secrets.toml contains live-looking keys — never commit it")
        else:
            print(f"  {OK}  local secrets.toml present (placeholder)")


def main() -> int:
    print("=" * 62)
    print("  Senior Counsel — pre-deployment check")
    print("  (c) 2026 Dr Shantanu Samanta")
    print("=" * 62)
    print(f"{DIM}Checking: {HERE}{RESET}")

    check_files()
    check_imports()
    check_dependencies()
    check_secrets()
    check_gitignore()

    print("\n" + "=" * 62)
    if failures:
        print(f"{RED}{len(failures)} BLOCKING PROBLEM(S){RESET}\n")
        for f in failures:
            print(f"  - {f}")
        print(
            f"\n{YELLOW}Most likely cause:{RESET} files uploaded to GitHub via the web\n"
            "interface, which does not reliably carry nested folders or empty files.\n\n"
            "Fix by pushing the whole project with git:\n\n"
            "    git add -A\n"
            '    git commit -m "Add missing package folders"\n'
            "    git push\n"
        )
        return 1

    if warnings:
        print(f"{GREEN}Structure OK{RESET} — {len(warnings)} warning(s):\n")
        for w in warnings:
            print(f"  - {w}")
        print("\nSafe to deploy; warnings above are configuration, not code.")
        return 0

    print(f"{GREEN}ALL CHECKS PASSED — safe to deploy.{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
