#!/usr/bin/env python3
"""Two-stage Claude Code hook guard for Form-Guard (registered in .claude/settings.json). Standard library only.

    python scripts/hook_guard.py pre-tool-use     # PreToolUse:  Write|Edit|MultiEdit|NotebookEdit|Bash|PowerShell
    python scripts/hook_guard.py post-tool-use    # PostToolUse: Write|Edit|MultiEdit

Stage 1 — PreToolUse, BEFORE anything reaches the disk (a violation exits with code 2 and prints a
``permissionDecision: "deny"`` decision, so the tool call never runs):

* Blank-template guard. The blank forms in samples/blank_forms/ (*.docx, *.xlsx, *.pdf) are the untouched "before" of
  every structural proof. A Write / Edit / MultiEdit / NotebookEdit of such a file, or a shell command that writes,
  copies over, moves or deletes one (redirection, cp/mv/rm, tee, Set-Content, Copy-Item, sed -i, an inline
  python / node / .NET write …), is refused — and so is deleting, moving or renaming the samples/blank_forms
  folder itself, the samples folder or any folder above them (``rm -rf samples/blank_forms``, ``Remove-Item
  -LiteralPath samples/blank_forms -Recurse``, ``rmdir /s``, ``rm -rf .`` at the project root, ``cd samples`` then
  ``rm -rf blank_forms``), a wildcard that can match a template or that folder (``rm samples/blank_forms/*``,
  ``rm -rf samples/*``), ``find … -delete`` / ``-exec rm`` over them, a listing of them piped into ``rm`` /
  ``Remove-Item`` / ``xargs rm``, and ``shutil.rmtree`` / ``Directory::Delete`` on that folder. Only the
  deterministic sample generator itself — a Python launcher running ``-m form_guard.cli`` (or ``form-guard``) with
  ``--generate-samples`` as a real argument, not a word in a ``# comment`` — is allowed, and never with an output
  redirection into a template. A shell is a programming language, so this is a best-effort guard of the direct
  forms; the verifier's byte-level comparison stays the backstop. A human can override by starting Claude Code with
  FORM_GUARD_ALLOW_TEMPLATE_WRITE=1: the hook reads its own environment, so an agent cannot switch the guard off
  from inside the command it wants to run.
* Secret & hygiene guard. The text a Write / Edit / MultiEdit / NotebookEdit is about to put into a project file
  (``content``, ``new_string``, ``edits[].new_string``, ``new_source``) is scanned for an Anthropic API key, a
  literal ``ANTHROPIC_API_KEY = "sk-…"`` assignment, a personal user-profile path (C:\\Users\\<user>,
  /Users/<user>, /home/<user> with any user name, Japanese or other non-ASCII names included, also right after
  Japanese text — generic placeholders such as <user>, user, runner, root, ubuntu or container are fine) and
  e-mail addresses outside the reserved example domains (example.com / example.jp / example.org and their
  subdomains, users.noreply.github.com; the only other address allowed is noreply@anthropic.com). Shell commands
  are checked for API keys only. The git-ignored .env file is where the key belongs and is exempt; files outside
  the project are not inspected.

Stage 2 — PostToolUse, AFTER the write (findings go back to Claude as ``decision: "block"`` so they are fixed at
once):

* .py files: ``py_compile`` of the file, then (for form_guard/ and tests/) an import smoke test of the edited
  module with the project's interpreter (.venv), within a 1.8 s budget;
* .json files: ``json.loads`` validity (a UTF-8 BOM is reported too — Claude Code cannot read it);
* every text file written inside the project is re-scanned with the hygiene rules above.

Extensions are compared case-insensitively (``x.PY`` and ``x.JSON`` are checked like ``x.py`` / ``x.json`` on
Windows). The hook is run as ``python scripts/hook_guard.py …`` from .claude/settings.json and needs nothing but the
standard library: any Python 3.9+ on PATH works (activate the project's .venv before starting Claude Code — on
Windows ``.venv\\Scripts\\Activate.ps1``, elsewhere ``source .venv/bin/activate``); the import smoke test always
prefers the project's .venv interpreter (or FORM_GUARD_HOOK_PYTHON) whichever ``python`` started the hook.

The hook never blocks because of its own failure: odd payloads, missing optional dependencies, timeouts and
internal errors only produce a note on stderr and exit code 0. Findings never echo the matched secret, path or
address — only the rule, the file name and the line.
"""

from __future__ import annotations

import json
import os
import posixpath
import py_compile
import re
import shlex
import subprocess
import sys
import tempfile
import time
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
OVERRIDE_ENV = "FORM_GUARD_ALLOW_TEMPLATE_WRITE"
TEMPLATE_EXTS = (".docx", ".xlsx", ".docm", ".xlsm", ".pdf")
GENERATOR_FLAGS = ("--generate-samples", "--regenerate-samples")
POST_BUDGET_S = 1.8  # py_compile + import smoke test must finish well under 2 seconds
WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
SHELL_TOOLS = ("Bash", "PowerShell")
TEXT_EXTS = (".py", ".md", ".json", ".txt", ".toml", ".yml", ".yaml", ".js", ".mjs", ".ts", ".html", ".css", ".cfg",
             ".ini", ".sh", ".ps1", ".bat", ".cmd", ".svg", ".xml", ".csv", ".ipynb", ".rst", ".env.example")
MAX_SCAN_BYTES = 2_000_000

# ------------------------------------------------------------------ hygiene rules (never match this file itself)

