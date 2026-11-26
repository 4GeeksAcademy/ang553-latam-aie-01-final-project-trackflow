"""Versioned, tenant-bound publication catalog for QA knowledge units."""

from dataclasses import dataclass
from hashlib import sha256
import re
import unicodedata

from sqlalchemy import Column, DateTime, MetaData, Table, Text, func, select
from sqlalchemy.dialects.postgresql import ARRAY
from sqlmodel import Session

from services.api.chat.contracts import CoverageTopic
from services.rag.contracts import RetrievalResult


catalog = Table(
    "qa_unit_catalog",
    MetaData(),
    Column("unit_id", Text, primary_key=True),
    Column("version", Text, primary_key=True),
    Column("client_id", Text, primary_key=True),
    Column("approved_text", Text, nullable=False),
    Column("language", Text, nullable=False),
    Column("source_document", Text, nullable=False),
    Column("source_section", Text, nullable=False),
    Column("anchor_document", Text, nullable=False),
    Column("anchor_section", Text, nullable=False),
    Column("anchor_sha256", Text, nullable=False),
    Column("topic_aliases", ARRAY(Text), nullable=False),
    Column("qualifiers", ARRAY(Text), nullable=False),
    Column("composition_group", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("valid_until", DateTime(timezone=True), nullable=True),
)
_ID = re.compile(r"[A-Z][A-Z0-9-]{0,31}")
_VERSION = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}")
_SOURCE = re.compile(r"qa-[a-z0-9][a-z0-9-]{0,59}")
_SECTION = re.compile(r"[a-z0-9][a-z0-9-]{0,59}")
_WORDS = re.compile(r"[a-z]+")
_QUERY = re.compile(r"[a-z\s¿?.,]+")
_QA_INTENT = re.compile(r"\b(?:de prueba|simulad[oa]s?|fictici[oa]s?)\b")
_SYNTHESIS = re.compile(r"\b(?:por que|para que|que pasa si|en que se diferencian|como)\b")
_COMMON_WORDS = frozenset(
    "a al cual cuanto cuantos cuando como con de del dias el en es esta este esto hay la las los "
    "me para por que se sobre su sus tan tiene tienen un una y devolucion devoluciones "
    "prueba simulado simulada simulados simuladas ficticio ficticia ficticios ficticias".split()
)


@dataclass(frozen=True)
class CatalogUnit:
    id: str
    version: str
    text: str
    source_document: str
    source_section: str
    language: str
    composition_group: str

    @property
    def render(self) -> str:
        return (
            f"{self.text} [Fuente sintética: {self.source_document} · "
            f"{self.source_section} · v{self.version}]"
        )


def _unit(row) -> CatalogUnit | None:
    if (
        not _ID.fullmatch(row.unit_id)
        or not _VERSION.fullmatch(row.version)
        or not _SOURCE.fullmatch(row.source_document)
        or not _SECTION.fullmatch(row.source_section)
        or not _SECTION.fullmatch(row.composition_group)
        or row.language != "es"
        or not row.qualifiers
        or not all(qualifier.strip() and qualifier in row.approved_text for qualifier in row.qualifiers)
        or not row.approved_text.strip()
        or len(row.approved_text) > 800
    ):
        return None
    return CatalogUnit(
        row.unit_id, row.version, row.approved_text, row.source_document,
        row.source_section, row.language, row.composition_group,
    )


def _published(client_id: str):
    return select(catalog).where(
        catalog.c.client_id == client_id,
        catalog.c.status == "published",
        (catalog.c.valid_until.is_(None) | (catalog.c.valid_until > func.clock_timestamp())),
    )


def authorized_unit(
    db: Session, client_id: str, unit_id: str, version: str, *, lock: bool = False,
) -> CatalogUnit | None:
    """Revalidate the persisted publication grant and lock it through commit."""
    statement = _published(client_id).where(
        catalog.c.unit_id == unit_id, catalog.c.version == version,
    )
    row = db.execute(statement.with_for_update(read=True) if lock else statement).one_or_none()
    return _unit(row) if row is not None else None


