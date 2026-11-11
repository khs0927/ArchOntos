import asyncio
from dataclasses import asdict
from datetime import UTC, date, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError

from apps.common import authorization_enabled, create_service, require
from archontos.actions.gate import ApprovalDenied, assert_can_execute
from archontos.actions.persistence import PostgresActionStore
from archontos.actions.service import ProposedAction
from archontos.authz import AuthorizationError, Permission, authorize
from archontos.cad.bcf import export_bcf
from archontos.cad.check_persistence import CadCheckRepository, inputs_digest
from archontos.cad.checks import CadCheckInputError, extract_fact_space, run_checks
from archontos.cad.diff import DiffInputError, DiffTolerances
from archontos.cad.diff_persistence import CadDiffRepository, RevisionNotFound
from archontos.cad.dxf import CadDependencyError, CadParseError, parse_dxf
from archontos.cad.ifc import parse_ifc
from archontos.cad.issue_persistence import CadIssueRepository, RunNotFound
from archontos.cad.links import LinkInputError, ModelLinkRepository
from archontos.cad.markers import MarkerError, apply_markers_to_dxf, to_power_cad_payload
from archontos.cad.model import UnsupportedModelFormat, building_facts, detect_source_format
from archontos.cad.outputs import (
    MARKER_ACTION_TYPE,
    DiffNotFound,
    build_report,
    find_marker_output,
    get_output,
    insert_artifact,
    marker_plan_for_run,
    output_id_for,
    record_output,
    revision_source_uri,
)
from archontos.cad.persistence import (
    CadConflict,
    CadRepository,
    InvalidCadCursor,
    decode_cursor,
    encode_cursor,
    facts_export,
    filter_fingerprint,
)
from archontos.cad.report import (
    REPORT_VERSION,
    canonical_bytes,
    content_sha256,
    render_html,
    render_json,
)
from archontos.cad.skp import parse_skp_json
from archontos.config import get_settings
from archontos.db.session import get_session_factory
from archontos.identity import current_actor, current_jurisdictions, current_principal
from archontos.ingestion.adapters import LawGoKrAdapter, SourceAdapterError
from archontos.ingestion.contracts import NormalizedLegalVersion
from archontos.ingestion.persistence import CanonicalizationError, SourceVersionConflict
from archontos.ingestion.service import LawIngestionService, LawNotFoundError
from archontos.storage.artifacts import LocalArtifactStore, MinioArtifactStore

app = create_service("ingestion")
settings = get_settings()


def _lawgo() -> LawGoKrAdapter:
    return LawGoKrAdapter(base_url=settings.lawgo_base_url, oc=settings.lawgo_oc)


def _artifact_store():
    if settings.artifact_backend == "minio":
        return MinioArtifactStore(
            endpoint=settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            bucket=settings.minio_bucket,
            secure=settings.minio_secure,
        )
    if settings.artifact_backend == "local":
        return LocalArtifactStore(settings.artifact_local_path)
    raise RuntimeError(f"unsupported artifact backend: {settings.artifact_backend}")


@app.post("/v1/contracts/legal-version/validate", dependencies=[require(Permission.QUERY_READ)])
async def validate_legal_version(payload: NormalizedLegalVersion):
    return {
        "valid": True,
        "source_key": payload.source_key,
        "evidence_count": len(payload.evidence),
    }


@app.get("/v1/sources/lawgo/search", dependencies=[require(Permission.QUERY_READ)])
async def search_lawgo(
    query: Annotated[str, Query(min_length=1)],
    page: Annotated[int, Query(ge=1)] = 1,
    display: Annotated[int, Query(ge=1, le=100)] = 20,
):
    try:
        result = await _lawgo().search_laws(query, page=page, display=display)
    except SourceAdapterError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        "target": result.target,
        "query": result.query,
        "page": result.page,
        "total_count": result.total_count,
        "artifact_sha256": result.envelope.sha256,
        "items": [asdict(item) for item in result.items],
    }