_SK = "sk-" + "ant-"
API_KEY_RE = re.compile(re.escape(_SK) + r"[A-Za-z0-9_\-]{16,}")
KEY_ASSIGN_RE = re.compile(r"(?i)\b(?:ANTHROPIC_API_KEY|ANTHROPIC_AUTH_TOKEN|api[_-]?key)\b[\"']?\s*[:=]\s*[\"']?"
                           r"sk-[A-Za-z0-9_\-]{6,}")
# The profile folder names are assembled from fragments, so these patterns never match this file's own source.
_USERS, _HOME = "Us" + "ers", "ho" + "me"
_SEP = r"(?:\\\\|\\|/)+"
# A user name is any run of non-separator characters — Japanese and other non-ASCII names included.
_NAME = r"(?P<name>[^\\/\s\"'<>|:*?`,;)\]]+)"
# Only an ASCII letter / digit right before the drive letter or slash means "not a path start": a path written
# straight after Japanese text (e.g. 保存先 directly followed by the profile path) is still found.
WIN_PROFILE_RE = re.compile(r"(?i)(?<![A-Za-z0-9])[A-Z]:" + _SEP + _USERS + _SEP + _NAME)
POSIX_PROFILE_RE = re.compile(r"(?<![A-Za-z0-9_.~-])(?:/[A-Za-z])?/(?:" + _USERS + "|" + _HOME + ")/" + _NAME)
EMAIL_RE = re.compile(r"(?<![\w.%+-])(?P<local>[A-Za-z0-9._%+-]+)@(?P<domain>[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)")
# Reserved example domains (and their subdomains) and GitHub's no-reply domain; nothing else, compared label-wise:
# "notexample.com" or "example.com.evil.org" never pass. Other addresses only when listed exactly.
EXAMPLE_MAIL_DOMAINS = ("example.com", "example.jp", "example.org", "users.noreply.github.com")
EXACT_MAIL_ALLOW = frozenset({"noreply@" + "anthropic.com"})  # the Co-Authored-By trailer of a commit
PLACEHOLDER_USERS = frozenset({"user", "username", "user-name", "user_name", "you", "yourname", "your-name",
                               "your_name", "yourusername", "name", "me", "public", "default", "default user",
                               "all users", "example", "xxx", "...", "…", "runner", "root", "ubuntu",
                               "container"})


def _placeholder(name: str) -> bool:
    n = name.strip().lower()
    return not n or n in PLACEHOLDER_USERS or n[0] in "<{$%*." or set(n) <= {"x", "."}


def _mail_ok(local: str, domain: str) -> bool:
    d = domain.lower().rstrip(".")
    if local.lower() == "git":  # the "git" user of an SSH clone URL is not an address
        return True
    if not re.fullmatch(r"[a-z]{2,}", d.rsplit(".", 1)[-1]):
        return True  # a last label with digits (a version suffix) is not a mail domain
    if f"{local.lower()}@{d}" in EXACT_MAIL_ALLOW:
        return True
    return any(d == base or d.endswith("." + base) for base in EXAMPLE_MAIL_DOMAINS)


def _ext(path: Path, *exts: str) -> bool:
    """Case-insensitive extension test: x.PY is a Python file and x.JSON a JSON file, as on Windows."""
    return path.name.lower().endswith(tuple(e.lower() for e in exts))


def hygiene_findings(text: str, secrets_only: bool = False) -> List[Tuple[int, str]]:
    """[(line number, rule)] for every hygiene problem in ``text``; never the matched value itself."""
    found: List[Tuple[int, str]] = []

    def line_of(pos: int) -> int:
        return text.count("\n", 0, pos) + 1

    for m in API_KEY_RE.finditer(text):
        found.append((line_of(m.start()), "Anthropic API key"))
    for m in KEY_ASSIGN_RE.finditer(text):
        found.append((line_of(m.start()), "literal API key assignment (keep the key in the git-ignored .env)"))
    if not secrets_only:
        for rx in (WIN_PROFILE_RE, POSIX_PROFILE_RE):
            for m in rx.finditer(text):
                if not _placeholder(m.group("name")):
                    found.append((line_of(m.start()), "local user-profile path (use a placeholder such as <user>)"))
        for m in EMAIL_RE.finditer(text):
            if not _mail_ok(m.group("local"), m.group("domain")):
                found.append((line_of(m.start()), "e-mail address outside the reserved example domains"))
    return sorted(dict.fromkeys(found))


def _format(findings: List[Tuple[int, str]], shown: str, limit: int = 8) -> str:
    out = [f"{shown}: line {ln}: {rule}" for ln, rule in findings[:limit]]
    if len(findings) > limit:
        out.append(f"… and {len(findings) - limit} more")
    return "; ".join(out)


# ------------------------------------------------------------------ I/O helpers


def _force_utf8() -> None:
    """Windows consoles default to cp932 / cp1252: make every stream UTF-8 so nothing is mangled or crashes."""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError, OSError):
            pass


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj))  # ASCII-only JSON: safe whatever the console code page
    sys.stdout.flush()


def _stderr(text: str) -> None:
    data = (text.rstrip() + "\n").encode("utf-8", "replace")
    try:
        sys.stderr.buffer.write(data)
    except AttributeError:  # pragma: no cover - a stream without a buffer
        sys.stderr.write(data.decode("utf-8"))
    sys.stderr.flush()


def _project_roots(payload: dict) -> List[Path]:
    roots = [ROOT]
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env:
        try:
            roots.append(Path(env).resolve())
        except OSError:
            pass
    return list(dict.fromkeys(roots))


