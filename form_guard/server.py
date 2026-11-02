"""FastAPI web app: demo runner, upload, visual diff, source inspector, structural audit, HITL approval gate.

The Human-in-the-Loop gate is enforced on the server: the filled file can only be
downloaded after a named reviewer has confirmed every required checklist item and
the verifier reports PASS. The approval is bound to the revision the reviewer saw and
to the SHA-256 of the exact bytes the verifier checked; the download serves those
bytes from memory after re-checking the file, under a per-run lock.

The server is meant for 127.0.0.1 only: requests with any other Host header (DNS
rebinding) and state-changing requests from another Origin (cross-site forms) are refused.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import zipfile
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import DEFAULT_MODEL, __version__
from .mapper import live_credentials_present, load_dotenv
from .models import FieldMapping
from .pipeline import PipelineResult, new_run_id, run_pipeline
from .samples import DEMOS, demo_by_id, demo_paths, ensure_samples, read_memo
from .scanner import detect_file_type

STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 80 * 1024 * 1024
MAX_MEMO_CHARS = 60_000
MAX_VALUE_CHARS = 1_000
_RUN_ID = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$")
MEDIA = {".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
         ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]", "testserver"})
UNSAFE_METHODS = frozenset({"POST", "PUT", "DELETE", "PATCH"})


class RunRequest(BaseModel):
    demo_id: str
    mode: str = "auto"
    memo_text: Optional[str] = Field(default=None, max_length=MAX_MEMO_CHARS)


class OverrideRequest(BaseModel):
    values: dict[str, str]


class ApproveRequest(BaseModel):
    reviewer: str = Field(max_length=120)
    checked: list[str]
    # What the reviewer was looking at: approval is refused if the run changed since (stale view).
    filled_sha256: Optional[str] = Field(default=None, max_length=64)
    revision: Optional[int] = None


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
    verified_sha256: str = ""
    revision: int = 0               # +1 on every reviewer edit; approval must name the revision it saw
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


class RunStore:
    def __init__(self, root: Path, limit: int = 60):
        self.root = root
        self.limit = limit
        self.runs: "OrderedDict[str, RunState]" = OrderedDict()
        self.lock = threading.Lock()

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
        if st is None:
            raise HTTPException(404, "unknown run")
        return st


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _verified_copy(result: PipelineResult) -> tuple[bytes, str]:
    """Read the filled file once and make sure it is the very file the verifier checked."""
    blob = result.filled_path.read_bytes()
    sha = hashlib.sha256(blob).hexdigest()
    if sha != result.report.stats.get("filled_sha256", sha):
        raise HTTPException(409, "the filled file changed while it was being verified — run again")
    return blob, sha


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


def _payload(run_id: str, st: RunState) -> dict[str, Any]:
    d = st.result.to_dict(include_previews=True)
    rep = st.result.report
    d["display_name"] = st.display_name
    d["approval"] = st.approval
    d["overrides"] = st.overrides
    d["revision"] = st.revision
    d["filled"]["sha256"] = st.verified_sha256 or d["filled"]["sha256"]
    d["can_approve"] = rep.overall_status == "PASS"
    d["links"] = {"report": f"/api/runs/{run_id}/report", "audit": f"/api/runs/{run_id}/audit",
                  "download": f"/api/runs/{run_id}/download"}
    return d


def _check_package(path: Path) -> str:
    if not zipfile.is_zipfile(path):
        raise HTTPException(400, "not a .docx / .xlsx file (ZIP container expected)")
    with zipfile.ZipFile(path) as zf:
        infos = zf.infolist()
        if len(infos) > 3000:
            raise HTTPException(400, "too many entries in the package")
        total = sum(i.file_size for i in infos)
        if total > MAX_UNCOMPRESSED_BYTES:
            raise HTTPException(400, "package expands to more than 80 MB — refused")
        for i in infos:
            if i.compress_size and i.file_size / max(1, i.compress_size) > 200:
                raise HTTPException(400, "suspicious compression ratio — refused")
    try:
        return detect_file_type(path)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


def create_app(run_root: Optional[Path] = None, samples_dir: Optional[Path] = None, client: Any = None) -> FastAPI:
    load_dotenv()
    app = FastAPI(title="Form-Guard Agent", version=__version__, docs_url="/api/docs", redoc_url=None)
    store = RunStore(Path(run_root or os.environ.get("FORM_GUARD_RUN_DIR") or Path.cwd() / "output" / "web_runs"))
    app.state.store = store
    app.state.client = client
    app.state.samples_dir = samples_dir

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

    def samples() -> Path:
        return ensure_samples(app.state.samples_dir)

    def _run(form_path: Path, memo: str, mode: str, *, naive: Optional[Path], spec: Optional[dict],
             demo_id: Optional[str], display: str) -> tuple[str, RunState]:
        run_id = new_run_id()
        run_dir = store.root / run_id
        try:
            result = run_pipeline(form_path, memo, run_dir, mode=mode, naive_path=naive, slot_spec=spec,
                                  demo_id=demo_id, run_id=run_id, client=app.state.client)
        except Exception as exc:  # noqa: BLE001 - surface any pipeline error to the UI
            raise HTTPException(422, f"pipeline failed: {exc}") from exc
        blob, sha = _verified_copy(result)
        st = RunState(result=result, ai_mappings=[m.model_copy(deep=True) for m in result.mappings],
                      form_path=form_path, memo_text=memo, naive_path=naive, spec=spec, display_name=display,
                      verified_bytes=blob, verified_sha256=sha)
        store.put(run_id, st)
        return run_id, st

    @app.get("/", response_class=HTMLResponse)
    def index():
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html; charset=utf-8")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/api/health")
    def health():
        live = live_credentials_present() or app.state.client is not None
        return {"status": "ok", "version": __version__, "live_available": live,
                "default_mode": "live" if live else "demo", "default_model": os.environ.get("FORM_GUARD_MODEL") or DEFAULT_MODEL}

    @app.get("/api/demos")
    def demos():
        base = samples()
        out = []
        for d in DEMOS:
            p = demo_paths(d, base)
            out.append({**{k: d[k] for k in ("id", "title", "title_en", "org", "file_type", "highlights")},
                        "form": p["form"].name, "memo_name": p["memo"].name, "memo_text": read_memo(p["memo"])})
        return out

    @app.post("/api/runs")
    def run_demo(req: RunRequest):
        try:
            d = demo_by_id(req.demo_id)
        except KeyError as exc:
            raise HTTPException(404, "unknown demo") from exc
        p = demo_paths(d, samples())
        memo = req.memo_text if req.memo_text is not None else read_memo(p["memo"])
        run_id, st = _run(p["form"], memo, req.mode, naive=p["naive"], spec=None, demo_id=d["id"], display=p["form"].name)
        return _payload(run_id, st)

    @app.post("/api/runs/upload")
    def run_upload(file: UploadFile = File(...), memo_text: str = Form(...), mode: str = Form("auto"),
                   slot_spec: Optional[UploadFile] = File(None)):
        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in MEDIA:
            raise HTTPException(400, "upload a .docx or .xlsx form")
        if len(memo_text) > MAX_MEMO_CHARS:
            raise HTTPException(413, "memo too long")
        data = file.file.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "file larger than 10 MB")
        staging = store.root / "_uploads" / new_run_id()
        staging.mkdir(parents=True, exist_ok=True)
        form_path = staging / f"{_safe_stem(file.filename or 'form')}{suffix}"
        form_path.write_bytes(data)
        ftype = _check_package(form_path)
        if f".{ftype}" != suffix:
            raise HTTPException(400, f"file content is {ftype}, but the extension is {suffix}")
        spec = None
        if slot_spec is not None and slot_spec.filename:
            raw = slot_spec.file.read(200_001)
            if len(raw) > 200_000:
                raise HTTPException(413, "slot spec too large")
            try:
                spec = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise HTTPException(400, f"slot spec is not valid JSON: {exc}") from exc
        run_id, st = _run(form_path, memo_text, mode, naive=None, spec=spec, demo_id=None, display=file.filename or form_path.name)
        return _payload(run_id, st)

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str):
        return _payload(run_id, store.get(run_id))

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
            if (req.revision is not None and req.revision != st.revision) or \
                    (req.filled_sha256 is not None and req.filled_sha256 != st.verified_sha256):
                raise HTTPException(409, {"message": "stale view or file modified — refresh before approving",
                                          "revision": st.revision, "filled_sha256": st.verified_sha256})
            try:
                on_disk = _sha(st.result.filled_path)
            except OSError:
                on_disk = ""
            if on_disk != st.verified_sha256:
                raise HTTPException(409, {"message": "the filled file was modified after verification — run it again"})
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
            (st.result.out_dir or store.root / run_id).joinpath("approval.json").write_text(
                json.dumps(st.approval, ensure_ascii=False, indent=2), encoding="utf-8")
            return {"approved": True, "download_url": f"/api/runs/{run_id}/download", **st.approval}

    @app.get("/api/runs/{run_id}/download")
    def download(run_id: str):
        st = store.get(run_id)
        with st.lock:
            if not st.approval:
                raise HTTPException(403, "human-in-the-loop approval is required before download")
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
        p = (st.result.out_dir or store.root / run_id) / "report.html"
        return HTMLResponse(p.read_text(encoding="utf-8"))

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