@app.get("/v1/sources/lawgo/effective-versions", dependencies=[require(Permission.QUERY_READ)])
async def search_lawgo_effective_versions(
    query: Annotated[str, Query(min_length=1)],
    page: Annotated[int, Query(ge=1)] = 1,
    display: Annotated[int, Query(ge=1, le=100)] = 100,
):
    try:
        result = await _lawgo().search_effective_versions(
            query,
            page=page,
            display=display,
        )
    except SourceAdapterError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        "target": result.target,
        "query": result.query,
        "page": result.page,
        "total_count": result.total_count,
        "artifact_sha256": result.envelope.sha256,
        "items": [asdict(item) for item in result.items],
    }


@app.post("/v1/sources/lawgo/ingest-current", dependencies=[require(Permission.SOURCE_INGEST)])
async def ingest_lawgo_current(query: Annotated[str, Query(min_length=1)]):
    service = LawIngestionService(
        adapter=_lawgo(),
        artifact_store=_artifact_store(),
        session_factory=get_session_factory(),
    )
    try:
        result = await service.ingest_current(query)
    except LawNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SourceVersionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except CanonicalizationError as exc:
        # A malformed or unverifiable official document is a bad request, not a
        # server fault. It must not be retried either: the input is not
        # transient, and retrying re-fetches the same wrong body.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except SourceAdapterError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        "law_id": result.item.law_id,
        "mst": result.item.mst,
        "source_version_id": str(result.persisted.source_version_id),
        "created": result.persisted.created,
        "body_sha256": result.body_sha256,
        "discovery_sha256": result.discovery_sha256,
    }


# --- MVP-1: CAD (DXF) drawings -> raw entities -> architectural objects -------------------

CAD_MAX_BYTES = 50 * 1024 * 1024
_CAD_STORAGE = {
    "dxf": ("image/vnd.dxf", ".dxf"),
    "ifc": ("application/x-step", ".ifc"),
    "skp_json": ("application/json", ".json"),
}
_SOURCE_TEXT = r"^[^\x00-\x1f]{1,300}$"


@app.post("/v1/cad/artifacts", status_code=201, dependencies=[require(Permission.CAD_INGEST)])
async def upload_cad_artifact(
    request: Request,
    response: Response,
    project_id: Annotated[str, Query(min_length=1, max_length=200, pattern=_SOURCE_TEXT)],
    source_key: Annotated[str, Query(min_length=1, max_length=300, pattern=_SOURCE_TEXT)],
    jurisdiction_code: Annotated[str, Query(min_length=2, max_length=20)] = "KR",
    revision_label: Annotated[str | None, Query(min_length=1, max_length=100)] = None,
    file_name: Annotated[str | None, Query(max_length=255)] = None,
    assume_units: Annotated[str, Query(pattern=r"^(mm|cm|m|in|ft)$")] = "mm",
    source_format: Annotated[str, Query(pattern=r"^(auto|dxf|ifc|skp_json)$")] = "auto",
):
    """Upload DXF, IFC or SketchUp-JSON bytes (request body); returns the revision.

    ``source_format=auto`` sniffs the content (``ISO-10303-21`` -> IFC, ``{`` -> SketchUp JSON
    ``archontos-skp-export/1``), then the file name / content type, else DXF. Binary ``.skp``
    is refused with 415. Same bytes -> same ids (200)."""
    if jurisdiction_code not in current_jurisdictions():
        raise HTTPException(status_code=403, detail="jurisdiction not allowed for this caller")
    data = await request.body()
    if len(data) > CAD_MAX_BYTES:
        raise HTTPException(status_code=413, detail="CAD upload exceeds 50 MiB")
    try:
        fmt = detect_source_format(
            data,
            explicit=source_format,
            content_type=request.headers.get("content-type"),
            file_name=file_name,
        )
    except UnsupportedModelFormat as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc
    try:
        if fmt == "ifc":
            drawing = await asyncio.to_thread(parse_ifc, data)
        elif fmt == "skp_json":
            drawing = await asyncio.to_thread(parse_skp_json, data)
        else:
            drawing = await asyncio.to_thread(parse_dxf, data, assume_units=assume_units)
    except CadParseError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except CadDependencyError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    mime, suffix = _CAD_STORAGE[fmt]
    artifact = await _artifact_store().put_bytes("cad", data, mime=mime, suffix=suffix)
    async with get_session_factory()() as session:
        try:
            async with session.begin():
                result = await CadRepository(session).persist(
                    drawing=drawing,
                    artifact=artifact,
                    project_id=project_id,
                    source_key=source_key,
                    jurisdiction_code=jurisdiction_code,
                    revision_label=revision_label,
                    file_name=file_name,
                )
        except CadConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except IntegrityError as exc:
            raise HTTPException(status_code=409, detail="CAD revision conflict") from exc
    if not result.created:
        response.status_code = 200
    return {"created": result.created, "revision": result.revision}