def _resolve(raw: str, payload: dict) -> Path:
    p = Path(raw)
    if not p.is_absolute():
        p = Path(payload.get("cwd") or os.getcwd()) / p
    try:
        return p.resolve()
    except OSError:
        return p


def _inside(path: Path, roots: Iterable[Path]) -> Optional[Path]:
    for r in roots:
        try:
            path.relative_to(r)
            return r
        except ValueError:
            continue
    return None


def _hygiene_exempt(path: Path) -> bool:
    """The git-ignored .env is where the API key belongs; settings.local.json is personal and git-ignored."""
    name = path.name.lower()
    return name == ".env" or (name.startswith(".env.") and name != ".env.example") or name == "settings.local.json"


# ------------------------------------------------------------------ stage 1a: blank-template guard

TEMPLATE_RE = re.compile(r"samples[\\/]+blank_forms[\\/]+[^\s\"'`<>|;&()]*?\.(?:docx|xlsx|docm|xlsm|pdf)(?![\w.])", re.I)
TEMPLATE_DIR_RE = re.compile(r"(?:^|[\\/])samples[\\/]+blank_forms[\\/]*$", re.I)
_LIT = r"[\"'][^\"']*samples[\\/]+blank_forms[\\/][^\"']*\.(?:docx|xlsx|docm|xlsm|pdf)[\"']"
INLINE_WRITES = [  # write calls whose own argument is a template path literal
    re.compile(r"\bopen\(\s*" + _LIT + r"\s*,\s*(?:mode\s*=\s*)?[\"'][^\"']*[wax+]", re.I),
    re.compile(r"\.save(?:_?as)?\(\s*(?:filename\s*=\s*)?" + _LIT, re.I),
    re.compile(_LIT + r"\s*\)\s*\.(?:write_bytes|write_text|unlink|rename|replace|touch)\(", re.I),
    re.compile(r"\b(?:os\.(?:remove|unlink|rename|replace)|unlinkSync|rmSync|renameSync|writeFileSync|writeFile|"
               r"appendFileSync|createWriteStream)\(\s*" + _LIT, re.I),
    re.compile(r"\b(?:copy|copy2|copyfile|move|copyFileSync|cpSync)\([^)]*?,\s*" + _LIT, re.I),
    re.compile(r"\[(?:System\.)?IO\.File\]::(?:WriteAll\w*|AppendAll\w*|Delete|Move|Replace|Create|OpenWrite)\(\s*" + _LIT,
               re.I),
    re.compile(r"\[(?:System\.)?IO\.File\]::Copy\([^)]*?,\s*" + _LIT, re.I),
]
# the samples folder or samples/blank_forms itself as a string literal (directly after the quote or after a slash)
_DIR_LIT = r"[\"'](?:[^\"']*[\\/])?samples(?:[\\/]+blank_forms)?[\\/]*[\"']"
INLINE_WRITES += [  # folder deletions / moves whose own argument is that folder
    re.compile(r"\b(?:shutil\.)?(?:rmtree|move)\(\s*" + _DIR_LIT, re.I),
    re.compile(r"\b(?:os\.(?:rmdir|removedirs|rename|replace)|rmSync|rmdirSync|renameSync)\(\s*" + _DIR_LIT, re.I),
    re.compile(_DIR_LIT + r"\s*\)\s*\.(?:rmdir|rename|replace)\(", re.I),
    re.compile(r"\[(?:System\.)?IO\.Directory\]::(?:Delete|Move)\(\s*" + _DIR_LIT, re.I),
]
DELETE_VERBS = {"rm", "del", "erase", "unlink", "shred", "remove-item", "ri", "rmdir", "rd"}
MOVE_VERBS = {"mv", "move", "move-item", "mi", "ren", "rename", "rename-item", "rni"}
WRITE_VERBS = DELETE_VERBS | MOVE_VERBS | {"truncate", "set-content", "sc", "add-content", "ac", "clear-content", "clc",
                                           "out-file", "new-item", "ni", "tee", "tee-object"}
COPY_VERBS = {"cp", "copy", "copy-item", "cpi", "xcopy", "robocopy", "install", "rsync", "scp", "ln"}
CMD_VERBS = {"copy", "xcopy", "robocopy", "move", "del", "erase", "ren", "rename", "rd", "rmdir"}  # options look like /Y
LIST_VERBS = {"ls", "dir", "gci", "get-childitem"}  # listings that can feed a pipeline into rm / Remove-Item
_CD_VERBS = {"cd", "chdir", "pushd", "set-location", "sl", "push-location"}
_WRAPPERS = {"sudo", "env", "command", "builtin", "exec", "nohup", "time", "nice", "&", "call", "start"}
GENERATOR_MODULES = ("form_guard.cli", "form_guard")
_PYTHON_VERB_RE = re.compile(r"^(?:python|pythonw|py)(?:\d+(?:\.\d+)*)?$")  # python, python3, python3.13, py …
_GLOB_RE = re.compile(r"[*?\[]")


def is_template_path(path: str) -> bool:
    p = path.replace("\\", "/")
    return bool(re.search(r"(?:^|/)samples/+blank_forms/+[^/]+$", p, re.I)) and p.lower().endswith(TEMPLATE_EXTS)


def _segments(command: str) -> List[str]:
    """Split a shell command into simple commands at ; | & && || and newlines, outside quotes."""
    out, buf, quote = [], [], ""
    for i, ch in enumerate(command):
        if quote:
            quote = "" if ch == quote else quote
        elif ch in "'\"":
            quote = ch
        elif ch == "\n" or ch == ";" or (ch == "|" and command[i - 1:i] != ">") or (
                ch == "&" and command[i - 1:i] not in (">", "<") and command[i + 1:i + 2] != ">"):
            out.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    out.append("".join(buf))
    return [s.strip() for s in out if s.strip()]


