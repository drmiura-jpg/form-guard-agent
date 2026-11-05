"""FastAPI web app: demo runner, upload, visual diff, source inspector, structural audit, HITL approval gate.

The Human-in-the-Loop gate is enforced on the server: the filled file can only be
downloaded after a named reviewer has confirmed every required checklist item and
the verifier reports PASS. The approval is bound to the revision the reviewer saw and
to the SHA-256 of the exact bytes the verifier checked: ``POST /api/runs/{id}/approve``
and ``GET /api/runs/{id}/download`` both *require* ``filled_sha256`` (64 hex digits) and
``revision`` (422 if missing or malformed, 409 if stale). The download serves those
bytes from memory after re-checking the file. Every read and write of a run's state
(values, previews, approval, revision, SHA-256) happens under the run's re-entrant lock,
so a response is always one consistent snapshot.

MCP drafts: ``form_guard_fill`` leaves ``web_handoff.json`` next to its unapproved draft in
``output/mcp_runs/<run id>/``. ``POST /api/runs/import`` (``{"run_id": …}`` or
``{"run_dir": …}``) — or simply ``GET /api/runs/<run id>`` / ``/?run_id=<run id>``, which
import it on first use — loads that exact file without running any model, re-runs the
21 deterministic checks and VIS-01 against the blank form recorded in the handoff, and
refuses a draft whose SHA-256 no longer matches its audit log or that fails verification.

The server is meant for 127.0.0.1 only: requests with any other Host header (DNS
rebinding) and state-changing requests from another Origin (cross-site forms) are refused.

Unmarked blank forms: ``POST /api/discover`` (an uploaded form) or ``POST /api/discover/demo``
(the built-in form4 / form5 / form6, with form4 / form5 listed by ``GET /api/unmarked-demos``) returns candidate empty
cells (applicant vs office-only) and a ``discovery_id``. ``POST /api/runs/from-discovery``
(JSON) or ``POST /api/runs/upload`` (multipart) with that ``discovery_id`` and the reviewer's
``confirmed_targets`` fills only those cells — after re-checking that the blank form is
byte-identical to the one discovery looked at. The discovery stays on the server; the
client only names candidate ids.

``GET /api/demos`` lists all six fictional demos (``unmarked: true`` for form4 / form5 / form6).
``POST /api/run-demo`` runs one: a marked demo fills its designated fields; an unmarked demo
runs blank-cell discovery and fills the recommended applicant cells as an auto-preview — that
draft can never be approved; the reviewer confirms the cells (``confirmed_targets``) to get a
run that can be signed off. ``POST /api/discover`` accepts an upload or a ``demo_id``.

Uploads are limited to 15 MB and pass the archive gate (``ziputil.check_archive``): path
traversal in entry names, more than 2,000 entries, more than 100 MB uncompressed (ZIP bomb)
and DTD / entity declarations (XXE) are refused before anything parses the file.

``GET /api/claude-suite`` describes the repository's Claude Code skills, subagents, two-stage
hook guard and MCP server (read from the files), the model routing policy and the measured
workflow numbers.

Fixed PDF forms (Phase 4): デモ6 ``form6_study_group_grant.pdf`` runs blank-box discovery like
form4 / form5 (``POST /api/run-demo``), or its sidecar spec (``POST /api/runs``). Uploads accept
``.pdf`` behind the PDF gate (encryption, JavaScript / launch actions, embedded files, XFA,
compressed cross-references and decompression bombs are refused). ``GET /portal`` is a fictional
grant / study-group portal (架空学術振興ポータル) listing the blank forms with their printed rules and
SHA-256, a download link (``GET /api/portal/forms/{id}/download``) and a link that opens the form
in this app; ``GET /api/portal/forms`` is the same list as JSON for Claude Code / Claude in Chrome.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import tempfile
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Literal, Optional, Union
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from . import DEFAULT_MODEL, MODEL_HAIKU, MODEL_OPUS, MODEL_SONNET, __version__
from .claude_suite import PROJECT_ROOT, describe_suite
from .mapper import MODEL_TIERS, DemoEngine, live_credentials_present, load_dotenv
from .measure import measure, totals
from .models import FieldMapping
from .pipeline import HANDOFF_NAME, HandoffError, PipelineResult, load_handoff_draft, new_run_id, run_pipeline
from .samples import (
    DEMOS,
    PDF_FORMS,
    UNMARKED_FORMS,
    demo_by_id,
    demo_paths,
    ensure_samples,
    pdf_by_id,
    pdf_paths,
    read_memo,
    unmarked_by_id,
    unmarked_paths,
)
from .scanner import build_confirmed_slot_spec, detect_file_type, discover_blank_cells, recommended_ids
from .ziputil import MAX_UNCOMPRESSED_BYTES, ZipSurgeryError, check_archive

STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_UPLOAD_BYTES = 15 * 1024 * 1024  # 15 MB; the archive gate also caps the uncompressed size at 100 MB
MAX_MEMO_CHARS = 60_000
MAX_VALUE_CHARS = 1_000
_RUN_ID = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$")
_DISCOVERY_ID = re.compile(r"^[0-9a-f]{24}$")
SHA256_HEX = r"^[0-9a-fA-F]{64}$"
ModelTier = Literal["auto", "opus", "sonnet", "haiku"]
MEDIA = {".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
         ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
         ".pdf": "application/pdf"}
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]", "testserver"})
UNSAFE_METHODS = frozenset({"POST", "PUT", "DELETE", "PATCH"})


class RunRequest(BaseModel):
    demo_id: str
    mode: str = "auto"
    memo_text: Optional[str] = Field(default=None, max_length=MAX_MEMO_CHARS)
    model_tier: Optional[ModelTier] = None  # None: FORM_GUARD_MODEL_TIER or auto
    auto_adapt: bool = False


class OverrideRequest(BaseModel):
    values: dict[str, str]


class ApproveRequest(BaseModel):
    reviewer: str = Field(max_length=120)
    checked: list[str]
    # What the reviewer was looking at — both required: approval is refused if the run changed since (stale view).
    filled_sha256: str = Field(..., pattern=SHA256_HEX)
    revision: int = Field(..., ge=0, strict=True)


class ImportRequest(BaseModel):
    """POST /api/runs/import: an unapproved draft written by form_guard_fill (MCP), by run id or run folder."""

    model_config = ConfigDict(extra="forbid")
    run_id: Optional[str] = Field(default=None, max_length=32)
    run_dir: Optional[str] = Field(default=None, max_length=1024)


class RunDemoRequest(BaseModel):
    """POST /api/run-demo: any of the six demos. For form4 / form5 / form6 without ``confirmed_targets`` the server runs
    discovery and fills the recommended applicant cells as an auto-preview that can never be approved."""

    model_config = ConfigDict(extra="forbid")
    demo_id: str = Field(max_length=32)
    mode: str = "auto"
    memo_text: Optional[str] = Field(default=None, max_length=MAX_MEMO_CHARS)
    model_tier: Optional[ModelTier] = None
    discovery_id: Optional[str] = Field(default=None, max_length=64)
    confirmed_targets: Optional[list[Annotated[str, Field(max_length=32)]]] = Field(default=None, max_length=500)
    auto_adapt: bool = False


class DiscoverDemoRequest(BaseModel):
    demo_id: str = Field(max_length=32)
    mode: str = "auto"
    model_tier: Optional[ModelTier] = None


CandidateIds = Annotated[list[Annotated[str, Field(max_length=32)]], Field(max_length=500)]


class FromDiscoveryRequest(BaseModel):
    """Fill the cells a reviewer confirmed. The stored discovery — never anything the client sends — defines them,
    so extra fields (a client-side discovery, a form hash) are refused outright."""

    model_config = ConfigDict(extra="forbid")
    discovery_id: str = Field(max_length=64)
    confirmed_targets: Union[CandidateIds, Annotated[str, Field(max_length=4000)]]  # ids, or "recommended"
    memo_text: Optional[str] = Field(default=None, max_length=MAX_MEMO_CHARS)  # None: the demo's sample memo
    mode: str = "auto"
    model_tier: Optional[ModelTier] = None
    auto_adapt: bool = False


@dataclass
class RunState:
    result: PipelineResult
    ai_mappings: list[FieldMapping]
    form_path: Path
    memo_text: str
    naive_path: Optional[Path]
    spec: Optional[dict]
    display_name: str
    overrides: dict[str, str] = field(default_factory=dict)
    approval: Optional[dict[str, Any]] = None
    verified_bytes: bytes = b""     # the exact filled file the verifier checked (read once, right after the run)
    verified_sha256: str = ""       # SHA-256 of those bytes; approve() and download() refuse any other file
    revision: int = 0               # +1 on every reviewer edit; approval must name the revision it saw
    auto_preview: bool = False      # cells picked by the server (recommended), not confirmed by a reviewer
    imported_from: str = ""         # an MCP draft loaded for sign-off (web_handoff.json): its run folder
    # re-entrant: _payload() takes it too, also when called from overrides() / approve() that already hold it
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def stale(self, filled_sha256: str, revision: int) -> bool:
        """True if the client looked at another revision or another file than the verified one (both are required)."""
        return str(filled_sha256).lower() != str(self.verified_sha256).lower() or int(revision) != int(self.revision)

    def on_disk_sha256(self) -> str:
        try:
            return _sha(self.result.filled_path)
        except OSError:
            return ""


class RunStore:
    def __init__(self, root: Path, limit: int = 60, loader: Any = None):
        self.root = root
        self.limit = limit
        self.runs: "OrderedDict[str, RunState]" = OrderedDict()
        self.lock = threading.Lock()
        self.loader = loader  # run id -> RunState | None: imports an MCP draft (web_handoff.json) on first use
        self.load_lock = threading.Lock()

    def put(self, run_id: str, st: RunState) -> None:
        with self.lock:
            self.runs[run_id] = st
            self.runs.move_to_end(run_id)
            while len(self.runs) > self.limit:
                self.runs.popitem(last=False)

    def get(self, run_id: str) -> RunState:
        if not _RUN_ID.match(run_id or ""):
            raise HTTPException(404, "unknown run")
        with self.lock:
            st = self.runs.get(run_id)
        if st is None and self.loader is not None:
            with self.load_lock:  # one import at a time; a second request for the same id finds it stored
                with self.lock:
                    st = self.runs.get(run_id)
                if st is None:
                    st = self.loader(run_id)
                    if st is not None:
                        self.put(run_id, st)
        if st is None:
            raise HTTPException(404, "unknown run")
        return st


class DiscoveryStore:
    """Discovery reports of blank forms (uploaded or built-in), kept on the server so a client cannot edit them."""

    def __init__(self, limit: int = 60):
        self.limit = limit
        self.items: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
        self.lock = threading.Lock()

    def put(self, discovery: dict[str, Any], path: Path, **meta: Any) -> str:
        """meta: memo_text (built-in demos), demo_id, display_name."""
        token = secrets.token_hex(12)
        with self.lock:
            self.items[token] = {"discovery": discovery, "path": path, **meta}
            while len(self.items) > self.limit:
                self.items.popitem(last=False)
        return token

    def get(self, token: Optional[str]) -> dict[str, Any]:
        if not _DISCOVERY_ID.match(token or ""):
            raise HTTPException(404, "unknown discovery")
        with self.lock:
            item = self.items.get(token or "")
        if item is None:
            raise HTTPException(404, "unknown discovery — run /api/discover again")
        return item


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _verified_copy(result: PipelineResult) -> tuple[bytes, str]:
    """Read the filled file once and make sure it is the very file the verifier checked."""
    blob = result.filled_path.read_bytes()
    sha = hashlib.sha256(blob).hexdigest()
    if sha != (result.verified_sha256 or sha):
        raise HTTPException(409, "the filled file changed while it was being verified — run again")
    return blob, sha


def _parse_ids(raw: str) -> list[str]:
    """confirmed_targets as a JSON list (["c01", "c02"]) or a comma-separated string."""
    raw = (raw or "").strip()
    if raw.startswith("["):
        try:
            ids = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HTTPException(400, f"confirmed_targets is not valid JSON: {exc}") from exc
        if not isinstance(ids, list):
            raise HTTPException(400, "confirmed_targets must be a list of candidate ids")
        return [str(i) for i in ids]
    return [x.strip() for x in raw.split(",") if x.strip()]


def _confirmed_ids(discovery: dict[str, Any], targets: Union[list[str], str]) -> list[str]:
    """The reviewer's confirmed candidate ids: a list, a JSON list / comma-separated string, or "recommended"."""
    if isinstance(targets, str):
        if targets.strip().lower() == "recommended":
            return recommended_ids(discovery)
        return _parse_ids(targets)
    return [str(i) for i in targets]


