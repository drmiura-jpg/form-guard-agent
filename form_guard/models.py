"""Typed data models shared by the scanner, mapper, writer, verifier and web API.

All models are Pydantic v2 models so they serialise straight into the JSON audit
log and the FastAPI responses.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

FileType = Literal["docx", "xlsx", "pdf"]
# date_template = a cell printed as an era-date skeleton (「令和　　年　　月　　日」), designated by coordinates: Word replaces
# the skeleton's characters like a marker; Excel rewrites the cell as one inline string
# checkbox_text: a printed "□ 選択肢　□ 選択肢" line whose □ of the chosen option becomes ■ (Word / Excel / PDF)
MarkerKind = Literal["braces", "kinyuran", "empty_cell", "date_template", "checkbox_text"]
# recovered = a value read back from a filled file whose provenance is unknown (CLI --verify without memo / audit log)
MappingStatus = Literal["filled", "needs_human_input", "human_entered", "recovered"]
Severity = Literal["critical", "major", "minor", "info"]


class SlotOccurrence(BaseModel):
    """One physical place in the file where a slot marker (or target cell) lives."""

    part_name: str
    container_index: int
    location_path: str
    marker: str
    marker_kind: MarkerKind = "braces"
    paragraph_index: Optional[int] = None
    table_idx: Optional[int] = None
    row_idx: Optional[int] = None
    col_idx: Optional[int] = None
    cell_ref: Optional[str] = None
    sheet_name: Optional[str] = None
    sheet_index: Optional[int] = None
    box_id: str = ""
    context_text: str = ""
    # fixed PDF forms: the page and the ruled box (PDF user space, points, origin bottom-left: x0, y0, x1, y1)
    page_index: Optional[int] = None
    rect: Optional[list[float]] = None
    # checkbox_text: every printed option — label, character offset of its □ in the container text, and for PDF the
    # origin / size / font of the printed □ glyph
    checkbox_options: list[dict[str, Any]] = Field(default_factory=list)


class FormSlot(BaseModel):
    """A fillable field discovered in a blank form."""

    slot_id: str
    label: str
    location_path: str
    file_type: FileType
    current_text: str
    max_chars: Optional[int] = None
    required_format: Optional[str] = None
    constraints: dict[str, Any] = Field(default_factory=dict)
    context_label: str = ""
    hints: list[str] = Field(default_factory=list)
    description: str = ""
    aliases: list[str] = Field(default_factory=list)
    importance: Literal["high", "normal"] = "normal"
    occurrences: list[SlotOccurrence] = Field(default_factory=list)

    @property
    def table_idx(self) -> Optional[int]:
        return self.occurrences[0].table_idx if self.occurrences else None

    @property
    def row_idx(self) -> Optional[int]:
        return self.occurrences[0].row_idx if self.occurrences else None

    @property
    def col_idx(self) -> Optional[int]:
        return self.occurrences[0].col_idx if self.occurrences else None


class SourceSpan(BaseModel):
    """A verbatim excerpt of the source memo, located by character offsets."""

    text: str
    start_char: int
    end_char: int
    line_start: int
    line_end: int


class ValidationIssue(BaseModel):
    code: str
    message: str
    severity: Literal["error", "warning"] = "error"


class CorrectionAttempt(BaseModel):
    """One step of the self-correction loop for a single field."""

    attempt: int
    action: str
    value: str
    source_quotes: list[str] = Field(default_factory=list)
    issues: list[ValidationIssue] = Field(default_factory=list)
    passed: bool = False


class FieldMapping(BaseModel):
    """The value chosen for a slot, with its verbatim evidence and validation trail."""

    slot_id: str
    filled_value: str = ""
    source_quote: str = ""
    source_line_range: Optional[tuple[int, int]] = None
    confidence: float = 0.0
    reasoning: str = ""
    char_count: int = 0
    validation_passed: bool = False
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    status: MappingStatus = "needs_human_input"
    suggested_value: str = ""
    source_spans: list[SourceSpan] = Field(default_factory=list)
    attempts: list[CorrectionAttempt] = Field(default_factory=list)
    normalizations: list[str] = Field(default_factory=list)
    engine: str = ""
    condensed_removed: list[str] = Field(default_factory=list)  # full text deleted to meet a character limit
    # Live: the model id(s) that served this field ("claude-haiku-4-5-20251001 → claude-opus-5-5 (revision)").
    # Offline: the model live auto mode would use ("offline (claude-haiku-4-5-20251001 in live auto)") — no call was made.
    routed_model: str = ""
    route_reason: str = ""

    @property
    def is_written(self) -> bool:
        return self.status in ("filled", "human_entered", "recovered") and bool(self.filled_value)


class AuditCheckItem(BaseModel):
    check_id: str
    category: str
    description: str
    passed: bool
    details: str = ""
    severity: Severity = "major"
    evidence: dict[str, Any] = Field(default_factory=dict)


class HITLItem(BaseModel):
    """One line of the human-in-the-loop final confirmation checklist."""

    item_id: str
    kind: Literal["field", "pending_input", "global"]
    prompt: str  # 日本語 / English, as recorded in the audit log and the report
    prompt_ja: str = ""  # the same prompt in one language (the web UI shows the selected one)
    prompt_en: str = ""
    slot_id: Optional[str] = None
    label: str = ""
    value: str = ""
    source_quote: str = ""
    source_line_range: Optional[tuple[int, int]] = None
    importance: Literal["high", "normal"] = "normal"
    flags: list[str] = Field(default_factory=list)
    required: bool = True


class EntryHash(BaseModel):
    name: str
    role: Literal["untouched", "target", "added", "removed"]
    original_sha256: str = ""
    filled_sha256: str = ""
    identical: bool = False
    raw_identical: bool = False
    metadata_identical: bool = False
    size: int = 0


class PatchRecord(BaseModel):
    """A byte-range edit made by the surgical writer (OOXML: offsets refer to the original part; PDF: the slot's block
    inside the overlay stream of the appended incremental update — no original byte is rewritten)."""

    part_name: str
    slot_id: str
    # text / attr: marker characters (or a Word 「令和　　年　　月　　日」 skeleton); insert_run / expand_empty: a run in an
    # empty Word cell; fill_cell: an existing empty worksheet <c>; fill_template_cell: a worksheet <c> printed as
    # 「令和　　年　　月　　日」, rewritten as the date; insert_cell: a worksheet <c> (and row) that did not exist yet;
    # checkbox: one printed □ replaced by ■ (same byte length); pdf_text / pdf_mark: a value / a ■ in the PDF overlay
    kind: Literal["text", "attr", "insert_run", "expand_empty", "fill_cell", "fill_template_cell", "insert_cell",
                  "checkbox", "pdf_text", "pdf_mark"]
    byte_start: int
    byte_end: int
    old_text: str
    new_text: str


class VerificationReport(BaseModel):
    file_type: FileType
    zip_integrity_passed: bool
    xml_schema_wellformed: bool
    non_target_xml_unchanged: bool
    style_and_borders_unchanged: bool
    all_slots_filled: bool
    ungrounded_claims_count: int
    checks: list[AuditCheckItem] = Field(default_factory=list)
    hitl_checklist: list[HITLItem] = Field(default_factory=list)
    visual_diff_summary: dict[str, Any] = Field(default_factory=dict)
    overall_status: Literal["PASS", "NEEDS_HUMAN_INPUT", "FAIL"] = "FAIL"
    passed_count: int = 0
    failed_count: int = 0
    pending_slots: list[str] = Field(default_factory=list)
    entry_hashes: list[EntryHash] = Field(default_factory=list)
    stats: dict[str, Any] = Field(default_factory=dict)
    subject: str = "form_guard"