def _redirect_targets(segment: str) -> List[str]:
    targets, quote, i = [], "", 0
    while i < len(segment):
        ch = segment[i]
        if quote:
            quote = "" if ch == quote else quote
        elif ch in "'\"":
            quote = ch
        elif ch == ">":
            j = i + 1
            while j < len(segment) and segment[j] in ">|&":
                j += 1
            while j < len(segment) and segment[j] in " \t":
                j += 1
            m = re.match(r"\"([^\"]*)\"|'([^']*)'|([^\s;|&<>]+)", segment[j:])
            if m:
                targets.append(next(g for g in m.groups() if g is not None))
                i = j + m.end()
                continue
        i += 1
    return targets


def _tokens(segment: str) -> List[str]:
    try:
        toks = shlex.split(segment, posix=False)  # keeps Windows back-slashes intact
    except ValueError:
        toks = segment.split()
    return [t[1:-1] if len(t) >= 2 and t[0] == t[-1] and t[0] in "'\"" else t for t in toks]


def _verb(tokens: List[str]) -> Tuple[str, List[str]]:
    i = 0
    while i < len(tokens) and (tokens[i].lower() in _WRAPPERS or re.match(r"^[A-Za-z_]\w*=", tokens[i])):
        i += 1
    if i >= len(tokens):
        return "", []
    verb = tokens[i].replace("\\", "/").rsplit("/", 1)[-1].lower()
    return (verb[:-4] if verb.endswith(".exe") else verb), tokens[i + 1:]


def _positional(verb: str, args: List[str]) -> List[str]:
    out, skip = [], False
    for a in args:
        if skip:
            skip = False
            continue
        if a.lower() in ("-destination", "-path", "-literalpath", "-filepath", "-newname", "-t", "--target-directory"):
            skip = True
            continue
        if a.startswith("-") or (verb in CMD_VERBS and re.match(r"^/[A-Za-z]{1,4}(:\S*)?$", a)):
            continue
        out.append(a)
    return out


def _named(args: List[str], *names: str) -> Optional[str]:
    for k, a in enumerate(args):
        low = a.lower()
        if low in names and k + 1 < len(args):
            return args[k + 1]
        for n in names:
            if n.startswith("--") and low.startswith(n + "="):
                return a.split("=", 1)[1]
    return None


def _strip_comment(segment: str) -> str:
    """The segment without an unquoted ``# …`` comment (a ``#`` that starts a word, as sh and PowerShell read it)."""
    quote = ""
    for i, ch in enumerate(segment):
        if quote:
            quote = "" if ch == quote else quote
        elif ch in "'\"":
            quote = ch
        elif ch == "#" and (i == 0 or segment[i - 1] in " \t"):
            return segment[:i]
    return segment


def _is_generator(segment: str) -> bool:
    """True only for the deterministic sample generator itself: a Python launcher (python, python3, py, a path to
    python.exe …) running ``-m form_guard.cli`` / ``-m form_guard`` / ``form_guard/cli.py`` — or the ``form-guard``
    console script — with --generate-samples / --regenerate-samples as a real argument. A flag in a ``# comment``,
    after another command or in ``python -c`` code is not the generator."""
    verb, args = _verb(_tokens(_strip_comment(segment)))
    if verb != "form-guard":
        if not _PYTHON_VERB_RE.match(verb):
            return False
        rest: Optional[List[str]] = None
        i = 0
        while i < len(args):
            a = args[i]
            if a == "-m":
                if i + 1 < len(args) and args[i + 1] in GENERATOR_MODULES:
                    rest = args[i + 2:]
                break
            if a.startswith("-c"):  # inline code is never the generator
                return False
            if a.startswith("-"):  # interpreter options: -u, -B, -X utf8, -W ignore …
                i += 2 if a in ("-X", "-W") else 1
                continue
            if a.replace("\\", "/").lower().endswith("form_guard/cli.py"):
                rest = args[i + 1:]
            break
        if rest is None:
            return False
        args = rest
    low = [a.lower() for a in args]
    return any(a in GENERATOR_FLAGS for a in low) and not any(a in WRITE_VERBS | COPY_VERBS for a in low)


def _native(raw: str) -> str:
    """A Git Bash path (/c/hackathon/…) as the Windows path it names; anything else unchanged."""
    m = re.match(r"^/([A-Za-z])(?=/|$)", raw)
    return f"{m.group(1)}:{raw[2:] or '/'}" if m and os.name == "nt" else raw


def _could_hold_template(name: str) -> bool:
    """A (lower-case) file name — or a wildcard for one — that is or can match a blank template (.docx / .xlsx / .pdf …)."""
    if not _GLOB_RE.search(name):
        return name.endswith(TEMPLATE_EXTS)
    ext = re.search(r"\.[a-z0-9]+$", name)  # a literal extension at the end of the wildcard decides
    return ext is None or name.endswith(TEMPLATE_EXTS)


def _below_guard(rest: List[str]) -> set:
    """What a path below samples/blank_forms reaches: the folder itself, a template, or nothing protected."""
    if not rest:
        return {"folder"}
    if any(_GLOB_RE.search(r) for r in rest[:-1]) or _could_hold_template(rest[-1]):
        return {"template"}
    return set()