def _require_memo(memo: Optional[str]) -> str:
    """Every run needs a source memo: a blank one would only produce a draft with every field awaiting input."""
    if not (memo or "").strip():
        raise HTTPException(400, "memo_text is required — paste the source memo")
    return memo or ""


def _hostname(host: str) -> str:
    host = host.strip().lower()
    if host.startswith("["):  # [::1]:8000
        return host[: host.find("]") + 1] if "]" in host else host
    if host.count(":") == 1:  # 127.0.0.1:8000
        return host.split(":", 1)[0]
    return host


def _origin_host(origin: str) -> str:
    """Hostname of an Origin header; "" for "null" or anything unparsable (fails closed)."""
    try:
        return (urlsplit(origin.strip()).hostname or "").lower()
    except ValueError:
        return ""


def allowed_hosts() -> set[str]:
    extra = os.environ.get("FORM_GUARD_ALLOWED_HOSTS", "")
    return set(LOCAL_HOSTS) | {h.strip().lower() for h in extra.split(",") if h.strip()}


def _safe_stem(name: str) -> str:
    stem = Path(name or "form").stem
    stem = re.sub(r"[^\w\-぀-ヿ一-鿿]+", "_", stem).strip("_")
    return (stem or "form")[:60]


def download_url(run_id: str, filled_sha256: str, revision: int) -> str:
    """The download link names the exact file and revision it is for; the server refuses any other."""
    return f"/api/runs/{run_id}/download?filled_sha256={quote(filled_sha256)}&revision={int(revision)}"


