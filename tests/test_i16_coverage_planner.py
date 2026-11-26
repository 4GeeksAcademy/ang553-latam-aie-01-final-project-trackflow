from hashlib import sha256

import pytest
from sqlmodel import Session

from services.api.chat.qa_unit_catalog import catalog, coverage_anchor_query, plan_coverage
from services.rag.contracts import KnowledgeChunk, RetrievalResult
from tests.test_websocket_chat_postgres import chat_db  # noqa: F401 - pytest fixture


CLIENT = "coverage-tenant-a"
OTHER = "coverage-tenant-b"
ANCHOR = KnowledgeChunk("test-point", "returns-policy", "return-window-and-costs", 0, "A QA anchor")
EVIDENCE = (RetrievalResult(ANCHOR, 0.63),)
COMPOUND = "¿Cuál es el plazo y el coste de devolución de prueba?"


def _row(unit_id, aliases, *, status="published", group="retornos-qa"):
    return {
        "unit_id": unit_id,
        "version": "1.0",
        "client_id": CLIENT,
        "language": "es",
        "source_document": "qa-i16-devoluciones",
        "source_section": unit_id.lower(),
        "approved_text": "Información sintética solo en QA.",
        "anchor_document": ANCHOR.source_document,
        "anchor_section": ANCHOR.section,
        "anchor_sha256": sha256(ANCHOR.text.encode()).hexdigest(),
        "topic_aliases": aliases,
        "qualifiers": ["solo en QA"],
        "composition_group": group,
        "status": status,
    }


@pytest.mark.parametrize("question", [
    "¿Cuál es el plazo o el costo de devolución de prueba?",
    "¿Cuál es el plazo del coste de devolución de prueba?",
    "¿Cuál es el plazo y algún dato de devolución simulada?",
    "¿Cuál es la información de devolución de prueba?",
    "¿Cuál es el precio de devolución de prueba?",
    "¿Cuánto es el coste de devolución de prueba?",
])
def test_ambiguous_qa_intent_cannot_resolve_or_lookup_a_catalog(question):
    assert plan_coverage(None, CLIENT, question, EVIDENCE).requested is None
    assert coverage_anchor_query(None, CLIENT, question) is None


@pytest.mark.parametrize("question", [
    "¿Cuál es el plazo de devolución?",
    "¿Por qué hay un plazo de devolución de prueba?",
    "¿Cuál es el precio de importación de prueba?",
])
def test_conventional_synthesis_and_outside_domain_remain_natural(question):
    assert plan_coverage(None, CLIENT, question, EVIDENCE) is None
    assert coverage_anchor_query(None, CLIENT, question) is None


def test_topics_evidence_and_authorization_are_distinct_in_postgres(chat_db):
    catalog.create(chat_db)
    with Session(chat_db) as db:
        db.execute(catalog.insert(), [
            _row("DEV-PLAZO", ["plazo", "limite"]),
            _row("DEV-COSTE", ["coste", "costo"], status="revoked"),
            dict(_row("DEV-INSPECCION", ["inspeccion"]), client_id=OTHER),
        ])
        db.commit()
        query = "¿Cuál es el plazo, el costo y la inspección de devolución de prueba?"
        report = plan_coverage(db, CLIENT, query, EVIDENCE)
        assert report.requested == ("return_window", "return_cost", "return_inspection")
        assert report.evidence_topics == {"return_window", "return_cost"}
        assert report.authorized_topics == {"return_window"}
        assert [unit.id for unit in report.units] == ["DEV-PLAZO"]
        assert report.unit_topics == (("DEV-PLAZO", "return_window"),)
        assert report.required == ("DEV-PLAZO",)
        assert coverage_anchor_query(db, CLIENT, COMPOUND) == "TrackFlow returns policy: return window and costs"
        assert coverage_anchor_query(db, CLIENT, "¿Cuál es el coste de devolución de prueba?") is None

        other = plan_coverage(db, OTHER, COMPOUND, EVIDENCE)
        assert other.requested == ("return_window", "return_cost")
        assert not (other.authorized_topics or other.evidence_topics or other.units)
        assert coverage_anchor_query(db, OTHER, COMPOUND) is None

        for unqualified in (
            (RetrievalResult(ANCHOR, 0.5699),),
            (RetrievalResult(ANCHOR, 0.9, "complementary"),),
            (RetrievalResult(KnowledgeChunk(
                ANCHOR.point_id, ANCHOR.source_document, ANCHOR.section, 0, "Different revision",
            ), 0.9),),
        ):
            missing = plan_coverage(db, CLIENT, COMPOUND, unqualified)
            assert missing.requested == ("return_window", "return_cost")
            assert not (missing.evidence_topics or missing.authorized_topics or missing.units)


def test_alternatives_are_candidates_without_becoming_required(chat_db):
    catalog.create(chat_db)
    with Session(chat_db) as db:
        db.execute(catalog.insert(), [
            _row("DEV-PLAZO", ["plazo", "limite"]),
            _row("DEV-PLAZO-ALTERNATIVO", ["ventana"]),
            _row("DEV-COSTE", ["coste", "costo"]),
        ])
        db.commit()
        report = plan_coverage(db, CLIENT, COMPOUND, EVIDENCE)
        assert report.requested == ("return_window", "return_cost")
        assert report.evidence_topics == report.authorized_topics == {"return_window", "return_cost"}
        assert [unit.id for unit in report.units] == ["DEV-COSTE", "DEV-PLAZO", "DEV-PLAZO-ALTERNATIVO"]
        assert report.required == ("DEV-COSTE",)

        db.execute(catalog.update().where(catalog.c.unit_id == "DEV-COSTE").values(composition_group="otros"))
        db.commit()
        incompatible = plan_coverage(db, CLIENT, COMPOUND, EVIDENCE)
        assert incompatible.authorized_topics == {"return_window", "return_cost"}
        assert not (incompatible.units or incompatible.required)