def _reaches(arg: str, cwd: Optional[Path], roots: Iterable[Path], resolve_ancestors: bool = True) -> set:
    """What a path or wildcard argument can reach: ``template`` (a blank form), ``folder`` (samples/blank_forms
    itself) and / or ``ancestor`` (the samples folder or any folder above it). Read from the text — a
    ``blank_forms`` component counts in any directory, ``samples/*`` matches that folder — and, with a known
    ``cwd``, from the resolved path (``.``, ``..``, ``*`` at the project root …)."""
    found: set = set()
    raw = arg.strip().strip("\"'").replace("\\", "/")
    if not raw or raw.startswith(("$", "%", "~", "{")):
        return found
    parts = [c.lower() for c in posixpath.normpath(raw).split("/") if c not in ("", ".")]
    for k, c in enumerate(parts):
        if c == "blank_forms" or (k and parts[k - 1] == "samples" and _GLOB_RE.search(c)
                                  and fnmatchcase("blank_forms", c)):
            found |= _below_guard(parts[k + 1:])
            break
    if parts and parts[-1] == "samples" and (cwd is None or len(parts) == 1):
        found.add("ancestor")
    if cwd is None:
        return found
    p = Path(_native(raw))
    p = Path(os.path.normpath(str(p if p.is_absolute() else cwd / p)))
    full = [s.lower() for s in p.parts]
    for root in roots:
        guard = [s.lower() for s in (root / "samples" / "blank_forms").parts]
        n = min(len(full), len(guard))
        if not all(fnmatchcase(g, a) for a, g in zip(full[:n], guard[:n])):
            continue
        if len(full) > len(guard):
            found |= _below_guard(full[len(guard):])
        elif len(full) == len(guard):
            found.add("folder")
        elif resolve_ancestors:
            found.add("ancestor")
    return found


def _operands(verb: str, args: List[str]) -> List[str]:
    """Path operands: every argument that is not an option — PowerShell parameter values (``-Path X``,
    ``-Path:X``) and ``--opt=X`` values included."""
    out = []
    for a in args:
        if a.startswith("-"):
            m = re.match(r"^-{1,2}[A-Za-z][\w-]*[:=](.+)$", a)
            if m:
                out.append(m.group(1))
            continue
        if verb in CMD_VERBS and re.match(r"^/[A-Za-z]{1,4}(:\S*)?$", a):
            continue
        out.append(a)
    return out


def _move_operands(verb: str, args: List[str]) -> Tuple[List[str], Optional[str]]:
    """(sources, destination) of mv / Move-Item / ren / Rename-Item."""
    dest = _named(args, "-destination", "-newname", "-t", "--target-directory")
    pos = _positional(verb, args)
    sources = [v for v in [_named(args, "-path", "-literalpath")] if v]
    if dest is None and len(pos) >= 2:
        dest, pos = pos[-1], pos[:-1]
    return sources + pos, dest


def _search_reaches(verb: str, args: List[str], cwd: Optional[Path], roots: List[Path]) -> Optional[str]:
    """The start folder of a ``find`` (or a recursive ``ls`` / ``Get-ChildItem``) whose matches can include a blank
    template or its folder, or None (a -name / -Filter that cannot match one keeps it harmless)."""
    low = [a.lower() for a in args]
    if verb == "find":
        starts = []
        for a in args:
            if a.startswith("-") or a in ("(", ")", "!", "\\(", "\\)"):
                break
            starts.append(a)
        names = [args[i + 1] for i, a in enumerate(low[:-1]) if a in ("-name", "-iname", "-path", "-ipath", "-wholename")]
    else:  # ls / dir / Get-ChildItem: what it lists (the folder's entries, or everything below it with -Recurse)
        names = [v for v in [_named(args, "-filter"), _named(args, "-include")] if v]
        starts = [a for a in _operands(verb, args) if a not in names and a != _named(args, "-exclude")]
    if names and not any(_could_hold_template(n.lower()) or fnmatchcase("blank_forms", n.lower())
                         or fnmatchcase("samples", n.lower()) for n in names):
        return None
    return next((s for s in starts or ["."] if _reaches(s, cwd, roots)), None)


def _xargs_verb(args: List[str]) -> str:
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-I", "-n", "-P", "-d", "-L", "-s", "-E", "-a", "--delimiter", "--max-args", "--max-procs",
                 "--arg-file", "--replace"):
            i += 2
        elif a.startswith("-"):
            i += 1
        else:
            return _verb(args[i:])[0]
    return ""


def _consumes_paths(segment: str) -> bool:
    """A command that deletes or moves the paths piped into it: ``xargs rm``, ``Remove-Item`` / ``rm`` without a
    path of its own, ``ForEach-Object { Remove-Item $_ }``."""
    verb, args = _verb(_tokens(segment))
    if verb == "xargs":
        return _xargs_verb(args) in DELETE_VERBS | MOVE_VERBS
    if verb in ("foreach-object", "%", "foreach"):
        return any(a.lower().strip("{}();") in DELETE_VERBS | MOVE_VERBS for a in args)
    return verb in DELETE_VERBS | MOVE_VERBS and not _operands(verb, args)