def _payload(run_id: str, st: RunState) -> dict[str, Any]:
    with st.lock:  # one snapshot: values, previews, approval, revision and SHA-256 belong to the same revision
        d = st.result.to_dict(include_previews=True)
        rep = st.result.report
        d["display_name"] = st.display_name
        d["approval"] = dict(st.approval) if st.approval else None
        d["overrides"] = dict(st.overrides)
        d["revision"] = st.revision
        d["filled"]["sha256"] = st.verified_sha256 or d["filled"]["sha256"]
        d["can_approve"] = rep.overall_status == "PASS"
        confirmed = st.spec.get("confirmed_candidates") if isinstance(st.spec, dict) else None
        d["confirmed_candidates"] = [str(c) for c in confirmed] if isinstance(confirmed, list) else []  # unmarked forms
        d["auto_preview"] = st.auto_preview
        if st.auto_preview:
            d["can_approve"] = False
            d["cell_selection"] = ("auto-preview: the recommended applicant cells were filled by the server — confirm "
                                   "the detected cells to get a run a reviewer can sign off")
        if st.imported_from:
            d["imported_from"] = st.imported_from
        d["links"] = {"report": f"/api/runs/{run_id}/report", "audit": f"/api/runs/{run_id}/audit",
                      "download": download_url(run_id, d["filled"]["sha256"], st.revision)}
        return d


def _check_package(path: Path) -> str:
    """The archive gate (path traversal, entry count, ZIP bomb, DTD / entities) then the OOXML type — or, for a PDF,
    the PDF gate (encryption, active content, compressed cross-references, decompression bombs)."""
    try:
        if path.read_bytes()[:5] != b"%PDF-":
            check_archive(path, max_uncompressed=MAX_UNCOMPRESSED_BYTES)
        return detect_file_type(path)
    except ZipSurgeryError as exc:
        raise HTTPException(400, str(exc) if "refused" in str(exc) else
                            "not a .docx / .xlsx file (ZIP container expected) or a .pdf form") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


def discoverable(demo_id: str) -> tuple[dict, bool]:
    """A demo filled through blank-cell discovery: (form4 / form5, False) or the fixed PDF (form6, True)."""
    try:
        return unmarked_by_id(demo_id), False
    except KeyError:
        return pdf_by_id(demo_id), True


