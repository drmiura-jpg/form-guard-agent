"""Form-Guard MCP server: the Form-Guard tools over the Model Context Protocol (stdio, JSON-RPC 2.0).

    python -m form_guard.mcp_server             # built-in stdio server, no extra dependencies
    python -m form_guard.mcp_server --fastmcp   # the same tools through the official `mcp` SDK (pip install mcp)
    python -m form_guard.cli --mcp              # the built-in server, started from the CLI

Claude Code picks it up from ``.mcp.json`` (server name ``form-guard``). Messages may be newline-delimited
JSON (the MCP stdio transport) or Content-Length framed; every answer uses the framing of its request.

Tools: ``form_guard_list_demos``, ``form_guard_scan``, ``form_guard_discover_blank_cells``, ``form_guard_fill``,
``form_guard_verify`` — for .docx / .xlsx and fixed .pdf forms alike (a PDF is filled by an incremental update).

Human approval is never bypassed: ``form_guard_fill`` only writes an unapproved draft into
``output/mcp_runs/<run id>/`` — never inside ``samples/`` and never over a blank form — and marks it
``UNAPPROVED_DRAFT_REQUIRES_HUMAN_SIGN_OFF`` in the tool result, in ``APPROVAL_STATUS.json`` and in
``audit_log.json``. The .docx/.xlsx itself is not watermarked: changing the document body would break the very
proof that nothing but the designated fields changed. Values attributed to a human reviewer (``--set`` in the
CLI) cannot be sent through MCP, and office-only cells can be neither confirmed nor designated in a slot spec.
The draft folder also holds ``web_handoff.json``: the result's ``review_url`` (``http://127.0.0.1:8000/?run_id=…``,
web app started with ``python -m form_guard.cli --serve``) loads that exact file — re-verified, no model run — for
the named reviewer's sign-off. Paths are resolved against the server root (the working directory, or
``FORM_GUARD_MCP_ROOT``) and must stay inside it; ``.env`` files, ``.git``, ``.ssh`` and ``.claude-hackathon`` are
refused in any path component. ``mode`` defaults to ``demo`` (offline, no network): ``live`` / ``auto`` send the
memo to the Claude API and must be asked for explicitly.

Transport hardening: stdout carries JSON-RPC only. On the real stdio, file descriptor 1 is pointed at stderr and
protocol messages go to a private duplicate of the original stdout, so a stray print from any library — even a C
extension — cannot corrupt the stream. Messages are UTF-8 bytes on the wire whatever the console code page
(cp932, cp1252 …), and stderr is reconfigured to UTF-8. A message larger than 10 MB (a ``Content-Length`` or a
newline-delimited line), a negative or malformed ``Content-Length`` and a body cut short by EOF are answered with a
JSON-RPC parse error instead of being read. If this interpreter lacks the runtime dependencies, the server
re-launches itself with the project's ``.venv``.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Iterable, Optional

if __package__ in (None, ""):  # python form_guard/mcp_server.py: run the package module itself
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    if __name__ == "__main__":
        from form_guard.mcp_server import main as _main

        raise SystemExit(_main())
    __package__ = "form_guard"

from . import __version__

PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
APPROVAL_STATUS = "UNAPPROVED_DRAFT_REQUIRES_HUMAN_SIGN_OFF"
MAX_MEMO_CHARS = 60_000
MAX_MCP_MESSAGE_BYTES = 10 * 1024 * 1024  # one JSON-RPC message (Content-Length body or newline-delimited line)
MAX_HEADER_LINES = 32
REVIEW_URL = "http://127.0.0.1:8000/?run_id={run_id}"
PARSE_ERROR, INVALID_REQUEST = -32700, -32600
# never readable or writable through a tool, in any path component (case-insensitive)
FORBIDDEN_PATH_PARTS = frozenset({".git", ".ssh", ".claude-hackathon"})
RUNTIME_DEPENDENCIES = ("pydantic", "lxml", "openpyxl", "docx")
SERVER_INFO = {"name": "form-guard", "title": "Form-Guard Agent", "version": __version__}
INSTRUCTIONS = (
    "Form-Guard fills only the designated fields of .docx/.xlsx forms and fixed .pdf forms and proves nothing else "
    "changed (a PDF is filled by an incremental update: the blank file stays an exact byte prefix). "
    "Use form_guard_list_demos to find the fictional demo forms and form_guard_scan to list slots; for an unmarked "
    "blank form or a fixed PDF use form_guard_discover_blank_cells, show the candidates to the user and let them choose; "
    "form_guard_fill writes an UNAPPROVED draft — tell the user that a named human reviewer must sign it off "
    "(python -m form_guard.cli --serve, then open the review_url from the result) before it is used; "
    "form_guard_verify audits any filled copy against its blank original. mode defaults to demo (offline); use live "
    "only when the user asks for Claude. All sample forms are fictional; do not use real personal data."
)

_MODE = {"type": "string", "enum": ["auto", "demo", "live"]}
_TIER = {"type": "string", "enum": ["auto", "opus", "sonnet", "haiku"],
         "description": "Claude model tier in live mode: auto = Haiku for structured fields, Sonnet for standard text, "
                        "Opus for long text and every self-correction (a rejected Haiku/Sonnet draft escalates to Opus)"}
_PATH = {"type": "string"}
_READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
# live / auto may send the memo and the printed labels to the Claude API (an external service)
_READ_ONLY_OPEN = {**_READ_ONLY, "openWorldHint": True}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "form_guard_list_demos",
        "title": "List the fictional demo forms",
        "description": "List the 6 built-in demo forms (100% fictional, non-medical) with paths relative to the server "
                       "root: 3 MARKED forms whose fields are designated by {{markers}}, [記入欄: …] or cell coordinates "
                       "(form1 .docx, form2 .docx, form3 .xlsx — each with its memo, slot spec and a simulated naive AI "
                       "regeneration), 2 UNMARKED blank forms (form4 .docx, form5 .xlsx) and 1 FIXED PDF form (form6 "
                       ".pdf, a study-group grant application with a printed □ 合意済み / □ 協議中 checkbox) that need "
                       "form_guard_discover_blank_cells and the user's confirmation before form_guard_fill (form6 also "
                       "ships a .slots.json designating the same boxes by page coordinates). Read-only.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": _READ_ONLY,
    },
    {
        "name": "form_guard_scan",
        "title": "Scan a form for fillable slots",
        "description": "List the fillable slots of a .docx/.xlsx/.pdf form ({{markers}}, [記入欄: …], cell coordinates "
                       "or PDF page boxes in a .slots.json, printed □ checkbox options) with the rules printed on the form "
                       "(character limits, 和暦, digits, caps, drop-down / checkbox options) and the Claude model live "
                       "auto mode would route each slot to. Read-only.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "form_path": {**_PATH, "description": "blank form, relative to the server root"},
                "slot_spec_path": {**_PATH, "description": "optional .slots.json (aliases, coordinate targets)"},
            },
            "required": ["form_path"],
            "additionalProperties": False,
        },
        "annotations": _READ_ONLY,
    },
    {
        "name": "form_guard_discover_blank_cells",
        "title": "Discover empty cells of an unmarked blank form",
        "description": "4-stage discovery for a form without markers (.docx/.xlsx, or a fixed .pdf whose boxes are "
                       "rebuilt from its ruled lines): (1) empty cells — and cells printing □ checkbox options — next to "
                       "a printed label, (2) applicant_fillable vs office_only (事務局・受付印・受付番号・審査欄・決裁…) with "
                       "unique slot ids and printed rules, (3) deterministic safety checks (formula and merged-continuation "
                       "cells are rejected), (4) a report for HUMAN confirmation. Nothing is written. Read-only.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "form_path": {**_PATH, "description": "unmarked blank form, relative to the server root"},
                "mode": {**_MODE, "default": "demo",
                         "description": "demo (default) = deterministic rules, offline; live / auto = Claude also "
                                        "classifies the printed labels (sends them to the Claude API) — explicit "
                                        "opt-in only"},
                "model_tier": {**_TIER, "default": "auto"},
                "expected_sha256": {
                    "type": "string",
                    "description": "optional SHA-256 of the downloaded blank form to verify before discovery",
                },
            },
            "required": ["form_path"],
            "additionalProperties": False,
        },
        "annotations": _READ_ONLY_OPEN,
    },
    {
        "name": "form_guard_fill",
        "title": "Fill a form into an unapproved draft",
        "description": "Run scan → grounded mapping with self-correction → surgical write → 21-check verification into "
                       "output/mcp_runs/<run id>/ (never inside samples/, never over the blank form). The result is an "
                       f"UNAPPROVED DRAFT (approval_status {APPROVAL_STATUS}, also recorded in audit_log.json and "
                       "APPROVAL_STATUS.json) with the verification report, the model that drafted every field and the "
                       "HITL checklist; a named human reviewer must sign it off before use — the result's review_url "
                       "opens this exact draft in the web app (python -m form_guard.cli --serve). For an unmarked form "
                       "pass the discovery report and the candidate ids the user confirmed (office-only cells are "
                       "always refused, also when a slot spec designates them).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "form_path": {**_PATH, "description": "blank form, relative to the server root"},
                "memo_text": {"type": "string", "description": "source memo text (or use memo_path)"},
                "memo_path": {**_PATH, "description": "UTF-8 memo file, relative to the server root"},
                "slot_spec_path": {**_PATH, "description": "optional .slots.json"},
                "discovery": {"type": "object", "description": "report returned by form_guard_discover_blank_cells"},
                "discovery_path": {**_PATH, "description": "discovery report saved as JSON (alternative to discovery)"},
                "confirmed_ids": {"type": "array", "items": {"type": "string"},
                                  "description": "candidate ids the user confirmed, e.g. [\"c01\", \"c02\"]"},
                "mode": {**_MODE, "default": "demo",
                         "description": "demo (default) = the offline engine, no network; live = Claude (sends the memo "
                                        "to the Claude API); auto = Claude if ANTHROPIC_API_KEY is set, else offline — "
                                        "live / auto are an explicit opt-in"},
                "model_tier": {**_TIER, "default": "auto"},
                "auto_adapt": {"type": "boolean", "default": False,
                               "description": "automatically discover blank cells on unmarked forms (bound to blank form "
                                              "SHA-256) and self-heal VIS-01 layout overflows by tightening max_chars"},
            },
            "required": ["form_path"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
    },
    {
        "name": "form_guard_verify",
        "title": "Verify a filled copy against its blank original",
        "description": "Re-run the deterministic checks (archive safety, ZIP/SHA-256, XML skeleton, tables/borders/merges, "
                       "boilerplate text, designated cells, printed rules) plus VIS-01 layout drift / cell overflow on any "
                       "filled .docx/.xlsx — or, for a fixed .pdf, the same 21 check ids (blank file an exact byte prefix, "
                       "original content stream SHA-256, page dictionary keys, overlay only inside the slot boxes, "
                       "office-only boxes untouched) plus a pixel diff outside the boxes. Grounding is checked against "
                       "memo_text / memo_path, or a matching audit_log.json next to the filled file. Read-only.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "original_path": {**_PATH, "description": "blank original, relative to the server root"},
                "filled_path": {**_PATH, "description": "filled copy, relative to the server root"},
                "memo_text": {"type": "string"},
                "memo_path": _PATH,
                "slot_spec_path": _PATH,
            },
            "required": ["original_path", "filled_path"],
            "additionalProperties": False,
        },
        "annotations": _READ_ONLY,
    },
]
TOOL_NAMES = [t["name"] for t in TOOLS]


class ToolError(Exception):
    """A tool-level failure, reported to the client as a result with isError: true."""


def scan_summary(r, rel=None) -> dict[str, Any]:
    """JSON view of a ScanResult — the ``form_guard_scan`` result and ``python -m form_guard.cli --scan --json``."""
    from .constraints import FORMAT_GUIDE
    from .mapper import TIER_MODELS, route_slot
    from .scanner import sha256_file

    out = {
        "form": rel(r.path) if rel else str(r.path), "file_type": r.file_type, "title": r.title,
        "form_sha256": sha256_file(r.path), "target_parts": list(r.target_parts), "slot_count": len(r.slots),
        "read_only": True, "slots": [], "warnings": list(r.warnings),
    }
    for s in r.slots:
        tier, why = route_slot(s, r.slots, "")
        out["slots"].append({
            "slot_id": s.slot_id, "label": s.label, "marker_kind": s.occurrences[0].marker_kind if s.occurrences else "",
            "required_format": s.required_format, "format_guide": FORMAT_GUIDE.get(s.required_format or "text", ""),
            "max_chars": s.max_chars, "constraints": {k: v for k, v in s.constraints.items() if not k.startswith("_")},
            "context_label": s.context_label, "context_on_form": s.context_label,
            "hints_printed_on_form": s.hints[:3], "importance": s.importance, "location": s.location_path,
            "model_tier": tier, "planned_model": TIER_MODELS[tier], "route_reason": why,
            "occurrences": [{"location": o.location_path, "marker": o.marker, "marker_kind": o.marker_kind,
                             **({"cell": o.cell_ref, "sheet": o.sheet_name} if o.cell_ref else {})}
                            for o in s.occurrences],
        })
    if not r.slots:
        out["hint"] = "0 slots — for an unmarked blank form call form_guard_discover_blank_cells"
    return out


class FormGuardMCP:
    def __init__(self, root: Optional[Path] = None, client: Any = None):
        self.root = Path(root or os.environ.get("FORM_GUARD_MCP_ROOT") or Path.cwd()).resolve()
        self.out_root = self.root / "output" / "mcp_runs"
        self.client = client  # a Claude client for live mode (tests pass a fake one)
        self.protocol_version = PROTOCOL_VERSIONS[0]

    # ----------------------------------------------------------------- JSON-RPC

    def handle_payload(self, payload: Any) -> Any:
        if isinstance(payload, list):  # batch (2025-03-26)
            out = [r for r in (self.handle(m) for m in payload) if r is not None]
            return out or None
        return self.handle(payload)

    def handle(self, msg: Any) -> Optional[dict[str, Any]]:
        if not isinstance(msg, dict):
            return _error(None, -32600, "Invalid Request")
        method, mid = msg.get("method"), msg.get("id")
        if not isinstance(method, str):
            return None if "id" not in msg or "result" in msg or "error" in msg else _error(mid, -32600, "Invalid Request")
        if "id" not in msg:  # notification (notifications/initialized, notifications/cancelled, …): no answer
            return None
        params = msg.get("params") or {}
        if not isinstance(params, dict):
            return _error(mid, -32602, "params must be an object")
        if method == "initialize":
            wanted = params.get("protocolVersion")
            self.protocol_version = wanted if wanted in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
            return _result(mid, {"protocolVersion": self.protocol_version,
                                 "capabilities": {"tools": {"listChanged": False}},
                                 "serverInfo": SERVER_INFO, "instructions": INSTRUCTIONS})
        if method == "ping":
            return _result(mid, {})
        if method == "tools/list":
            return _result(mid, {"tools": TOOLS})
        if method == "tools/call":
            name, args = params.get("name"), params.get("arguments") or {}
            if name not in TOOL_NAMES:
                return _error(mid, -32602, f"Unknown tool: {name}")
            if not isinstance(args, dict):
                return _error(mid, -32602, "arguments must be an object")
            return _result(mid, self.call_tool(name, args))
        return _error(mid, -32601, f"Method not found: {method}")

    def call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        handler = {"form_guard_list_demos": self.tool_list_demos, "form_guard_scan": self.tool_scan,
                   "form_guard_discover_blank_cells": self.tool_discover, "form_guard_fill": self.tool_fill,
                   "form_guard_verify": self.tool_verify}[name]
        schema = next(t["inputSchema"] for t in TOOLS if t["name"] == name)
        try:
            unknown = sorted(set(args) - set(schema["properties"]))
            if unknown:
                raise ToolError(f"unknown argument(s) for {name}: {', '.join(unknown)}")
            missing = [k for k in schema.get("required", []) if args.get(k) in (None, "")]
            if missing:
                raise ToolError(f"missing required argument(s): {', '.join(missing)}")
            with contextlib.redirect_stdout(sys.stderr):  # stdout carries protocol messages only
                data = json.loads(json.dumps(handler(args), ensure_ascii=False, default=str))
        except ToolError as exc:
            return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        except Exception as exc:  # noqa: BLE001 - every failure is reported to the client, never crashes the server
            return {"content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}], "isError": True}
        return {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False, indent=1)}],
                "structuredContent": data, "isError": False}

    # -------------------------------------------------------------------- helpers

    @staticmethod
    def _forbidden(parts: Iterable[str]) -> bool:
        """.env / .env.* files, .git, .ssh and the .claude-hackathon config folder, in any path component."""
        for part in parts:
            low = part.strip().lower()
            if low == ".env" or low.startswith(".env.") or low in FORBIDDEN_PATH_PARTS:
                return True
        return False

    def _path(self, value: Any, what: str) -> Path:
        if not isinstance(value, str) or not value.strip():
            raise ToolError(f"{what} is required")
        p = Path(value)
        if self._forbidden(p.parts):
            raise ToolError("access to .env or hidden credential files is forbidden")
        p = (p if p.is_absolute() else self.root / p).resolve()
        if not p.is_relative_to(self.root):
            raise ToolError(f"{what} must be inside the server root")
        if self._forbidden(p.relative_to(self.root).parts):  # e.g. a link that resolves into .git/ or .env
            raise ToolError("access to .env or hidden credential files is forbidden")
        if not p.is_file():
            raise ToolError(f"{what} not found: {value}")
        return p

    def _rel(self, p: Path) -> str:
        p = Path(p).resolve()
        return p.relative_to(self.root).as_posix() if p.is_relative_to(self.root) else p.name

    def _memo(self, args: dict[str, Any], required: bool) -> Optional[str]:
        from .samples import read_memo

        if args.get("memo_text") is not None:
            text = str(args["memo_text"])
        elif args.get("memo_path"):
            text = read_memo(self._path(args["memo_path"], "memo_path"))
        elif required:
            raise ToolError("memo_text or memo_path is required")
        else:
            return None
        if len(text) > MAX_MEMO_CHARS:
            raise ToolError(f"memo longer than {MAX_MEMO_CHARS:,} characters")
        return text.replace("\r\n", "\n").replace("\r", "\n")

    def _spec(self, args: dict[str, Any]) -> Optional[dict[str, Any]]:
        if not args.get("slot_spec_path"):
            return None
        try:
            data = json.loads(self._path(args["slot_spec_path"], "slot_spec_path").read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ToolError(f"slot spec is not valid JSON: {exc}") from exc
        if not isinstance(data, dict):  # a JSON string would otherwise be opened as a second, unchecked path
            raise ToolError("slot spec must be a JSON object")
        return data

    def _samples_dir(self) -> Path:
        from .samples import default_samples_dir

        local = self.root / "samples"
        return local if (local / "blank_forms").is_dir() else default_samples_dir()

    def _run_dir(self, run_id: str) -> Path:
        """output/mcp_runs/<run id> — refused if the server root would put it inside a samples/ folder."""
        from .samples import default_samples_dir

        out = (self.out_root / run_id).resolve()
        protected = {(self.root / "samples").resolve(), default_samples_dir().resolve()}
        in_samples = any(out == p or out.is_relative_to(p) for p in protected) or "blank_forms" in out.parts or \
            any((parent / "blank_forms").is_dir() for parent in out.parents)  # a samples folder used as the root
        if in_samples:
            raise ToolError("refusing to write a draft inside samples/ — the blank form templates are read-only")
        return out

    @staticmethod
    def _checks(report) -> list[dict[str, Any]]:
        return [{"check_id": c.check_id, "passed": c.passed, "description": c.description, "details": c.details[:300],
                 **({"not_verified": True} if c.evidence.get("not_verified") else {})}
                for c in report.checks]

    # ---------------------------------------------------------------------- tools

    def tool_list_demos(self, args: dict[str, Any]) -> dict[str, Any]:
        from .samples import DEMOS, PDF_FORMS, UNMARKED_FORMS, demo_paths, pdf_paths, unmarked_paths

        base = self._samples_dir()

        def where(p: Path) -> dict[str, Any]:
            p = Path(p)
            return {"path": self._rel(p) if p.resolve().is_relative_to(self.root) else str(p), "exists": p.is_file()}

        demos = []
        for d in DEMOS:
            p = demo_paths(d, base)
            spec = p["form"].with_suffix(".slots.json")
            demos.append({"id": d["id"], "title": d["title"], "title_en": d.get("title_en", ""), "org": d.get("org", ""),
                          "file_type": d["file_type"], "marked": True, "unmarked": False,
                          "designation": "{{markers}} / [記入欄: …] / coordinate slots", "next_tool": "form_guard_scan",
                          "form_path": where(p["form"])["path"], "memo_path": where(p["memo"])["path"],
                          "slot_spec_path": where(spec)["path"] if spec.is_file() else None,
                          "naive_path": where(p["naive"])["path"], "highlights": list(d.get("highlights", [])),
                          "files_present": all(x.is_file() for x in (p["form"], p["memo"]))})
        for u in UNMARKED_FORMS:
            p = unmarked_paths(u, base)
            demos.append({"id": u["id"], "title": u["title"], "title_en": u.get("title_en", ""), "org": u.get("org", ""),
                          "file_type": u["file_type"], "marked": False, "unmarked": True,
                          "designation": "none — blank-cell discovery + human confirmation",
                          "next_tool": "form_guard_discover_blank_cells",
                          "form_path": where(p["form"])["path"], "memo_path": where(p["memo"])["path"],
                          "slot_spec_path": None, "naive_path": None, "highlights": list(u.get("highlights", [])),
                          "files_present": all(x.is_file() for x in (p["form"], p["memo"]))})
        for f in PDF_FORMS:
            p = pdf_paths(f, base)
            spec = p["form"].with_suffix(".slots.json")
            demos.append({"id": f["id"], "title": f["title"], "title_en": f.get("title_en", ""), "org": f.get("org", ""),
                          "file_type": f["file_type"], "marked": False, "unmarked": True, "pdf": True,
                          "designation": "fixed PDF — blank-box discovery + human confirmation (the .slots.json "
                                         "designates the same boxes by page coordinates)",
                          "next_tool": "form_guard_discover_blank_cells",
                          "form_path": where(p["form"])["path"], "memo_path": where(p["memo"])["path"],
                          "slot_spec_path": where(spec)["path"] if spec.is_file() else None,
                          "naive_path": where(p["naive"])["path"], "highlights": list(f.get("highlights", [])),
                          "portal": "http://127.0.0.1:8000/portal",
                          "files_present": all(x.is_file() for x in (p["form"], p["memo"]))})
        return {"samples_dir": self._rel(base) if base.resolve().is_relative_to(self.root) else str(base),
                "fictional": True, "count": len(demos), "demos": demos,
                "note": "All forms, memos, names and organisations were written from scratch during the hackathon and "
                        "are fictional (non-medical). The naive files reproduce a typical AI re-generation."}

    def tool_scan(self, args: dict[str, Any]) -> dict[str, Any]:
        from .scanner import scan

        form = self._path(args.get("form_path"), "form_path")
        return scan_summary(scan(form, self._spec(args)), rel=self._rel)

    def tool_discover(self, args: dict[str, Any]) -> dict[str, Any]:
        from .scanner import discover_blank_cells

        form = self._path(args.get("form_path"), "form_path")
        try:
            d = discover_blank_cells(form, mode=str(args.get("mode") or "demo"), client=self.client,
                                     model_tier=args.get("model_tier") or None,
                                     expected_sha256=args.get("expected_sha256") or None)
        except ValueError as exc:
            raise ToolError(f"refused before discovery: {exc}") from exc
        d["form_path"] = self._rel(form)
        return d

    def tool_fill(self, args: dict[str, Any]) -> dict[str, Any]:
        from .pipeline import new_run_id, run_pipeline, write_web_handoff
        from .scanner import OFFICE_REASON, build_confirmed_slot_spec, office_only_targets

        form = self._path(args.get("form_path"), "form_path")
        memo = self._memo(args, required=True)
        spec = self._spec(args)
        selection = "slot spec" if spec else "markers / sidecar .slots.json"
        if args.get("discovery") is None and not args.get("discovery_path") and args.get("confirmed_ids") is None:
            try:  # an explicit or sidecar slot spec may designate any cell: office-only ones are refused here
                office = office_only_targets(form, spec)
            except ValueError as exc:
                raise ToolError(f"refused before filling: {exc}") from exc
            if office:
                raise ToolError(f"refused before filling: the slot spec designates office-only cell(s) "
                                f"{'; '.join(office)} — {OFFICE_REASON}")
        if args.get("discovery") is not None or args.get("discovery_path") or args.get("confirmed_ids") is not None:
            if spec is not None:
                raise ToolError("send either slot_spec_path or discovery + confirmed_ids, not both")
            if isinstance(args.get("discovery"), dict):
                discovery = args["discovery"]
            elif args.get("discovery_path"):
                discovery = json.loads(self._path(args["discovery_path"], "discovery_path").read_text(encoding="utf-8"))
            else:
                raise ToolError("confirmed_ids needs the discovery report (discovery or discovery_path)")
            ids = args.get("confirmed_ids")
            if not isinstance(ids, list):
                raise ToolError("confirmed_ids must be a list of candidate ids confirmed by the user")
            try:
                spec = build_confirmed_slot_spec(discovery, [str(i) for i in ids], form)
            except ValueError as exc:
                raise ToolError(f"refused before filling: {exc}") from exc
            selection = (f"discovery cells {', '.join(spec['confirmed_candidates'])} passed by the MCP client — the "
                         f"human reviewer checks them at sign-off")
        auto_adapt = bool(args.get("auto_adapt", False))
        run_id = new_run_id()
        out_dir = self._run_dir(run_id)
        try:
            r = run_pipeline(form, memo, out_dir, mode=str(args.get("mode") or "demo"), client=self.client,
                             slot_spec=spec, run_id=run_id, model_tier=args.get("model_tier") or None,
                             auto_adapt=auto_adapt)
        except ValueError as exc:  # a changed blank form, an office-only target, … — refused before anything is written
            raise ToolError(f"refused before filling: {exc}") from exc
        if r.filled_path.resolve() == form.resolve():  # pragma: no cover - run_pipeline always writes a copy
            raise ToolError("refusing to overwrite the blank form")
        rep = r.report
        spec_path: Optional[str] = args.get("slot_spec_path") or None
        if spec is not None and not spec_path:  # keep the confirmed cells next to the draft (needed to verify it later)
            sp = out_dir / "confirmed.slots.json"
            sp.write_text(json.dumps(spec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
            spec_path = self._rel(sp)
        elif auto_adapt and getattr(r, "adapt_history", None) and not spec_path:
            asp = out_dir / f"{form.stem}.adapted.slots.json"
            if asp.is_file():
                spec_path = self._rel(asp)
            if any(a.get("action") == "auto_discover_unmarked" for a in r.adapt_history):
                selection = "auto-adapt blank-cell discovery — the human reviewer checks the cells at sign-off"
        review_url = REVIEW_URL.format(run_id=run_id)
        note = ("Draft written through MCP. It is not approved: a named human reviewer must confirm every HITL "
                f"checklist item before the file is used — start the web app (python -m form_guard.cli --serve) and "
                f"open {review_url} to review, edit and sign off this exact draft.")
        handoff = write_web_handoff(r, created_via="mcp:form_guard_fill", approval_status=APPROVAL_STATUS,
                                    review_url=review_url)
        status = {
            "approval_status": APPROVAL_STATUS, "run_id": run_id, "draft": r.filled_path.name,
            "filled_sha256": r.verified_sha256, "verification": rep.overall_status, "cell_selection": selection,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "note": note,
            "review_url": review_url, "web_handoff": handoff.name,
        }
        (out_dir / "APPROVAL_STATUS.json").write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n",
                                                      encoding="utf-8", newline="\n")
        audit_path = out_dir / "audit_log.json"  # the status travels with the audit log, never inside the document
        if audit_path.is_file():
            audit = json.loads(audit_path.read_text(encoding="utf-8"))
            audit.update(approval_status=APPROVAL_STATUS, approval=None, created_via="mcp:form_guard_fill",
                         human_sign_off_required=True)
            audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2, default=str), encoding="utf-8",
                                  newline="\n")
        usage = r.engine.get("usage") or {}
        return {
            "approval_status": APPROVAL_STATUS,
            "human_sign_off_required": True,
            "next_step": note,
            "run_id": run_id,
            "review_url": review_url,
            "web_handoff": self._rel(handoff),
            "out_dir": self._rel(out_dir),
            "draft_path": self._rel(r.filled_path),
            "report_html": self._rel(out_dir / "report.html"),
            "audit_log": self._rel(audit_path),
            "slot_spec_path": spec_path,
            "filled_sha256": r.verified_sha256,
            "cell_selection": selection,
            "engine": {k: r.engine.get(k) for k in ("mode", "label", "model", "model_tier", "fallback_reason")},
            "routing": usage.get("routing", []),
            "model_fallbacks": usage.get("model_fallbacks", []),
            "escalations": usage.get("escalations", []),
            "verification": {"overall_status": rep.overall_status, "passed": rep.passed_count,
                             "failed": rep.failed_count, "pending_slots": rep.pending_slots,
                             "ungrounded_claims": rep.ungrounded_claims_count, "checks": self._checks(rep)},
            "fields": [{"slot_id": m.slot_id, "status": m.status, "value": m.filled_value,
                        "suggested_value": m.suggested_value, "source_quote": m.source_quote,
                        "source_line_range": m.source_line_range, "normalizations": m.normalizations,
                        "routed_model": m.routed_model, "route_reason": m.route_reason}
                       for m in r.mappings],
            "hitl_checklist": [{"item_id": h.item_id, "kind": h.kind, "prompt": h.prompt, "value": h.value,
                                "source_quote": h.source_quote, "flags": h.flags, "required": h.required}
                               for h in rep.hitl_checklist],
        }

    def tool_verify(self, args: dict[str, Any]) -> dict[str, Any]:
        from .pipeline import _drift_check, build_previews, verify_filled_copy

        original = self._path(args.get("original_path"), "original_path")
        filled = self._path(args.get("filled_path"), "filled_path")
        memo = self._memo(args, required=False)
        out = verify_filled_copy(original, filled, memo, self._spec(args), None,
                                 memo_name=str(args.get("memo_path") or ""))
        rep = out.report
        try:  # VIS-01: layout drift or cell overflow outside the slots fails the copy
            visual = build_previews(out.scan, filled, out.values, None, None)[1]
            vis: dict[str, Any] = {"checked": True, **{k: visual.get(k) for k in (
                "identical_outside_slots", "drift_count", "overflow_count", "max_shift_px", "boxes_compared",
                "pages_blank", "pages_filled")}}
            if not visual.get("identical_outside_slots", True):
                rep.checks.append(_drift_check(visual))
                rep.failed_count += 1
                rep.overall_status = "FAIL"
        except Exception as exc:  # noqa: BLE001 - a file the layout engine cannot read fails VIS-01
            from .models import AuditCheckItem

            vis = {"checked": False, "error": f"{type(exc).__name__}: {exc}"}
            rep.checks.append(AuditCheckItem(
                check_id="VIS-01", category="visual", passed=False, severity="major",
                description="Rendered layout outside the designated fields is unchanged and every value fits its cell",
                details=f"visual verification failed: {type(exc).__name__}: {exc}",
                evidence={"error": f"{type(exc).__name__}: {exc}"},
            ))
            rep.failed_count += 1
            rep.overall_status = "FAIL"
        return {
            "original": self._rel(original), "filled": self._rel(filled), "overall_status": rep.overall_status,
            "passed": rep.passed_count, "failed": rep.failed_count, "pending_slots": rep.pending_slots,
            "grounding_verified": out.grounding_verified, "grounding_basis": out.basis, "audit_log": out.audit_note,
            "ungrounded_claims": rep.ungrounded_claims_count, "values_found": out.values, "checks": self._checks(rep),
            "visual": vis,
        }


def _result(mid: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _error(mid: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


# ------------------------------------------------------------------------ transport


def _force_utf8_stdio() -> None:
    """Diagnostics on stderr must never crash on a cp932 / cp1252 console (the protocol itself is raw UTF-8 bytes)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError, OSError):
            pass