def _cad_cursor_error(exc: InvalidCadCursor) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


@app.get("/v1/cad/revisions", dependencies=[require(Permission.CAD_READ)])
async def list_cad_revisions(
    project_id: str | None = None,
    source_key: str | None = None,
    sha256: Annotated[str | None, Query(pattern=r"^[0-9a-f]{64}$")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    cursor: str | None = None,
):
    async with get_session_factory()() as session, session.begin():
        try:
            items, nxt = await CadRepository(session).list_revisions(
                project_id=project_id,
                source_key=source_key,
                sha256=sha256,
                limit=limit,
                cursor=cursor,
            )
        except InvalidCadCursor as exc:
            raise _cad_cursor_error(exc) from exc
    return {"items": items, "next_cursor": nxt}


async def _revision_or_404(repo: CadRepository, revision_id: UUID) -> dict:
    revision = await repo.get_revision(str(revision_id))
    if revision is None:
        raise HTTPException(status_code=404, detail="CAD revision not found")
    return revision


@app.get("/v1/cad/revisions/{revision_id}", dependencies=[require(Permission.CAD_READ)])
async def get_cad_revision(revision_id: UUID):
    async with get_session_factory()() as session, session.begin():
        return await _revision_or_404(CadRepository(session), revision_id)


@app.get("/v1/cad/revisions/{revision_id}/objects", dependencies=[require(Permission.CAD_READ)])
async def list_cad_objects(
    revision_id: UUID,
    kind: Annotated[
        str | None, Query(pattern=r"^(wall|door|window|space|stair|column|opening)$")
    ] = None,
    layer: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    cursor: str | None = None,
):
    async with get_session_factory()() as session, session.begin():
        repo = CadRepository(session)
        await _revision_or_404(repo, revision_id)
        try:
            items, nxt = await repo.list_objects(
                str(revision_id), kind=kind, layer=layer, limit=limit, cursor=cursor
            )
        except InvalidCadCursor as exc:
            raise _cad_cursor_error(exc) from exc
    return {"items": items, "next_cursor": nxt}


@app.get("/v1/cad/revisions/{revision_id}/entities", dependencies=[require(Permission.CAD_READ)])
async def list_cad_entities(
    revision_id: UUID,
    layer: str | None = None,
    entity_type: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    cursor: str | None = None,
):
    async with get_session_factory()() as session, session.begin():
        repo = CadRepository(session)
        await _revision_or_404(repo, revision_id)
        try:
            items, nxt = await repo.list_entities(
                str(revision_id),
                layer=layer,
                entity_type=entity_type,
                limit=limit,
                cursor=cursor,
            )
        except InvalidCadCursor as exc:
            raise _cad_cursor_error(exc) from exc
    return {"items": items, "next_cursor": nxt}


@app.get("/v1/cad/revisions/{revision_id}/facts", dependencies=[require(Permission.CAD_READ)])
async def cad_revision_facts(revision_id: UUID):
    """The revision as ``aec-facts-export/1`` (input of archontos.integration.ontology)."""
    async with get_session_factory()() as session, session.begin():
        repo = CadRepository(session)
        revision = await _revision_or_404(repo, revision_id)
        objects = await repo.all_objects(str(revision_id))
    return facts_export(revision, objects)


# --- MVP-2: regulation-to-CAD checks ---------------------------------------------------------


class CadCheckRequest(BaseModel):
    """Run the active, approved rules of a jurisdiction against one CAD revision.

    ``land_parcel`` and ``facts`` follow the Ontology-platform ``POST /api/v1/regulation/evaluate``
    body (``{rule, land_parcel, facts}``) and the site-pipeline ``parcel_facts`` shape. They are
    taken from the request only; this endpoint makes no network calls.
    """

    model_config = ConfigDict(extra="forbid")

    jurisdiction_code: str = Field(default="KR", min_length=2, max_length=20)
    reference_date: date | None = None
    land_parcel: dict[str, Any] = Field(default_factory=dict)
    facts: dict[str, Any] = Field(default_factory=dict)
    fact_units: dict[str, str] = Field(default_factory=dict)
    rule_version_ids: list[UUID] | None = Field(default=None, max_length=500)


@app.post(
    "/v1/cad/revisions/{revision_id}/checks",
    status_code=201,
    dependencies=[require(Permission.CAD_CHECK)],
)
async def run_cad_checks(revision_id: UUID, payload: CadCheckRequest, response: Response):
    """Evaluate bound rules; same revision + inputs + rules -> same run (200, created false)."""
    if payload.jurisdiction_code not in current_jurisdictions():
        raise HTTPException(status_code=403, detail="jurisdiction not allowed for this caller")
    reference_date = payload.reference_date or datetime.now(UTC).date()
    ids = [str(i) for i in payload.rule_version_ids] if payload.rule_version_ids else None
    async with get_session_factory()() as session, session.begin():
        cad = CadRepository(session)
        revision = await _revision_or_404(cad, revision_id)
        objects = await cad.all_objects(str(revision_id))
        checks = CadCheckRepository(session)
        rules = await checks.load_rules(reference_date=reference_date, rule_version_ids=ids)
        # MVP-4: IFC storeys give building.floor_count etc.; the model wins, never the caller.
        model_facts, model_units = building_facts(revision.get("model_meta"))
        extra_facts = dict(payload.facts or {})
        extra_units = dict(payload.fact_units or {})
        if model_facts:
            if "building" in extra_facts:
                raise HTTPException(
                    status_code=422, detail="facts.building is derived from the model storeys"
                )
            extra_facts.update(model_facts)
            extra_units.update(model_units)
        try:
            run = run_checks(
                rules,
                extract_fact_space(objects, model_meta=revision.get("model_meta")),
                jurisdiction=payload.jurisdiction_code,
                reference_date=reference_date,
                land_parcel=payload.land_parcel,
                extra_facts=extra_facts,
                extra_fact_units=extra_units,
            )
        except CadCheckInputError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        bound = [r.rule for r in run.results]
        digest = inputs_digest(
            land_parcel=payload.land_parcel,
            facts={"facts": payload.facts, "fact_units": payload.fact_units},
            rules=bound,
        )
        persisted = await checks.persist(
            revision_id=str(revision_id), run=run, land_parcel=payload.land_parcel, digest=digest
        )
        results, _ = await checks.list_results(
            str(revision_id),
            run_id=persisted.run["id"],
            outcome=None,
            rule_version_id=None,
            arch_object_id=None,
            limit=500,
            cursor=None,
        )
    if not persisted.created:
        response.status_code = 200
    return {
        "created": persisted.created,
        "run": persisted.run,
        "skipped_rules": run.skipped,
        "results": results,
    }


@app.get("/v1/cad/revisions/{revision_id}/checks", dependencies=[require(Permission.CAD_READ)])
async def list_cad_check_runs(revision_id: UUID):
    async with get_session_factory()() as session, session.begin():
        await _revision_or_404(CadRepository(session), revision_id)
        return {"items": await CadCheckRepository(session).list_runs(str(revision_id))}


@app.get(
    "/v1/cad/revisions/{revision_id}/checks/results",
    dependencies=[require(Permission.CAD_READ)],
)
async def list_cad_check_results(
    revision_id: UUID,
    run_id: UUID | None = None,
    outcome: Annotated[str | None, Query(pattern=r"^(PASS|FAIL|REVIEW)$")] = None,
    rule_version_id: UUID | None = None,
    arch_object_id: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    cursor: str | None = None,
):
    async with get_session_factory()() as session, session.begin():
        await _revision_or_404(CadRepository(session), revision_id)
        try:
            items, nxt = await CadCheckRepository(session).list_results(
                str(revision_id),
                run_id=str(run_id) if run_id else None,
                outcome=outcome,
                rule_version_id=str(rule_version_id) if rule_version_id else None,
                arch_object_id=str(arch_object_id) if arch_object_id else None,
                limit=limit,
                cursor=cursor,
            )
        except InvalidCadCursor as exc:
            raise _cad_cursor_error(exc) from exc
    return {"items": items, "next_cursor": nxt}


# --- MVP-3: permit vs construction / revision diff ---------------------------------------------


class CadDiffRequest(BaseModel):
    """Diff two revisions of one drawing lineage, or any explicitly paired two (permit set vs
    construction set). ``rerun_checks`` re-runs, on the target revision, every check run of
    the base revision (same jurisdiction, date, land parcel and caller facts) and reports the
    outcome transitions; it also needs ``cad.check``."""

    model_config = ConfigDict(extra="forbid")

    base_revision_id: UUID
    target_revision_id: UUID
    tolerances: dict[str, Any] = Field(default_factory=dict)
    rerun_checks: bool = False


@app.post("/v1/cad/diffs", status_code=201, dependencies=[require(Permission.CAD_DIFF)])
async def create_cad_diff(payload: CadDiffRequest, response: Response):
    """Same revisions + tolerances + rerun flag -> same diff (200, created false)."""
    if payload.rerun_checks:
        try:
            authorize(current_principal(), Permission.CAD_CHECK, enabled=authorization_enabled())
        except AuthorizationError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    try:
        tolerances = DiffTolerances.from_mapping(payload.tolerances)
        async with get_session_factory()() as session, session.begin():
            persisted = await CadDiffRepository(session).create(
                base_revision_id=str(payload.base_revision_id),
                target_revision_id=str(payload.target_revision_id),
                tolerances=tolerances,
                rerun_checks=payload.rerun_checks,
            )
    except RevisionNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DiffInputError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not persisted.created:
        response.status_code = 200
    return {"created": persisted.created, "diff": persisted.diff}


async def _diff_or_404(repo: CadDiffRepository, diff_id: UUID) -> dict:
    diff = await repo.get_diff(str(diff_id))
    if diff is None:
        raise HTTPException(status_code=404, detail="CAD diff not found")
    return diff


@app.get("/v1/cad/diffs/{diff_id}", dependencies=[require(Permission.CAD_DIFF)])
async def get_cad_diff(diff_id: UUID):
    async with get_session_factory()() as session, session.begin():
        return await _diff_or_404(CadDiffRepository(session), diff_id)


@app.get("/v1/cad/diffs/{diff_id}/changes", dependencies=[require(Permission.CAD_DIFF)])
async def list_cad_diff_changes(
    diff_id: UUID,
    change_type: Annotated[
        str | None, Query(pattern=r"^(added|removed|modified|unchanged)$")
    ] = None,
    kind: Annotated[
        str | None, Query(pattern=r"^(wall|door|window|space|stair|column|opening)$")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    cursor: str | None = None,
):
    async with get_session_factory()() as session, session.begin():
        repo = CadDiffRepository(session)
        await _diff_or_404(repo, diff_id)
        try:
            items, nxt = await repo.list_changes(
                str(diff_id), change_type=change_type, kind=kind, limit=limit, cursor=cursor
            )
        except InvalidCadCursor as exc:
            raise _cad_cursor_error(exc) from exc
    return {"items": items, "next_cursor": nxt}


# --- MVP-4: cross-source model links (IFC / SketchUp JSON / DXF) ----------------------------


class ModelLinkRequest(BaseModel):
    """Link two objects of different revisions (usually different source formats)."""

    model_config = ConfigDict(extra="forbid")
    object_a_id: UUID
    object_b_id: UUID
    note: str | None = Field(default=None, max_length=1000)


@app.post("/v1/models/links", status_code=201, dependencies=[require(Permission.CAD_LINK)])
async def create_model_link(payload: ModelLinkRequest, response: Response):
    """Manual cross-source link; the same pair again returns the existing link (200)."""
    async with get_session_factory()() as session:
        try:
            async with session.begin():
                persisted = await ModelLinkRepository(session).create_manual(
                    object_a_id=str(payload.object_a_id),
                    object_b_id=str(payload.object_b_id),
                    note=payload.note,
                )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except LinkInputError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except IntegrityError as exc:
            raise HTTPException(status_code=409, detail="model link conflict") from exc
    if not persisted.created:
        response.status_code = 200
    return {"created": persisted.created, "link": persisted.link}


@app.get("/v1/models/links", dependencies=[require(Permission.CAD_READ)])
async def list_model_links(
    project_id: Annotated[str | None, Query(max_length=200)] = None,
    revision_id: UUID | None = None,
    object_id: UUID | None = None,
    method: Annotated[str | None, Query(pattern=r"^(shared_ref|manual)$")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    cursor: str | None = None,
):
    """Cross-source links visible to the caller (jurisdiction RLS), ordered by id."""
    rev = str(revision_id) if revision_id else None
    obj = str(object_id) if object_id else None
    fp = filter_fingerprint(p=project_id, r=rev, o=obj, m=method)
    try:
        after = decode_cursor(cursor, fp) if cursor else None
    except InvalidCadCursor as exc:
        raise _cad_cursor_error(exc) from exc
    async with get_session_factory()() as session, session.begin():
        rows = await ModelLinkRepository(session).list_links(
            project_id=project_id,
            revision_id=rev,
            object_id=obj,
            method=method,
            limit=limit + 1,
            after=after,
        )
    items = rows[:limit]
    nxt = encode_cursor(items[-1]["id"], fp) if len(rows) > limit else None
    return {"items": items, "next_cursor": nxt}


# --- MVP-5: issues + BCF 3.0 export -------------------------------------------------------------

_ISSUE_STATUS = r"^(open|resolved|stale)$"


@app.post("/v1/cad/checks/{run_id}/issues", dependencies=[require(Permission.CAD_ISSUE)])
async def derive_cad_issues(run_id: UUID):
    """Derive (idempotently) one issue per FAIL/REVIEW (rule_version, arch_object) of a run."""
    async with get_session_factory()() as session, session.begin():
        try:
            derived = await CadIssueRepository(session).derive(str(run_id))
        except RunNotFound as exc:
            raise HTTPException(status_code=404, detail="check run not found") from exc
    return {
        "run_id": str(run_id),
        "new_issue_count": derived.new_issue_count,
        "new_status_count": derived.new_status_count,
        "items": derived.issues,
    }


@app.get("/v1/cad/issues", dependencies=[require(Permission.CAD_READ)])
async def list_cad_issues(
    run_id: UUID | None = None,
    revision_id: UUID | None = None,
    status: Annotated[str | None, Query(pattern=_ISSUE_STATUS)] = None,
    outcome: Annotated[str | None, Query(pattern=r"^(FAIL|REVIEW)$")] = None,
    rule_version_id: UUID | None = None,
    origin: Annotated[str | None, Query(pattern=r"^(check|regression)$")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    cursor: str | None = None,
):
    async with get_session_factory()() as session, session.begin():
        try:
            items, nxt = await CadIssueRepository(session).list_issues(
                run_id=str(run_id) if run_id else None,
                revision_id=str(revision_id) if revision_id else None,
                status=status,
                outcome=outcome,
                rule_version_id=str(rule_version_id) if rule_version_id else None,
                origin=origin,
                limit=limit,
                cursor=cursor,
            )
        except InvalidCadCursor as exc:
            raise _cad_cursor_error(exc) from exc
    return {"items": items, "next_cursor": nxt}


@app.get("/v1/cad/issues/export.bcf", dependencies=[require(Permission.CAD_READ)])
async def export_cad_issues_bcf(
    run_id: UUID | None = None,
    revision_id: UUID | None = None,
    status: Annotated[str | None, Query(pattern=_ISSUE_STATUS)] = None,
):
    """BCF 3.0 zip of the caller-visible issues of one run or revision (no writes)."""
    if run_id is None and revision_id is None:
        raise HTTPException(status_code=422, detail="run_id or revision_id is required")
    async with get_session_factory()() as session, session.begin():
        issues = await CadIssueRepository(session).all_issues(
            run_id=str(run_id) if run_id else None,
            revision_id=str(revision_id) if revision_id else None,
            status=status,
            outcome=None,
            rule_version_id=None,
            origin=None,
        )
    if not issues:
        raise HTTPException(status_code=404, detail="no issues")
    data = export_bcf(issues, project_id=issues[0]["project_id"])
    name = f"archontos-{run_id or revision_id}.bcf"
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


# --- MVP-5: compliance reports + marker actions (approval gate) ---------------------------------


class CadReportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: UUID
    diff_id: UUID | None = None
    title: str = Field(default="CAD 법규 검토 보고서", min_length=1, max_length=200)
    generated_at: datetime | None = None


async def _run_or_404(session, run_id: UUID) -> dict:
    run = await CadCheckRepository(session).get_run(str(run_id))
    if run is None:
        raise HTTPException(status_code=404, detail="check run not found")
    return run


@app.post("/v1/cad/reports", status_code=201, dependencies=[require(Permission.CAD_REPORT)])
async def create_cad_report(payload: CadReportRequest, response: Response):
    """HTML + JSON compliance report of one run, stored as new artifacts (same content -> 200)."""
    generated_at = (payload.generated_at or datetime.now(UTC)).isoformat()
    store = _artifact_store()
    async with get_session_factory()() as session, session.begin():
        run = await _run_or_404(session, payload.run_id)
        try:
            report = await build_report(
                session,
                run,
                title=payload.title,
                generated_at=generated_at,
                diff_id=str(payload.diff_id) if payload.diff_id else None,
            )
        except DiffNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        body = canonical_bytes(report)
        sha = content_sha256(report)
        json_ref = await store.put_bytes(
            "cad-report", body, mime="application/json", suffix=".json"
        )
        html_ref = await store.put_bytes(
            "cad-report",
            render_html(report).encode("utf-8"),
            mime="text/html; charset=utf-8",
            suffix=".html",
        )
        json_id = await insert_artifact(session, json_ref)
        html_id = await insert_artifact(session, html_ref)
        issues = await CadIssueRepository(session).all_issues(
            run_id=run["id"],
            revision_id=None,
            status=None,
            outcome=None,
            rule_version_id=None,
            origin=None,
        )
        link, created = await record_output(
            session,
            output_id=output_id_for("report", run["id"], sha),
            kind="report",
            artifact_id=json_id,
            run_id=run["id"],
            revision_id=run["revision_id"],
            action_id=None,
            issue_ids=[i["id"] for i in issues],
            sha256=sha,
            media_type="application/json",
            meta={
                "html_artifact_id": html_id,
                "html_storage_uri": html_ref.storage_uri,
                "html_sha256": html_ref.content_hash,
                "report_version": REPORT_VERSION,
                "diff_id": str(payload.diff_id) if payload.diff_id else None,
            },
            jurisdiction_code=run["jurisdiction_code"],
            topic="cad.report-generated",
            event_type="CadReportGenerated",
        )
    if not created:
        response.status_code = 200
    return {"created": created, "report": link, "content": render_json(report)}


@app.get("/v1/cad/reports/{report_id}", dependencies=[require(Permission.CAD_READ)])
async def get_cad_report(
    report_id: UUID, format: Annotated[str, Query(pattern=r"^(meta|json|html)$")] = "meta"
):
    async with get_session_factory()() as session, session.begin():
        link = await get_output(session, str(report_id))
    if link is None or link["kind"] != "report":
        raise HTTPException(status_code=404, detail="report not found")
    if format == "meta":
        return link
    uri = link["storage_uri"] if format == "json" else link["meta"]["html_storage_uri"]
    data = await _artifact_store().get_bytes(uri)
    media = "application/json" if format == "json" else "text/html; charset=utf-8"
    return Response(content=data, media_type=media)


@app.post(
    "/v1/cad/checks/{run_id}/markers",
    status_code=201,
    dependencies=[require(Permission.ACTION_PROPOSE)],
)
async def propose_cad_markers(run_id: UUID):
    """Propose (not execute) drawing open issues as markers onto a NEW DXF copy.

    The proposal goes through the existing gate: ``POST /v1/actions/{id}/approve`` (four-eyes)
    then ``POST /v1/cad/markers/{id}/execute``.
    """
    async with get_session_factory()() as session, session.begin():
        run = await _run_or_404(session, run_id)
        revision = await CadRepository(session).get_revision(run["revision_id"])
        if revision is None:
            raise HTTPException(status_code=404, detail="CAD revision not found")
        try:
            plan, issue_ids = await marker_plan_for_run(session, run, revision)
        except MarkerError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    proposal = ProposedAction(
        action_type=MARKER_ACTION_TYPE,
        target_refs=[f"cad_check_run:{run['id']}", f"cad_revision:{run['revision_id']}"],
        input_payload={
            "run_id": run["id"],
            "revision_id": run["revision_id"],
            "issue_ids": issue_ids,
            "plan": plan,
        },
        proposed_output={
            "format": "dxf",
            "status": "proposal",
            "plan_sha256": plan["sha256"],
            "marker_count": len(plan["markers"]),
            "power_cad_payload": to_power_cad_payload(plan),
        },
        requires_approval=True,
    )
    stored = await PostgresActionStore(get_session_factory()).propose(
        proposal, created_by=current_actor(default="anonymous")
    )
    return stored.as_api()


@app.post("/v1/cad/markers/{action_id}/execute", dependencies=[require(Permission.ACTION_EXECUTE)])
async def execute_cad_markers(action_id: UUID):
    """Execute an APPROVED marker action: write a new DXF artifact; the original is untouched."""
    actions = PostgresActionStore(get_session_factory())
    found = await actions.get(str(action_id))
    if found is None or found.action_type != MARKER_ACTION_TYPE:
        raise HTTPException(status_code=404, detail="marker action not found")
    if found.status != "succeeded":
        try:
            assert_can_execute(status=found.status, requires_approval=found.requires_approval)
        except ApprovalDenied as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    inputs = found.input_payload
    store = _artifact_store()
    async with get_session_factory()() as session, session.begin():
        existing = await find_marker_output(session, str(action_id))
        if existing is not None:
            return {"created": False, "output": existing, "action": found.as_api()}
        run = await _run_or_404(session, UUID(inputs["run_id"]))
        uri = await revision_source_uri(session, inputs["revision_id"])
        if uri is None:
            raise HTTPException(status_code=404, detail="CAD revision not found")
        original = await store.get_bytes(uri)
        try:
            data = apply_markers_to_dxf(original, inputs["plan"])
        except (CadParseError, CadDependencyError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        ref = await store.put_bytes("cad-markers", data, mime="image/vnd.dxf", suffix=".dxf")
        art_id = await insert_artifact(session, ref)
        link, _ = await record_output(
            session,
            output_id=output_id_for("markers", str(action_id)),
            kind="markers",
            artifact_id=art_id,
            run_id=run["id"],
            revision_id=inputs["revision_id"],
            action_id=str(action_id),
            issue_ids=list(inputs["issue_ids"]),
            sha256=ref.content_hash,
            media_type="image/vnd.dxf",
            meta={"plan_sha256": inputs["plan"]["sha256"], "source_storage_uri": uri},
            jurisdiction_code=run["jurisdiction_code"],
            topic="cad.markers-exported",
            event_type="CadMarkersExported",
        )
    executed = found
    if found.status != "succeeded":
        try:
            executed = await actions.execute(str(action_id), current_actor(default="anonymous"))
        except ApprovalDenied as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"created": True, "output": link, "action": executed.as_api()}
