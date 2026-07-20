#!/usr/bin/env python3
"""Scan proposed Git files for credential material without printing matched values.

This scanner intentionally reports only path, line number, and rule name. It does
not print the matching line or secret candidate.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

MAX_TEXT_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True, order=True)
class Finding:
    path: str
    line: int
    rule: str


FILENAME_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(^|/)\.env($|\.)", re.IGNORECASE), "environment_file"),
    (re.compile(r"(^|/)(id_rsa|id_dsa|id_ecdsa|id_ed25519)(\.|$)", re.IGNORECASE), "ssh_private_key_filename"),
    (re.compile(r"\.(pem|key|p12|pfx|jks|keystore|kdbx)$", re.IGNORECASE), "private_key_or_keystore_filename"),
    (re.compile(r"(^|/).*(credential|secret|mnemonic|seed.?phrase|cookie).*$", re.IGNORECASE), "sensitive_filename"),
    (re.compile(r"(^|/).*wallet.*\.(json|txt|yaml|yml)$", re.IGNORECASE), "wallet_filename"),
)

CONTENT_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"), "private_key_block"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "aws_access_key"),
    (re.compile(r"\bASIA[0-9A-Z]{16}\b"), "aws_temporary_access_key"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), "github_token"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"), "slack_token"),
    (re.compile(r"\bsk_(?:live|test)_[A-Za-z0-9]{16,}\b"), "stripe_style_secret"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"), "google_api_key"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"), "jwt_token"),
    (re.compile(r"(?i)authorization\s*[:=]\s*['\"]?bearer\s+[A-Za-z0-9._~+/=-]{16,}"), "literal_bearer_token"),
    (re.compile(r"(?i)https?://[^\s/:@]+:[^\s/@]+@"), "credential_in_url"),
    (re.compile(r"(?i)(?:cookie|set-cookie)\s*[:=]\s*['\"][^'\"]{12,}['\"]"), "literal_cookie"),
    (
        re.compile(
            r"(?i)\b(?:api[_-]?key|api[_-]?secret|access[_-]?token(?:[_-]?secret)?|"
            r"private[_-]?key|client[_-]?secret|password|passwd|mnemonic|seed[_-]?phrase)\b"
            r"\s*[:=]\s*['\"](?!\s*(?:example|placeholder|redacted|change[-_ ]?me)?\s*['\"])[^'\"]{8,}['\"]"
        ),
        "literal_sensitive_assignment",
    ),
)


def run_git(root: Path, args: list[str]) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode:
        message = result.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(message or f"git exited {result.returncode}")
    return result.stdout


def candidate_paths(root: Path, staged: bool) -> list[Path]:
    if staged:
        raw = run_git(root, ["diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"])
    else:
        raw = run_git(root, ["ls-files", "--cached", "--others", "--exclude-standard", "-z"])
    paths: list[Path] = []
    for item in raw.split(b"\0"):
        if not item:
            continue
        relative = Path(item.decode("utf-8", "surrogateescape"))
        absolute = (root / relative).resolve()
        try:
            absolute.relative_to(root.resolve())
        except ValueError as exc:
            raise RuntimeError(f"candidate escapes repository root: {relative}") from exc
        if absolute.is_file() and not absolute.is_symlink():
            paths.append(absolute)
    return sorted(set(paths))


def scan_file(root: Path, path: Path) -> list[Finding]:
    relative = path.relative_to(root).as_posix()
    findings: set[Finding] = set()

    # These two repository files intentionally contain sensitive vocabulary but
    # no values: the blank template and this scanner implementation itself.
    if relative not in {".env.example", "tools/secret_scan.py"}:
        for pattern, rule in FILENAME_RULES:
            if pattern.search(relative):
                findings.add(Finding(relative, 0, rule))

    size = path.stat().st_size
    if size > MAX_TEXT_BYTES:
        findings.add(Finding(relative, 0, "candidate_file_over_10MiB"))
        return sorted(findings)

    raw = path.read_bytes()
    if b"\0" in raw:
        return sorted(findings)
    text = raw.decode("utf-8", "replace")
    for line_number, line in enumerate(text.splitlines(), 1):
        for pattern, rule in CONTENT_RULES:
            if pattern.search(line):
                findings.add(Finding(relative, line_number, rule))
    return sorted(findings)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--git-candidates", action="store_true", help="scan tracked plus untracked non-ignored files")
    mode.add_argument("--staged", action="store_true", help="scan staged added/modified files only")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = args.root.resolve()
    if not (root / ".git").exists():
        print(f"secret_scan error: not a Git repository: {root}", file=sys.stderr)
        return 3
    try:
        paths = candidate_paths(root, staged=args.staged)
        findings = [finding for path in paths for finding in scan_file(root, path)]
    except (OSError, RuntimeError) as exc:
        print(f"secret_scan error: {type(exc).__name__}", file=sys.stderr)
        return 3

    mode = "staged" if args.staged else "git_candidates"
    print(f"secret_scan mode={mode} files_scanned={len(paths)} findings={len(findings)}")
    for finding in findings:
        location = f"{finding.path}:{finding.line}" if finding.line else finding.path
        print(f"FINDING {location} rule={finding.rule}")
    return 2 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