def _protocol_stdout() -> BinaryIO:
    """A private handle on the real stdout for protocol messages. File descriptor 1 is pointed at stderr, so a stray
    print — from this package or any library, including C extensions — cannot corrupt the JSON-RPC stream."""
    sys.stdout.flush()
    proto = os.dup(1)
    os.dup2(2, 1)
    return os.fdopen(proto, "wb")


def _write(stdout, payload: Any, framed: bool) -> None:
    body = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), default=str).encode("utf-8")
    stdout.write((b"Content-Length: %d\r\n\r\n" % len(body)) + body if framed else body + b"\n")
    stdout.flush()


_CHUNK = 64 * 1024


def _skip_line(stdin) -> bool:
    """Discard the rest of an over-long line in bounded chunks; False at EOF."""
    while True:
        chunk = stdin.readline(_CHUNK)
        if not chunk:
            return False
        if chunk.endswith(b"\n"):
            return True


def _skip_bytes(stdin, n: int) -> bool:
    """Discard the body of a refused message in bounded chunks (never buffered); False at EOF."""
    while n > 0:
        chunk = stdin.read(min(n, _CHUNK))
        if not chunk:
            return False
        n -= len(chunk)
    return True


def serve(server: FormGuardMCP, stdin=None, stdout=None) -> None:
    """Read JSON-RPC messages until EOF: newline-delimited JSON, or Content-Length framed (LSP style).

    No message may exceed ``MAX_MCP_MESSAGE_BYTES``: a longer line, a larger / negative / malformed Content-Length and
    a body cut short by EOF are answered with a JSON-RPC parse error, and nothing is buffered beyond the limit."""
    stdin = stdin or sys.stdin.buffer
    stdout = stdout or _protocol_stdout()
    limit = MAX_MCP_MESSAGE_BYTES
    while True:
        line = stdin.readline(limit + 1)
        if not line:
            break
        if len(line) > limit and not line.endswith(b"\n"):  # a newline-delimited message larger than the limit
            _write(stdout, _error(None, PARSE_ERROR, f"Parse error: message larger than {limit:,} bytes"), False)
            if not _skip_line(stdin):
                break
            continue
        if not line.strip():
            continue
        framed = line.lstrip().lower().startswith(b"content-length:")
        if framed:
            raw = line.split(b":", 1)[1].strip()
            if re.fullmatch(rb"[+-]?\d{1,20}", raw) is None:
                _write(stdout, _error(None, PARSE_ERROR, "Parse error: bad Content-Length header"), True)
                continue
            length = int(raw)
            for _ in range(MAX_HEADER_LINES):  # further headers, up to the blank separator line
                header = stdin.readline(_CHUNK)
                if not header or not header.strip():
                    break
            if length < 0:  # never handed to read(): read(-1) would block until EOF
                _write(stdout, _error(None, INVALID_REQUEST, "Invalid Request: negative Content-Length"), True)
                continue
            if length > limit:
                _write(stdout, _error(None, PARSE_ERROR, f"Parse error: Content-Length {length:,} exceeds the "
                                                         f"{limit:,}-byte message limit"), True)
                if not _skip_bytes(stdin, length):
                    break
                continue
            body = stdin.read(length)
            if len(body) < length:  # the stream ended inside the message
                _write(stdout, _error(None, PARSE_ERROR, f"Parse error: EOF after {len(body):,} of {length:,} bytes"),
                       True)
                break
        else:
            body = line
        try:
            payload = json.loads(body.decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            _write(stdout, _error(None, PARSE_ERROR, "Parse error"), framed)
            continue
        response = server.handle_payload(payload)
        if response is not None:
            _write(stdout, response, framed)


def build_fastmcp(server: FormGuardMCP):
    """The same five tools on the official MCP Python SDK (optional dependency ``mcp``)."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise RuntimeError("the optional 'mcp' package is not installed — run the built-in server instead "
                           "(python -m form_guard.mcp_server)") from exc

    app = FastMCP("form-guard", instructions=INSTRUCTIONS)
    docs = {t["name"]: t["description"] for t in TOOLS}

    def run(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = server.call_tool(name, {k: v for k, v in arguments.items() if v is not None})
        if result.get("isError"):
            raise RuntimeError(result["content"][0]["text"])
        return result["structuredContent"]

    @app.tool(name="form_guard_list_demos", description=docs["form_guard_list_demos"])
    def form_guard_list_demos() -> dict:
        return run("form_guard_list_demos", {})

    @app.tool(name="form_guard_scan", description=docs["form_guard_scan"])
    def form_guard_scan(form_path: str, slot_spec_path: Optional[str] = None) -> dict:
        return run("form_guard_scan", {"form_path": form_path, "slot_spec_path": slot_spec_path})

    @app.tool(name="form_guard_discover_blank_cells", description=docs["form_guard_discover_blank_cells"])
    def form_guard_discover_blank_cells(form_path: str, mode: str = "demo", model_tier: str = "auto",
                                        expected_sha256: Optional[str] = None) -> dict:
        return run("form_guard_discover_blank_cells", {
            "form_path": form_path, "mode": mode, "model_tier": model_tier, "expected_sha256": expected_sha256})

    @app.tool(name="form_guard_fill", description=docs["form_guard_fill"])
    def form_guard_fill(form_path: str, memo_text: Optional[str] = None, memo_path: Optional[str] = None,
                        slot_spec_path: Optional[str] = None, discovery: Optional[dict] = None,
                        discovery_path: Optional[str] = None, confirmed_ids: Optional[list[str]] = None,
                        mode: str = "demo", model_tier: str = "auto") -> dict:
        return run("form_guard_fill", {
            "form_path": form_path, "memo_text": memo_text, "memo_path": memo_path, "slot_spec_path": slot_spec_path,
            "discovery": discovery, "discovery_path": discovery_path, "confirmed_ids": confirmed_ids, "mode": mode,
            "model_tier": model_tier})

    @app.tool(name="form_guard_verify", description=docs["form_guard_verify"])
    def form_guard_verify(original_path: str, filled_path: str, memo_text: Optional[str] = None,
                          memo_path: Optional[str] = None, slot_spec_path: Optional[str] = None) -> dict:
        return run("form_guard_verify", {"original_path": original_path, "filled_path": filled_path,
                                         "memo_text": memo_text, "memo_path": memo_path, "slot_spec_path": slot_spec_path})

    return app


def _venv_python(root: Path) -> Optional[Path]:
    for rel in ("Scripts/python.exe", "bin/python3", "bin/python"):
        p = root / ".venv" / rel
        if p.is_file():
            return p
    return None


def _relaunch_with_venv(argv: list[str]) -> Optional[int]:
    """Re-run this server with the project's .venv when this interpreter lacks the runtime dependencies."""
    missing = [m for m in RUNTIME_DEPENDENCIES if importlib.util.find_spec(m) is None]
    if not missing or os.environ.get("FORM_GUARD_MCP_CHILD"):
        return None
    project = Path(__file__).resolve().parent.parent
    venv = _venv_python(project)
    if venv is None or venv.resolve() == Path(sys.executable).resolve():
        print(f"form-guard MCP: missing {', '.join(missing)} — run pip install -r requirements.txt (tools will report "
              "the error)", file=sys.stderr)
        return None
    print(f"form-guard MCP: {', '.join(missing)} missing for {sys.executable}; re-launching with {venv}", file=sys.stderr)
    env = {**os.environ, "FORM_GUARD_MCP_CHILD": "1", "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
           "PYTHONPATH": os.pathsep.join(x for x in (str(project), os.environ.get("PYTHONPATH", "")) if x)}
    return subprocess.call([str(venv), "-m", "form_guard.mcp_server", *argv], env=env,
                           stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr)


def main(argv: Optional[list[str]] = None) -> int:
    _force_utf8_stdio()
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(prog="form-guard-mcp", description="Form-Guard MCP server (stdio)")
    ap.add_argument("--root", type=Path, default=None,
                    help="directory the tools may read and write (default: FORM_GUARD_MCP_ROOT or the working directory)")
    ap.add_argument("--fastmcp", action="store_true", help="serve through the official mcp SDK (pip install mcp)")
    a = ap.parse_args(argv)
    code = _relaunch_with_venv(argv)
    if code is not None:
        return code
    server = FormGuardMCP(root=a.root)
    print(f"form-guard MCP server {__version__}: stdio, {len(TOOLS)} tools, root {server.root}", file=sys.stderr)
    if a.fastmcp:
        build_fastmcp(server).run()
        return 0
    serve(server)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