def _topic_rows(db: Session, client_id: str, question: str):
    # Permit narrowly classifiable, explicit queries in the catalog's language.
    # Unknown terms, synthesis or open-ended queries use natural generation.
    folded = "".join(c for c in unicodedata.normalize("NFD", question.lower())
                     if not unicodedata.combining(c))
    if (
        len(folded) > 180 or folded.count("?") > 1
        or not _QUERY.fullmatch(folded) or not _QA_INTENT.search(folded)
        or _SYNTHESIS.search(folded)
    ):
        return ()
    words = frozenset(_WORDS.findall(folded))
    if not words:
        return ()
    rows = db.execute(_published(client_id).order_by(catalog.c.unit_id)).all()
    aliases = frozenset(alias for row in rows for alias in row.topic_aliases)
    if not words <= _COMMON_WORDS | aliases:
        return ()
    return tuple(row for row in rows if words.intersection(row.topic_aliases))


def catalog_anchor_query(db: Session, client_id: str, question: str) -> str | None:
    """Resolve a narrowly matched QA intent to an independent source search."""
    rows = _topic_rows(db, client_id, question)
    anchors = {(row.anchor_document, row.anchor_section) for row in rows}
    if len(anchors) != 1:
        return None
    document, section = anchors.pop()
    if not _SECTION.fullmatch(document) or not _SECTION.fullmatch(section):
        return None
    return f"TrackFlow {document.replace('-', ' ')}: {section.replace('-', ' ')}"


def eligible_units(
    db: Session, client_id: str, question: str, evidence: tuple[RetrievalResult, ...],
) -> tuple[CatalogUnit, ...]:
    anchors = {
        (item.chunk.source_document, item.chunk.section, sha256(item.chunk.text.encode()).hexdigest())
        for item in evidence if item.retrieval_kind == "primary" and item.score >= 0.57
    }
    if not anchors:
        return ()
    rows = _topic_rows(db, client_id, question)
    if any((row.anchor_document, row.anchor_section, row.anchor_sha256) not in anchors for row in rows):
        return ()
    matches = [_unit(row) for row in rows]
    if not matches or any(unit is None for unit in matches):
        return ()
    plan = tuple(unit for unit in matches if unit is not None)
    if len(plan) > 5 or len({unit.id for unit in plan}) != len(plan):
        return ()
    if len({unit.composition_group for unit in plan}) != 1:
        return ()
    return plan


_COVERAGE_ALIASES: dict[CoverageTopic, frozenset[str]] = {
    "return_window": frozenset({"plazo", "limite", "ventana"}),
    # This topic is cost responsibility. A price quote or a numerical amount
    # is a different claim the fixture cannot answer.
    "return_cost": frozenset({"coste", "costo", "costes", "costos"}),
    "return_inspection": frozenset({"inspeccion", "inspecciones"}),
}


@dataclass(frozen=True)
class CoveragePlan:
    """A bounded QA claim mapping. Evidence and grants remain private."""

    requested: tuple[CoverageTopic, ...] | None
    evidence_topics: frozenset[CoverageTopic] = frozenset()
    authorized_topics: frozenset[CoverageTopic] = frozenset()
    units: tuple[CatalogUnit, ...] = ()
    unit_topics: tuple[tuple[str, CoverageTopic], ...] = ()
    required: tuple[str, ...] = ()