def _shell_violation(segment: str, cwd: Optional[Path] = None, roots: Iterable[Path] = ()) -> Optional[str]:
    """Why this simple command would change, move or delete a blank template (or its folder), or None. ``cwd`` —
    followed through ``cd`` by the caller — resolves relative paths such as ``.`` or ``*``; without it only the
    paths written in the command are checked."""
    roots = list(roots)
    for target in _redirect_targets(segment):  # never exempt: the generator writes its files itself
        if is_template_path(target) or _reaches(target, cwd, roots, resolve_ancestors=False):
            return f"output redirection into {target}"
    if _is_generator(segment):
        return None  # the deterministic sample generator rebuilds the templates on purpose
    for rx in INLINE_WRITES:
        m = rx.search(segment)
        if m:
            return f"inline file write «{m.group(0)[:80]}»"
    verb, args = _verb(_tokens(segment))
    hits = [a for a in args if TEMPLATE_RE.search(a)]
    if verb in DELETE_VERBS:
        hit = next((a for a in _operands(verb, args) if _reaches(a, cwd, roots)), None)
        if hit:
            return f"{verb} {hit}"
    if verb in MOVE_VERBS:
        sources, dest = _move_operands(verb, args)
        hit = next((a for a in sources if _reaches(a, cwd, roots)), None)
        if hit is None and dest and _reaches(dest, cwd, roots, resolve_ancestors=False):
            hit = dest
        if hit:
            return f"{verb} {hit}"
    if verb in WRITE_VERBS:
        hit = next((a for a in _operands(verb, args) if _reaches(a, cwd, roots, resolve_ancestors=False)), None)
        if hits or hit:
            return f"{verb} {hits[0] if hits else hit}"
    if verb == "find" and ("-delete" in args or any(
            a in ("-exec", "-execdir", "-ok", "-okdir") and _verb(args[k + 1:])[0] in WRITE_VERBS
            for k, a in enumerate(args))):
        start = _search_reaches(verb, args, cwd, roots)
        if start:
            return f"find {start} … deletes or moves what it finds"
    if verb in COPY_VERBS:
        pos = _positional(verb, args)
        dest = _named(args, "-destination", "-t", "--target-directory")
        sources = pos + [v for v in [_named(args, "-path", "-literalpath")] if v]
        if dest is None and len(pos) >= 2:
            dest, sources = (pos[1], pos[2:]) if verb == "robocopy" else (pos[-1], pos[:-1])
        reach = _reaches(dest, cwd, roots, resolve_ancestors=False) if dest else set()
        into_dir = dest is not None and (TEMPLATE_DIR_RE.search(dest.rstrip("/\\") + "/") is not None or "folder" in reach)
        if dest and (is_template_path(dest) or "template" in reach or (into_dir and (verb == "robocopy" or any(
                s.lower().endswith(TEMPLATE_EXTS) or "*" in s for s in sources)))):
            return f"{verb} into {dest}"
    if verb == "dd" and any(a.startswith("of=") and is_template_path(a[3:]) for a in args):
        return "dd of=" + next(a[3:] for a in args if a.startswith("of="))
    if verb in ("sed", "perl", "ruby") and hits and any(
            (a.startswith("-") and not a.startswith("--") and "i" in a) or a.startswith("--in-place") for a in args):
        return f"{verb} in-place edit of {hits[0]}"
    pos = _positional(verb, args)
    if verb == "zip" and pos and is_template_path(pos[0]):
        return f"zip updates {pos[0]} in place"
    if verb in ("7z", "7za", "7zz") and len(pos) >= 2 and pos[0].lower() in ("a", "u", "d", "rn") and is_template_path(pos[1]):
        return f"7z {pos[0]} updates {pos[1]} in place"
    if verb == "git" and pos[:1] in (["rm"], ["mv"]):
        sources, dest = (pos[1:], None) if pos[0] == "rm" else _move_operands("mv", args[args.index(pos[0]) + 1:])
        hit = hits[0] if hits else next((a for a in sources if _reaches(a, cwd, roots)), None)
        if hit is None and dest and _reaches(dest, cwd, roots, resolve_ancestors=False):
            hit = dest
        if hit:
            return f"git {pos[0]} {hit}"
    return None


def _after_cd(segment: str, cwd: Optional[Path]) -> Optional[Path]:
    """The working directory after ``cd`` / ``pushd`` / ``Set-Location`` (None once it can no longer be followed)."""
    verb, args = _verb(_tokens(segment))
    if verb not in _CD_VERBS:
        return cwd
    target = _named(args, "-path", "-literalpath") or next((a for a in args if not a.startswith("-")), None)
    if not target or target == "-" or target.startswith(("$", "%", "~")) or _GLOB_RE.search(target):
        return None
    p = Path(_native(target.replace("\\", "/")))
    if not p.is_absolute() and cwd is None:
        return None
    return Path(os.path.normpath(str(p if p.is_absolute() else cwd / p)))


def _shell_command_violation(command: str, payload: Optional[dict] = None) -> Tuple[Optional[str], str]:
    """Check each simple command of a shell command line, following ``cd`` from the hook's ``cwd``, and the pipelines
    that feed a listing of the blank templates into ``rm`` / ``Remove-Item`` / ``xargs rm``."""
    payload = payload or {}
    roots = _project_roots(payload)
    cwd: Optional[Path] = Path(os.path.abspath(str(payload.get("cwd") or os.getcwd())))
    steps: List[Tuple[str, Optional[Path]]] = []
    for segment in _segments(command):
        why = _shell_violation(segment, cwd, roots)
        if why:
            return why, segment
        steps.append((segment, cwd))
        cwd = _after_cd(segment, cwd)
    if "|" in command and any(_consumes_paths(s) for s, _ in steps):  # e.g. Get-ChildItem samples\blank_forms | Remove-Item
        for segment, at in steps:
            verb, args = _verb(_tokens(segment))
            if _consumes_paths(segment) or not verb:
                continue
            if verb == "find" or verb in LIST_VERBS:
                start = _search_reaches(verb, args, at, roots)
                if start:
                    return f"{verb} {start} piped into a delete / move", segment
            hit = next((a for a in _operands(verb, args) if _reaches(a, at, roots, resolve_ancestors=False)), None)
            if hit:
                return f"{hit} piped into a delete / move", segment
    return None, ""