def default_import_roots(project_root: Path) -> list[Path]:
    """Where MCP drafts can be imported from: output/mcp_runs/<run id> and output/<run id>, under the working
    directory, the MCP server root (FORM_GUARD_MCP_ROOT) and the project root."""
    bases = [Path.cwd(), Path(project_root)]
    if os.environ.get("FORM_GUARD_MCP_ROOT"):
        bases.insert(1, Path(os.environ["FORM_GUARD_MCP_ROOT"]))
    roots: list[Path] = []
    for base in bases:
        for sub in (Path("output") / "mcp_runs", Path("output")):
            p = (base / sub).resolve()
            if p not in roots:
                roots.append(p)
    return roots


def create_app(run_root: Optional[Path] = None, samples_dir: Optional[Path] = None, client: Any = None,
               project_root: Optional[Path] = None, import_roots: Optional[list[Path]] = None) -> FastAPI:
    load_dotenv()
    app = FastAPI(title="Form-Guard Agent", version=__version__, docs_url="/api/docs", redoc_url=None)
    store = RunStore(Path(run_root or os.environ.get("FORM_GUARD_RUN_DIR") or Path.cwd() / "output" / "web_runs"))
    app.state.store = store
    app.state.client = client
    app.state.samples_dir = samples_dir
    app.state.project_root = Path(project_root or PROJECT_ROOT)  # where .claude/ and .mcp.json live
    app.state.import_roots = [Path(p).resolve() for p in (import_roots if import_roots is not None
                                                          else default_import_roots(app.state.project_root))]
    app.state.workflow = None  # measured once per process (GET /api/claude-suite)
    workflow_lock = threading.Lock()

    @app.middleware("http")
    async def security_headers(request, call_next):
        hosts = allowed_hosts()
        if _hostname(request.headers.get("host", "")) not in hosts:  # DNS rebinding
            resp = JSONResponse({"detail": "forbidden host"}, status_code=403)
        elif request.method in UNSAFE_METHODS and "origin" in request.headers and \
                _origin_host(request.headers["origin"]) not in hosts:
            resp = JSONResponse({"detail": "forbidden origin"}, status_code=403)  # cross-site form / fetch
        else:
            resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            resp.headers["Cache-Control"] = "no-cache"  # always revalidate the UI (ETag) so updates show up
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        resp.headers.setdefault("Content-Security-Policy",
                                "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                                "script-src 'self'; connect-src 'self'; frame-ancestors 'none'")
        return resp

    discoveries = DiscoveryStore()
    app.state.discoveries = discoveries

    def samples() -> Path:
        return ensure_samples(app.state.samples_dir)

    def _check_tier(model_tier: Optional[str]) -> Optional[str]:
        if not model_tier:
            return None  # FORM_GUARD_MODEL_TIER or auto
        tier = model_tier.lower()
        if tier not in MODEL_TIERS:
            raise HTTPException(400, f"model_tier must be one of: {', '.join(MODEL_TIERS)}")
        return tier

    def _stage_upload(file: UploadFile) -> Path:
        """Store an uploaded .docx / .xlsx / .pdf in a fresh staging folder after size, ZIP-bomb and type checks."""
        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in MEDIA:
            raise HTTPException(400, "upload a .docx, .xlsx or .pdf form")
        data = file.file.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "file larger than 15 MB")
        staging = store.root / "_uploads" / new_run_id()
        staging.mkdir(parents=True, exist_ok=True)
        form_path = staging / f"{_safe_stem(file.filename or 'form')}{suffix}"
        form_path.write_bytes(data)
        ftype = _check_package(form_path)
        if f".{ftype}" != suffix:
            raise HTTPException(400, f"file content is {ftype}, but the extension is {suffix}")
        return form_path

    def _run(form_path: Path, memo: str, mode: str, *, naive: Optional[Path], spec: Optional[dict],
             demo_id: Optional[str], display: str, model_tier: Optional[str] = None,
             auto_preview: bool = False, auto_adapt: bool = False) -> tuple[str, RunState]:
        run_id = new_run_id()
        run_dir = store.root / run_id
        try:
            result = run_pipeline(form_path, memo, run_dir, mode=mode, naive_path=naive, slot_spec=spec,
                                  demo_id=demo_id, run_id=run_id, client=app.state.client, model_tier=model_tier,
                                  auto_adapt=auto_adapt)
        except Exception as exc:  # noqa: BLE001 - surface any pipeline error to the UI
            raise HTTPException(422, f"pipeline failed: {exc}") from exc
        blob, sha = _verified_copy(result)  # recorded once, at pipeline completion
        eff_spec = result.scan.spec if isinstance(result.scan.spec, dict) and (result.scan.spec.get("targets") or result.scan.spec.get("slots")) else spec
        st = RunState(result=result, ai_mappings=[m.model_copy(deep=True) for m in result.mappings],
                      form_path=form_path, memo_text=memo, naive_path=naive, spec=eff_spec, display_name=display,
                      verified_bytes=blob, verified_sha256=sha, auto_preview=auto_preview)
        store.put(run_id, st)
        return run_id, st

    # ------------------------------------------------ MCP drafts: web_handoff.json -> human sign-off

    def _shown(p: Path) -> str:
        for base in (app.state.project_root, Path.cwd()):
            try:
                return p.resolve().relative_to(Path(base).resolve()).as_posix()
            except ValueError:
                continue
        return p.name

    def _handoff_dir(run_id: str) -> Optional[Path]:
        """output/mcp_runs/<run id> or output/<run id> (under an import root) holding web_handoff.json."""
        for root in app.state.import_roots:
            d = root / run_id
            if (d / HANDOFF_NAME).is_file():
                return d
        return None

    def _import_dir(run_dir: Path) -> RunState:
        """Load the exact draft bytes for sign-off: no model runs; the copy is re-verified (21 checks + VIS-01)."""
        try:
            result, handoff = load_handoff_draft(run_dir, store.root / run_dir.name)
        except HandoffError as exc:
            raise HTTPException(exc.status, str(exc)) from exc
        blob, sha = _verified_copy(result)
        form = handoff.get("form") or {}
        return RunState(result=result, ai_mappings=[m.model_copy(deep=True) for m in result.mappings],
                        form_path=result.form_path, memo_text=result.memo_text, naive_path=None,
                        spec=result.scan.spec if isinstance(result.scan.spec, dict) else {},
                        display_name=str(form.get("display_name") or form.get("name") or result.form_path.name),
                        verified_bytes=blob, verified_sha256=sha, revision=0, imported_from=_shown(run_dir))

    def _load_run(run_id: str) -> Optional[RunState]:
        run_dir = _handoff_dir(run_id)
        return _import_dir(run_dir) if run_dir is not None else None

    store.loader = _load_run  # GET /api/runs/<run id> (and /?run_id=<run id> in the UI) imports a draft on first use

    def _resolve_run_dir(raw: str) -> Path:
        p = Path(raw)
        for cand in ([p] if p.is_absolute() else [Path.cwd() / p, app.state.project_root / p]):
            try:
                cand = cand.resolve()
            except OSError:
                continue
            if cand.parent in app.state.import_roots and _RUN_ID.match(cand.name):
                return cand
        raise HTTPException(400, "run_dir must be output/mcp_runs/<run id> or output/<run id> (a draft written by "
                                 "form_guard_fill)")

    @app.get("/", response_class=HTMLResponse)
    def index():
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html; charset=utf-8")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/api/health")
    def health():
        live = live_credentials_present() or app.state.client is not None
        return {"status": "ok", "version": __version__, "live_available": live,
                "default_mode": "live" if live else "demo", "default_model": os.environ.get("FORM_GUARD_MODEL") or DEFAULT_MODEL,
                "model_tiers": list(MODEL_TIERS),
                "default_model_tier": os.environ.get("FORM_GUARD_MODEL_TIER") or "auto",
                "models": {"opus": MODEL_OPUS, "sonnet": MODEL_SONNET, "haiku": MODEL_HAIKU}}

    @app.get("/api/demos")
    def demos():
        """All six fictional demos: デモ1–3 with designated fields, デモ4–5 unmarked (blank-cell discovery), デモ6 a fixed
        PDF (blank-box discovery; its sidecar spec also designates the boxes)."""
        base = samples()
        out = []
        pdf_ids = {x["id"] for x in PDF_FORMS}
        for n, d in enumerate([*DEMOS, *UNMARKED_FORMS, *PDF_FORMS], start=1):
            pdf = d["id"] in pdf_ids
            unmarked = pdf or d["id"] in {u["id"] for u in UNMARKED_FORMS}
            p = pdf_paths(d, base) if pdf else unmarked_paths(d, base) if unmarked else demo_paths(d, base)
            out.append({**{k: d[k] for k in ("id", "title", "title_en", "org", "file_type", "highlights")},
                        "number": n, "label": f"デモ{n}", "unmarked": unmarked, "marked": not unmarked, "pdf": pdf,
                        "badge": "固定PDF・空欄自動検出" if pdf else "空欄自動検出" if unmarked else "",
                        "has_naive": not unmarked or pdf,
                        "form": p["form"].name, "memo_name": p["memo"].name, "memo_text": read_memo(p["memo"])})
        return out

    @app.post("/api/runs")
    def run_demo(req: RunRequest):
        """A demo with designated fields (form1–3), or the fixed PDF form6 through its sidecar .slots.json."""
        try:
            d = demo_by_id(req.demo_id)
            p = demo_paths(d, samples())
        except KeyError:
            try:
                d = pdf_by_id(req.demo_id)
            except KeyError as exc:
                raise HTTPException(404, "unknown demo") from exc
            p = pdf_paths(d, samples())
        memo = _require_memo(req.memo_text if req.memo_text is not None else read_memo(p["memo"]))
        run_id, st = _run(p["form"], memo, req.mode, naive=p["naive"], spec=None, demo_id=d["id"], display=p["form"].name,
                          model_tier=req.model_tier, auto_adapt=req.auto_adapt)
        return _payload(run_id, st)

    def _discover(form_path: Path, mode: str, tier: Optional[str]) -> dict[str, Any]:
        try:
            return discover_blank_cells(form_path, mode=mode, client=app.state.client, model_tier=tier)
        except Exception as exc:  # noqa: BLE001 - surface any discovery error to the UI
            raise HTTPException(422, f"discovery failed: {exc}") from exc

    @app.get("/api/unmarked-demos")
    def unmarked_demos():
        base = samples()
        out = []
        for u in UNMARKED_FORMS:
            p = unmarked_paths(u, base)
            out.append({**{k: u[k] for k in ("id", "title", "title_en", "org", "file_type", "highlights")},
                        "form": p["form"].name, "memo_name": p["memo"].name, "memo_text": read_memo(p["memo"])})
        return out

    @app.post("/api/discover")
    def discover(file: Optional[UploadFile] = File(None), mode: str = Form("auto"), model_tier: Optional[str] = Form(None),
                 demo_id: Optional[str] = Form(None)):
        """Stages 1-3 of blank-cell discovery for an unmarked form (an upload, or demo_id form4 / form5 / form6); nothing is
        filled until a reviewer confirms."""
        tier = _check_tier(model_tier)
        if file is None or not file.filename:
            if not demo_id:
                raise HTTPException(400, "upload a .docx / .xlsx / .pdf form or name a demo_id (form4 / form5 / form6)")
            return discover_demo(DiscoverDemoRequest(demo_id=demo_id, mode=mode, model_tier=tier))
        form_path = _stage_upload(file)
        d = _discover(form_path, mode, tier)
        d["form"] = file.filename or form_path.name
        return {**d, "discovery_id": discoveries.put(d, form_path, display_name=d["form"])}

    @app.post("/api/discover/demo")
    def discover_demo(req: DiscoverDemoRequest):
        """Stages 1-3 on a built-in unmarked form (form4 .docx / form5 .xlsx / form6 fixed .pdf), with its memo."""
        try:
            u, pdf = discoverable(req.demo_id)
        except KeyError as exc:
            raise HTTPException(404, "unknown unmarked demo") from exc
        p = pdf_paths(u, samples()) if pdf else unmarked_paths(u, samples())
        d = _discover(p["form"], req.mode, req.model_tier)
        memo = read_memo(p["memo"])
        token = discoveries.put(d, p["form"], memo_text=memo, demo_id=u["id"], display_name=p["form"].name)
        return {**d, "discovery_id": token, "demo_id": u["id"], "title": u["title"], "title_en": u["title_en"],
                "org": u["org"], "memo_text": memo}

    @app.post("/api/runs/from-discovery")
    def run_from_discovery(req: FromDiscoveryRequest):
        """Stage 4: fill only the confirmed cells of a stored discovery (the blank form is re-checked by SHA-256)."""
        entry = discoveries.get(req.discovery_id)
        memo = _require_memo(req.memo_text if req.memo_text is not None else entry.get("memo_text"))
        ids = _confirmed_ids(entry["discovery"], req.confirmed_targets)
        try:  # refuses a changed form, 0 confirmed cells, unknown ids and office-only cells
            spec = build_confirmed_slot_spec(entry["discovery"], ids, entry["path"])
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        run_id, st = _run(entry["path"], memo, req.mode, naive=None, spec=spec, demo_id=entry.get("demo_id"),
                          display=entry.get("display_name") or entry["path"].name, model_tier=req.model_tier,
                          auto_adapt=req.auto_adapt)
        return _payload(run_id, st)

    @app.post("/api/run-demo")
    def run_any_demo(req: RunDemoRequest):
        """One click for any of the six demos. Marked (form1–3): fill the designated fields. Unmarked (form4 / form5 / form6):
        discovery, then either the reviewer's ``confirmed_targets`` or — without them — an auto-preview of the
        recommended applicant cells that can never be approved."""
        tier = _check_tier(req.model_tier)
        try:
            d = demo_by_id(req.demo_id)
        except KeyError:
            d = None
        if d is not None:
            if req.discovery_id or req.confirmed_targets is not None:
                raise HTTPException(400, "a marked demo has designated fields — discovery_id / confirmed_targets do not apply")
            p = demo_paths(d, samples())
            memo = _require_memo(req.memo_text if req.memo_text is not None else read_memo(p["memo"]))
            run_id, st = _run(p["form"], memo, req.mode, naive=p["naive"], spec=None, demo_id=d["id"],
                              display=p["form"].name, model_tier=tier, auto_adapt=req.auto_adapt)
            return {"demo_id": d["id"], "unmarked": False, "auto_preview": False, "run": _payload(run_id, st)}
        try:
            u, pdf = discoverable(req.demo_id)
        except KeyError as exc:
            raise HTTPException(404, "unknown demo") from exc
        p = pdf_paths(u, samples()) if pdf else unmarked_paths(u, samples())
        sample_memo = read_memo(p["memo"])
        memo = _require_memo(req.memo_text if req.memo_text is not None else sample_memo)
        if req.discovery_id:  # the stored discovery — never anything the client sends — defines the cells
            entry = discoveries.get(req.discovery_id)
            if entry.get("demo_id") != u["id"]:
                raise HTTPException(400, "this discovery_id belongs to another form")
            report, token = entry["discovery"], req.discovery_id
        else:
            report = _discover(p["form"], req.mode, tier)
            token = discoveries.put(report, p["form"], memo_text=sample_memo, demo_id=u["id"], display_name=p["form"].name)
        preview = req.confirmed_targets is None
        ids = recommended_ids(report) if preview else [str(i) for i in req.confirmed_targets or []]
        try:  # refuses a changed form, 0 confirmed cells, unknown ids and office-only cells
            spec = build_confirmed_slot_spec(report, ids, p["form"])
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        run_id, st = _run(p["form"], memo, req.mode, naive=p.get("naive"), spec=spec, demo_id=u["id"],
                          display=p["form"].name, model_tier=tier, auto_preview=preview, auto_adapt=req.auto_adapt)
        discovery = {**report, "discovery_id": token, "demo_id": u["id"], "title": u["title"], "title_en": u["title_en"],
                     "org": u["org"], "memo_text": sample_memo}
        return {"demo_id": u["id"], "unmarked": True, "auto_preview": preview, "discovery": discovery,
                "run": _payload(run_id, st)}

    @app.get("/api/claude-suite")
    def claude_suite():
        """Skills, subagents, hook and MCP server read from the repository, the routing policy and measured numbers."""
        suite = describe_suite(app.state.project_root)
        with workflow_lock:  # five offline pipeline runs, measured once per process
            if app.state.workflow is None:
                with tempfile.TemporaryDirectory() as tmp:
                    rows = measure(samples(), Path(tmp))
                app.state.workflow = {
                    "rows": rows, "totals": totals(rows), "engine": DemoEngine.label,
                    "measured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "note": "measured on this machine with the offline engine (no network); the manual columns are "
                            "counted from the files, not a timed study of a person"}
        return {**suite, "workflow": app.state.workflow}

    # ------------------------------------------------ 架空学術振興ポータル (fictional grant / study-group portal)
    portal_lock = threading.Lock()
    app.state.portal = None
    app.state.portal_sig = None

    def portal_forms() -> list[dict[str, Any]]:
        from .portal import build_portal

        base = samples()
        bf = base / "blank_forms"
        sig = tuple(sorted((p.name, p.stat().st_mtime_ns, p.stat().st_size,
                            hashlib.sha256(p.read_bytes()).hexdigest())
                           for p in bf.iterdir() if p.is_file())) if bf.is_dir() else ()
        with portal_lock:
            if app.state.portal is None or app.state.portal_sig != sig:
                app.state.portal = build_portal(base, app.state.project_root)
                app.state.portal_sig = sig
            return app.state.portal

    @app.get("/portal", response_class=HTMLResponse)
    def portal():
        """The fictional portal: calls, blank forms, printed rules, SHA-256, download and open-in-Form-Guard links."""
        from .portal import portal_html

        return HTMLResponse(portal_html(portal_forms()), headers={"Cache-Control": "no-cache"})

    @app.get("/api/portal/forms")
    def portal_forms_api():
        """The portal's forms as JSON (for Claude Code and Claude in Chrome)."""
        from .portal import PORTAL_TITLE_EN, PORTAL_TITLE_JA

        return {"portal": PORTAL_TITLE_JA, "portal_en": PORTAL_TITLE_EN, "fictional": True,
                "forms": portal_forms(),
                "workflow": ["download the blank form (download_url) or use mcp_form_path",
                             "form_guard_discover_blank_cells (or form_guard_scan for marked forms) — the user confirms",
                             "form_guard_fill with the memo — an unapproved draft and its review_url",
                             "form_guard_verify — the 21 checks + VIS-01",
                             "open review_url in Chrome — a named human reviewer approves"]}

    @app.get("/api/portal/forms/{form_id}/download")
    def portal_download(form_id: str):
        """The blank form exactly as published (its SHA-256 in X-Form-Guard-SHA256)."""
        entry = next((f for f in portal_forms() if f["id"] == form_id), None)
        if entry is None:
            raise HTTPException(404, "unknown form")
        base = samples()
        path = base / "blank_forms" / entry["form"]
        blob = path.read_bytes()
        sha = hashlib.sha256(blob).hexdigest()
        suffix = path.suffix.lower()
        headers = {"Content-Disposition": f"attachment; filename=\"blank{suffix}\"; filename*=UTF-8''{quote(path.name)}",
                   "X-Form-Guard-SHA256": sha}
        return Response(content=blob, media_type=MEDIA.get(suffix, "application/octet-stream"), headers=headers)

    @app.post("/api/runs/upload")
    def run_upload(file: UploadFile = File(...), memo_text: str = Form(...), mode: str = Form("auto"),
                   slot_spec: Optional[UploadFile] = File(None), model_tier: Optional[str] = Form(None),
                   discovery_id: Optional[str] = Form(None), confirmed_targets: Optional[str] = Form(None),
                   auto_adapt: bool = Form(False)):
        tier = _check_tier(model_tier)
        if len(memo_text) > MAX_MEMO_CHARS:
            raise HTTPException(413, "memo too long")
        _require_memo(memo_text)
        form_path = _stage_upload(file)
        spec = None
        if slot_spec is not None and slot_spec.filename:
            raw = slot_spec.file.read(200_001)
            if len(raw) > 200_000:
                raise HTTPException(413, "slot spec too large")
            try:
                spec = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise HTTPException(400, f"slot spec is not valid JSON: {exc}") from exc
            if not isinstance(spec, dict):  # a JSON string would otherwise be read as a file path on the server
                raise HTTPException(400, "slot spec must be a JSON object")
        if confirmed_targets is not None or discovery_id:
            if spec is not None:
                raise HTTPException(400, "send either a slot spec or confirmed discovery targets, not both")
            if confirmed_targets is None:
                raise HTTPException(400, "confirmed_targets is required with discovery_id")
            entry = discoveries.get(discovery_id)
            ids = _confirmed_ids(entry["discovery"], confirmed_targets)
            try:  # the stored discovery, not anything the client sends, defines the cells
                spec = build_confirmed_slot_spec(entry["discovery"], ids, form_path)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
        run_id, st = _run(form_path, memo_text, mode, naive=None, spec=spec, demo_id=None,
                          display=file.filename or form_path.name, model_tier=tier, auto_adapt=auto_adapt)
        return _payload(run_id, st)

    @app.post("/api/runs/import")
    def import_run(req: ImportRequest):
        """Load an unapproved MCP draft (web_handoff.json) for human sign-off: the exact file, re-verified."""
        if bool(req.run_id) == bool(req.run_dir):
            raise HTTPException(400, "send either run_id or run_dir")
        if req.run_id:
            if not _RUN_ID.match(req.run_id):
                raise HTTPException(400, "run_id must look like 20261103-101500-a1b2c3")
            run_id, run_dir = req.run_id, _handoff_dir(req.run_id)
        else:
            run_dir = _resolve_run_dir(req.run_dir or "")
            run_id = run_dir.name
        with store.load_lock:
            with store.lock:
                st = store.runs.get(run_id)
            if st is None:
                if run_dir is None or not (run_dir / HANDOFF_NAME).is_file():
                    raise HTTPException(404, f"no draft with {HANDOFF_NAME} for run {run_id}")
                st = _import_dir(run_dir)
                store.put(run_id, st)
        return _payload(run_id, st)

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str):
        st = store.get(run_id)  # an MCP draft is imported (and re-verified) on first use
        with st.lock:
            return _payload(run_id, st)

    @app.post("/api/runs/{run_id}/overrides")
    def overrides(run_id: str, req: OverrideRequest):
        st = store.get(run_id)
        with st.lock:
            slots = {s.slot_id: s for s in st.result.scan.slots}
            clean: dict[str, str] = {}
            for k, v in req.values.items():
                if k not in slots:
                    continue
                v = v.strip()
                limit = min(MAX_VALUE_CHARS, slots[k].max_chars) if slots[k].max_chars else MAX_VALUE_CHARS
                if len(v) > limit:  # never cut the reviewer's text silently; nothing is changed
                    raise HTTPException(422, f"{k} exceeds maximum length ({len(v)} > {limit} chars)")
                clean[k] = v
            merged = {**st.overrides, **clean}
            merged = {k: v for k, v in merged.items() if v}
            result = run_pipeline(st.form_path, st.memo_text, store.root / run_id, naive_path=st.naive_path,
                                  slot_spec=st.spec, demo_id=st.result.demo_id, run_id=run_id,
                                  prior=(st.ai_mappings, st.result.engine), overrides=merged)
            blob, sha = _verified_copy(result)
            st.result, st.overrides, st.approval = result, merged, None
            st.verified_bytes, st.verified_sha256 = blob, sha
            st.revision += 1
            return _payload(run_id, st)

    @app.post("/api/runs/{run_id}/approve")
    def approve(run_id: str, req: ApproveRequest):
        st = store.get(run_id)
        with st.lock:
            if st.stale(req.filled_sha256, req.revision):
                raise HTTPException(409, {"message": "stale view or file modified — refresh before approving",
                                          "revision": st.revision, "filled_sha256": st.verified_sha256})
            if not st.verified_sha256 or st.on_disk_sha256() != st.verified_sha256:
                raise HTTPException(409, {"message": "the filled file was modified after verification — run it again"})
            if st.auto_preview:  # the cells were the server's recommendation, not the reviewer's decision
                raise HTTPException(409, {"message": "auto-preview — confirm the detected cells first "
                                                     "(検出した空欄を承認して外科的差込を実行), then sign off that run"})
            rep = st.result.report
            if rep.pending_slots:
                raise HTTPException(409, {"message": "fields still awaiting human input", "pending": rep.pending_slots})
            if rep.overall_status != "PASS":
                raise HTTPException(409, {"message": "verification did not pass — download stays blocked"})
            required = [h.item_id for h in rep.hitl_checklist if h.required]
            missing = [i for i in required if i not in set(req.checked)]
            if missing:
                raise HTTPException(400, {"message": "every checklist item must be confirmed", "missing": missing})
            reviewer = req.reviewer.strip()
            if not reviewer:
                raise HTTPException(400, {"message": "reviewer name is required"})
            st.approval = {
                "reviewer": reviewer,
                "approved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "checked": required,
                "filled_sha256": st.verified_sha256,
                "revision": st.revision,
                "report_status": rep.overall_status,
                "engine": st.result.engine.get("label"),
            }
            if st.imported_from:
                st.approval["source_draft"] = st.imported_from  # the MCP draft folder this exact file came from
            (st.result.out_dir or store.root / run_id).joinpath("approval.json").write_text(
                json.dumps(st.approval, ensure_ascii=False, indent=2), encoding="utf-8")
            return {"approved": True, "download_url": download_url(run_id, st.verified_sha256, st.revision),
                    **st.approval}

    @app.get("/api/runs/{run_id}/download")
    def download(run_id: str, filled_sha256: Annotated[str, Query(pattern=SHA256_HEX)],
                 revision: Annotated[int, Query(ge=0)]):
        """Both query parameters are required (422 without them): the link names the exact approved file."""
        st = store.get(run_id)
        with st.lock:
            if not st.approval:
                raise HTTPException(403, "human-in-the-loop approval is required before download")
            if st.stale(filled_sha256, revision) or st.approval.get("revision") != st.revision or \
                    str(st.approval.get("filled_sha256", "")).lower() != filled_sha256.lower():
                raise HTTPException(409, {"message": "stale view — the approved revision / file is not the current one",
                                          "revision": st.revision, "filled_sha256": st.verified_sha256})
            path = st.result.filled_path
            try:
                blob = path.read_bytes()
            except OSError as exc:
                raise HTTPException(409, "the approved file is no longer available — run again") from exc
            if not (hashlib.sha256(blob).hexdigest() == st.approval["filled_sha256"] == st.verified_sha256):
                raise HTTPException(409, "file changed after approval — approve again")
            suffix = path.suffix.lower()
            name = f"filled_{_safe_stem(st.display_name)}{suffix}"
            headers = {"Content-Disposition": f"attachment; filename=\"filled{suffix}\"; filename*=UTF-8''{quote(name)}",
                       "X-Form-Guard-SHA256": st.approval["filled_sha256"]}
            # serve the bytes that were just hashed — nothing is re-read after the check
            return Response(content=blob, media_type=MEDIA.get(suffix, "application/octet-stream"), headers=headers)

    @app.get("/api/runs/{run_id}/report", response_class=HTMLResponse)
    def report(run_id: str):
        st = store.get(run_id)
        with st.lock:  # an edit rewrites report.html: never serve a half-written file or one of another revision
            p = (st.result.out_dir or store.root / run_id) / "report.html"
            page = p.read_text(encoding="utf-8")
        return HTMLResponse(page)

    @app.get("/api/runs/{run_id}/audit")
    def audit(run_id: str):
        st = store.get(run_id)
        with st.lock:
            p = (st.result.out_dir or store.root / run_id) / "audit_log.json"
            data = json.loads(p.read_text(encoding="utf-8"))
            data["approval"] = st.approval
            data["revision"] = st.revision
        return JSONResponse(data)

    return app


app = create_app()