def _requested_coverage(question: str) -> CoveragePlan | None:
    # A neutral taxonomy establishes intent independently of catalog access.
    folded = "".join(c for c in unicodedata.normalize("NFD", question.lower())
                     if not unicodedata.combining(c))
    words = frozenset(_WORDS.findall(folded))
    if (
        not words.intersection({"devolucion", "devoluciones"})
        or not _QA_INTENT.search(folded)
        or _SYNTHESIS.search(folded)
    ):
        return None
    accepted_words = _COMMON_WORDS.union(*_COVERAGE_ALIASES.values())
    if (
        len(folded) > 180 or folded.count("?") > 1
        or not _QUERY.fullmatch(folded) or not words <= accepted_words
    ):
        return CoveragePlan(requested=None)
    requested: list[CoverageTopic] = []
    for clause in re.split(r",|\by\b", folded):
        clause_words = frozenset(_WORDS.findall(clause))
        topics = [topic for topic, aliases in _COVERAGE_ALIASES.items() if clause_words & aliases]
        if len(topics) != 1 or (
            topics[0] != "return_window" and clause_words.intersection({"cuanto", "cuantos", "cuando", "dias"})
        ):
            return CoveragePlan(requested=None)
        if topics[0] not in requested:
            requested.append(topics[0])
    return CoveragePlan(requested=tuple(requested))


def _row_coverage_topic(row) -> CoverageTopic | None:
    topics = [
        topic for topic, aliases in _COVERAGE_ALIASES.items()
        if aliases.intersection(row.topic_aliases)
    ]
    return topics[0] if len(topics) == 1 else None


def coverage_anchor_query(db: Session, client_id: str, question: str) -> str | None:
    """Search at most one source anchor from current grants, without relaxing RAG."""
    intent = _requested_coverage(question)
    if intent is None or intent.requested is None:
        return None
    rows = db.execute(_published(client_id)).all()
    anchors = {
        (row.anchor_document, row.anchor_section) for row in rows
        if _row_coverage_topic(row) in intent.requested and _unit(row) is not None
    }
    if len(anchors) != 1:
        return None
    document, section = anchors.pop()
    if not _SECTION.fullmatch(document) or not _SECTION.fullmatch(section):
        return None
    return f"TrackFlow {document.replace('-', ' ')}: {section.replace('-', ' ')}"


def plan_coverage(
    db: Session, client_id: str, question: str, evidence: tuple[RetrievalResult, ...],
) -> CoveragePlan | None:
    intent = _requested_coverage(question)
    if intent is None or intent.requested is None:
        return intent
    anchors = {
        (item.chunk.source_document, item.chunk.section, sha256(item.chunk.text.encode()).hexdigest())
        for item in evidence if item.retrieval_kind == "primary" and item.score >= 0.57
    }
    if not anchors:
        return intent

    def supports(row) -> bool:
        return (row.anchor_document, row.anchor_section, row.anchor_sha256) in anchors

    # Historical versions can explain lack of authorization internally, but
    # their text is never fetched for this evidence check.
    links = db.execute(select(
        catalog.c.anchor_document, catalog.c.anchor_section, catalog.c.anchor_sha256,
        catalog.c.topic_aliases,
    ).where(catalog.c.client_id == client_id, catalog.c.status.in_(("published", "revoked")))).all()
    evidence_topics = frozenset(
        topic for row in links
        if supports(row) and (topic := _row_coverage_topic(row)) is not None
        and topic in intent.requested
    )
    candidates = tuple(
        (unit, topic) for row in db.execute(_published(client_id).order_by(catalog.c.unit_id)).all()
        if supports(row) and (topic := _row_coverage_topic(row)) is not None
        and topic in intent.requested and (unit := _unit(row)) is not None
    )
    authorized_topics = frozenset(topic for _, topic in candidates)
    if (
        len(candidates) > 5 or len({unit.id for unit, _ in candidates}) != len(candidates)
        or len({unit.composition_group for unit, _ in candidates}) > 1
    ):
        # Individual grants do not establish that the units combine safely.
        return CoveragePlan(intent.requested, evidence_topics, authorized_topics)
    required = tuple(
        unit.id for unit, topic in candidates if sum(other == topic for _, other in candidates) == 1
    )
    return CoveragePlan(
        intent.requested, evidence_topics, authorized_topics,
        tuple(unit for unit, _ in candidates),
        tuple((unit.id, topic) for unit, topic in candidates),
        required,
    )