def template_violation(tool: str, tool_input: dict, payload: Optional[dict] = None) -> Tuple[Optional[str], str]:
    if tool in WRITE_TOOLS:
        target = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
        if target and is_template_path(target):
            return f"{tool} tool", target
    elif tool in SHELL_TOOLS:
        why, segment = _shell_command_violation(str(tool_input.get("command") or ""), payload)
        if why:
            return f"{tool} command: {why}", segment[:200]
    return None, ""


# ------------------------------------------------------------------ stage 1b: secret & hygiene guard


def incoming_text(tool: str, tool_input: dict) -> str:
    """The text a write tool is about to put on disk."""
    parts: List[str] = []
    if tool == "Write":
        parts.append(str(tool_input.get("content") or ""))
    elif tool == "Edit":
        parts.append(str(tool_input.get("new_string") or ""))
    elif tool == "MultiEdit":
        for e in tool_input.get("edits") or []:
            if isinstance(e, dict):
                parts.append(str(e.get("new_string") or ""))
    elif tool == "NotebookEdit":
        parts.append(str(tool_input.get("new_source") or ""))
    return "\n".join(p for p in parts if p)


def hygiene_violation(tool: str, tool_input: dict, payload: dict) -> Tuple[Optional[str], str]:
    if tool in SHELL_TOOLS:
        found = hygiene_findings(str(tool_input.get("command") or ""), secrets_only=True)
        return (f"{tool} command contains {_format(found, 'command')}", "command") if found else (None, "")
    if tool not in WRITE_TOOLS:
        return None, ""
    raw = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
    if not raw:
        return None, ""
    path = _resolve(raw, payload)
    root = _inside(path, _project_roots(payload))
    if root is None or _hygiene_exempt(path):
        return None, ""
    found = hygiene_findings(incoming_text(tool, tool_input))
    if not found:
        return None, ""
    shown = path.relative_to(root).as_posix()
    return f"{tool} would write {_format(found, shown)}", shown


def _deny(reason: str, detail: str) -> int:
    _emit({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                  "permissionDecisionReason": reason},
           "decision": "block", "reason": reason})
    _stderr(detail)
    return 2


def pre_tool_use(payload: dict) -> int:
    tool = str(payload.get("tool_name") or "")
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return 0
    reason, target = template_violation(tool, tool_input, payload)
    if reason is not None:
        if os.environ.get(OVERRIDE_ENV) == "1":
            _stderr(f"form-guard template guard: allowed by {OVERRIDE_ENV}=1 ({reason})")
        else:
            short = (f"BLOCKED by the Form-Guard template guard (scripts/hook_guard.py) — {reason}. Blank forms in "
                     "samples/blank_forms/ are never written, moved or deleted in place; fill a COPY with "
                     "python -m form_guard.cli --form <blank form> --memo <memo> --out-dir output/<run>, or rebuild "
                     "the samples with python -m form_guard.cli --generate-samples.")
            return _deny(short, (
                f"BLOCKED by the Form-Guard template guard (scripts/hook_guard.py) — {reason}\n"
                f"  target: {target}\n"
                "Blank forms in samples/blank_forms/ are the untouched originals that every structural proof compares "
                "against; agents never write, move or delete them in place.\n"
                "  • Fill a form (writes a filled COPY): python -m form_guard.cli --form <blank form> --memo <memo> "
                "--out-dir output/<run>\n"
                "  • Rebuild all samples deterministically: python -m form_guard.cli --generate-samples\n"
                f"  • A human who really means it can start Claude Code with {OVERRIDE_ENV}=1."))
    reason, _shown = hygiene_violation(tool, tool_input, payload)
    if reason is not None:
        short = (f"BLOCKED by the Form-Guard secret & hygiene guard (scripts/hook_guard.py) before writing — {reason}. "
                 "Keep the API key only in the git-ignored .env (ANTHROPIC_API_KEY=…), use placeholders such as "
                 "C:\\Users\\<user> for paths and the reserved example.jp / example.com / example.org domains for "
                 "e-mail addresses, then retry.")
        return _deny(short, short)
    return 0


# ------------------------------------------------------------------ stage 2: PostToolUse checks


def _package_root(path: Path) -> Optional[Path]:
    for parent in path.parents:
        if (parent / "form_guard" / "__init__.py").is_file():
            return parent
    return None


def _interpreter(root: Path) -> str:
    override = os.environ.get("FORM_GUARD_HOOK_PYTHON")
    if override:
        return override
    for base in (root, ROOT, Path(os.environ.get("CLAUDE_PROJECT_DIR") or root)):
        for rel in ("Scripts/python.exe", "bin/python3", "bin/python"):
            if (base / ".venv" / rel).is_file():
                return str(base / ".venv" / rel)
    return sys.executable


def _module_name(rel: Path) -> str:
    if rel.parts[0].lower() != "form_guard":
        return "form_guard"  # tests/…: the package they exercise must still import
    if rel.name.lower() == "__main__.py":
        return "form_guard.cli"  # importing __main__ would run the CLI
    mod = ".".join(rel.with_suffix("").parts)
    return mod[: -len(".__init__")] if mod.lower().endswith(".__init__") else mod


_IMPORT_PROBE = """import importlib, sys
try:
    importlib.import_module(sys.argv[1])
except ModuleNotFoundError as e:
    top = (e.name or "").split(".")[0]
    if top and top != "form_guard":
        print(top)
        sys.exit(3)
    raise
"""


def _import_check(python: str, module: str, root: Path, budget: float) -> Tuple[str, str]:
    if budget <= 0.05:
        return "skipped", "import check skipped: time budget used up"
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    try:
        proc = subprocess.run([python, "-c", _IMPORT_PROBE, module], cwd=str(root), env=env, capture_output=True,
                              timeout=budget)
    except subprocess.TimeoutExpired:
        return "skipped", f"import check timed out after {budget:.1f}s"
    except OSError as exc:
        return "skipped", f"interpreter {python} could not start ({exc})"
    if proc.returncode == 0:
        return "ok", f"import {module} OK"
    out = proc.stdout.decode("utf-8", "replace").strip()
    if proc.returncode == 3:
        return "skipped", f"import check skipped: optional dependency '{out}' is not installed for {python}"
    lines = [ln for ln in proc.stderr.decode("utf-8", "replace").strip().splitlines() if ln.strip()]
    return "failed", f"import {module} failed: {lines[-1] if lines else f'exit code {proc.returncode}'}"


def _python_checks(path: Path, shown: str, started: float) -> Tuple[List[str], List[str]]:
    problems, notes = [], []
    try:
        with tempfile.TemporaryDirectory() as td:  # compile without leaving a .pyc behind
            py_compile.compile(str(path), cfile=os.path.join(td, "check.pyc"), doraise=True)
    except py_compile.PyCompileError as exc:
        msg = str(exc.msg).replace(str(path), shown)  # report the project-relative name, not a local absolute path
        problems.append(f"{shown} does not compile — {' '.join(msg.split())[:600]}")
        return problems, notes
    notes.append("py_compile OK")
    root = _package_root(path)
    if root is None:
        return problems, notes
    rel = path.relative_to(root)
    if rel.parts[0].lower() not in ("form_guard", "tests"):
        return problems, notes
    module = _module_name(rel)
    status, detail = _import_check(_interpreter(root), module, root, POST_BUDGET_S - (time.perf_counter() - started))
    if status == "failed":
        problems.append(f"{shown} compiles but {detail}")
    else:
        notes.append(detail)
    return problems, notes


def _json_check(data: bytes, shown: str) -> Optional[str]:
    if data.startswith(b"\xef\xbb\xbf"):
        return f"{shown} starts with a UTF-8 BOM — JSON readers such as Claude Code reject it"
    try:
        json.loads(data.decode("utf-8"))
    except UnicodeDecodeError as exc:
        return f"{shown} is not UTF-8 ({exc.reason} at byte {exc.start})"
    except ValueError as exc:
        return f"{shown} is not valid JSON — {exc}"
    return None


def post_tool_use(payload: dict) -> int:
    started = time.perf_counter()
    tool = str(payload.get("tool_name") or "")
    tool_input = payload.get("tool_input") or {}
    if tool not in ("Write", "Edit", "MultiEdit") or not isinstance(tool_input, dict):
        return 0
    raw = str(tool_input.get("file_path") or "")
    if not raw:
        return 0
    path = _resolve(raw, payload)
    root = _inside(path, _project_roots(payload))
    if root is None or not path.is_file():
        return 0
    shown = path.relative_to(root).as_posix()
    problems: List[str] = []
    notes: List[str] = []
    data = b""
    if path.stat().st_size <= MAX_SCAN_BYTES:
        data = path.read_bytes()
    if _ext(path, ".py"):  # case-insensitive: x.PY too
        p, n = _python_checks(path, shown, started)
        problems += p
        notes += n
    elif _ext(path, ".json") and data:  # x.JSON too
        bad = _json_check(data, shown)
        if bad:
            problems.append(bad)
        else:
            notes.append("JSON OK")
    if data and not _hygiene_exempt(path) and (_ext(path, *TEXT_EXTS) or path.name.startswith(".")):
        text = data.decode("utf-8", "replace")
        found = hygiene_findings(text)
        if found:
            problems.append("hygiene: " + _format(found, shown))
        elif notes:
            notes.append("hygiene OK")
    elapsed = time.perf_counter() - started
    if problems:
        _emit({"decision": "block",
               "reason": "form-guard post-write check (scripts/hook_guard.py): " + " | ".join(problems)
                         + ". Fix it before continuing.",
               "systemMessage": f"✗ form-guard check: {shown} — {problems[0][:160]} ({elapsed:.2f}s)"})
    elif notes:
        _emit({"systemMessage": f"✓ form-guard check: {shown} · {' · '.join(notes)} ({elapsed:.2f}s)"})
    return 0


# ------------------------------------------------------------------ entry point


def main(argv: Optional[List[str]] = None) -> int:
    _force_utf8()
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] not in ("pre-tool-use", "post-tool-use"):
        _stderr("usage: hook_guard.py pre-tool-use|post-tool-use  (reads the Claude Code hook JSON on stdin)")
        return 1
    try:
        raw = sys.stdin.buffer.read()
        payload = json.loads(raw.decode("utf-8-sig") or "{}")
    except (ValueError, UnicodeDecodeError, OSError):
        _stderr("form-guard hook: unreadable hook payload — skipped")
        return 0
    if not isinstance(payload, dict):
        return 0
    try:
        return pre_tool_use(payload) if argv[0] == "pre-tool-use" else post_tool_use(payload)
    except Exception as exc:  # a bug in the hook must never block the session
        _stderr(f"form-guard hook internal error ({type(exc).__name__}: {exc}) — skipped")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
