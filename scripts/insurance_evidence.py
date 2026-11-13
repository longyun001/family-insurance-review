#!/usr/bin/env python3
"""Local insurance evidence index. No network access; no automatic coverage decisions."""
from __future__ import annotations

import argparse
from collections import defaultdict
from contextlib import contextmanager
from datetime import date, datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import re
import sqlite3
import sys
import unicodedata

VERSION = "1.1.0"
SCHEMA_VERSION = 1
PDF_BACKENDS = ("auto", "pymupdf", "pypdf")
SCOPES = {"current", "historical", "reference"}
KINDS = {"policy", "report", "product", "note", "transcript"}
INVISIBLE = "\u200b\u200c\u200d\ufeff\u00ad"
SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
 sha256 TEXT PRIMARY KEY, page_count INTEGER NOT NULL,
 extractor TEXT NOT NULL, indexed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sources (
 source_id TEXT PRIMARY KEY, document_hash TEXT NOT NULL REFERENCES documents(sha256),
 metadata TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS extraction_diagnostics (
 document_hash TEXT PRIMARY KEY REFERENCES documents(sha256), details TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pages (
 id INTEGER PRIMARY KEY, document_hash TEXT NOT NULL REFERENCES documents(sha256),
 page_no INTEGER NOT NULL, text TEXT NOT NULL, quality TEXT NOT NULL,
 UNIQUE(document_hash, page_no)
);
CREATE VIRTUAL TABLE IF NOT EXISTS page_fts USING fts5(tokens);
CREATE TABLE IF NOT EXISTS source_events (
 id INTEGER PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(source_id),
 at TEXT NOT NULL, before_json TEXT NOT NULL, after_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS facts (
 fact_id TEXT PRIMARY KEY, original_json TEXT NOT NULL, current_json TEXT NOT NULL,
 status TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS review_events (
 id INTEGER PRIMARY KEY, fact_id TEXT NOT NULL REFERENCES facts(fact_id),
 revision INTEGER NOT NULL, at TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
 reason TEXT NOT NULL, before_json TEXT NOT NULL, after_json TEXT NOT NULL,
 UNIQUE(fact_id, revision)
);
"""


class EvidenceError(Exception):
    pass


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def identifier(value, name="id"):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", value):
        raise EvidenceError(f"invalid_{name}")
    return value


def canonical_with_positions(text):
    """Remove layout gaps only, preserving punctuation, numbers and negation."""
    chars, positions = [], []
    for index, char in enumerate(text):
        for normalized in unicodedata.normalize("NFKC", char):
            if not normalized.isspace() and normalized not in INVISIBLE:
                chars.append(normalized)
                positions.append(index)
    return "".join(chars), positions


def canonical(text):
    return canonical_with_positions(text)[0]


def tokens(text):
    text = unicodedata.normalize("NFKC", text).lower()
    text = re.sub(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])", "", text)
    result = []
    for part in re.findall(r"[\u3400-\u9fff]+|[a-z0-9]+(?:\.[0-9]+)?%?", text):
        if re.fullmatch(r"[\u3400-\u9fff]+", part) and len(part) > 1:
            result.extend(part[i:i + 2] for i in range(len(part) - 1))
        else:
            result.append(part)
    return result


@contextmanager
def database(path, *, write=False, create=False):
    path = Path(path).resolve()
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    elif not path.is_file():
        raise EvidenceError("database_missing: run index first")
    mode = "rwc" if create else ("rw" if write else "ro")
    conn = sqlite3.connect(path.as_uri() + f"?mode={mode}", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, SCHEMA_VERSION):
            raise EvidenceError("unsupported_schema_version")
        if create:
            conn.executescript(SCHEMA)
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            conn.commit()
        elif version != SCHEMA_VERSION:
            raise EvidenceError("not_an_evidence_database")
        yield conn
    finally:
        conn.close()


def source_row(conn, source_id):
    row = conn.execute("SELECT * FROM sources WHERE source_id=?", (source_id,)).fetchone()
    if row is None:
        raise EvidenceError(f"source_missing:{source_id}")
    return row


def freshness(row, cache=None):
    metadata = json.loads(row["metadata"])
    key = (metadata["path"], row["document_hash"])
    if cache is not None and key in cache:
        return cache[key]
    try:
        state = "ok" if digest(metadata["path"]) == row["document_hash"] else "source_changed"
    except OSError:
        state = "source_missing"
    if cache is not None:
        cache[key] = state
    return state


def text_anomalies(text):
    """Count undecoded text; never remove unknown glyphs or join their neighbours."""
    counts = defaultdict(int)
    for char in text:
        point = ord(char)
        if char == "\ufffd":
            counts["replacement_characters"] += 1
        elif 0xFDD0 <= point <= 0xFDEF or (point & 0xFFFF) in (0xFFFE, 0xFFFF):
            counts["noncharacters"] += 1
        elif unicodedata.category(char) == "Co":
            counts["private_use_characters"] += 1
        elif unicodedata.category(char) == "Cc" and not char.isspace():
            counts["control_characters"] += 1
    return dict(counts)


def extraction_diagnostics(pages, backend, warnings):
    return {
        "warning_count": len(warnings),
        "warning_details": warnings,
        "xref_recovery_warnings": sum(
            any(word in w.lower() for word in ("wrong pointing object", "xref", "repair"))
            for w in warnings),
        "font_encoding_warning": any(
            any(word in w.lower() for word in ("encoding", "cmap", "font", "glyph", "tounicode"))
            for w in warnings),
        "extractor_backend": backend,
        "raw_text_preserved": True,
        "anomaly_pages": [
            {"page": number, "counts": issues}
            for number, text in enumerate(pages, 1)
            if (issues := text_anomalies(text))
        ],
    }


def _extract_with_pymupdf(path):
    try:
        import pymupdf
    except ImportError as exc:
        raise EvidenceError("missing_pymupdf: install pymupdf or use --pdf-backend pypdf") from exc
    tools = pymupdf.TOOLS
    display_warnings = tools.mupdf_display_warnings()
    display_errors = tools.mupdf_display_errors()
    tools.mupdf_warnings(reset=True)
    doc = None
    try:
        tools.mupdf_display_warnings(False)
        tools.mupdf_display_errors(False)
        doc = pymupdf.open(str(path))
        if doc.is_encrypted and not doc.authenticate(""):
            raise EvidenceError("encrypted_pdf_requires_unlocked_copy")
        # Preserve physical page order and every extracted code point.
        pages = [page.get_text() or "" for page in doc]
    except EvidenceError:
        raise
    except Exception as exc:
        raise EvidenceError(f"pymupdf_extraction_failed:{type(exc).__name__}") from exc
    finally:
        try:
            if doc is not None:
                doc.close()
        finally:
            warnings = [w for w in tools.mupdf_warnings(reset=True).splitlines() if w.strip()]
            tools.mupdf_display_warnings(display_warnings)
            tools.mupdf_display_errors(display_errors)
    if not pages:
        raise EvidenceError("empty_pdf")
    return pages, "pymupdf-" + pymupdf.__version__, extraction_diagnostics(pages, "pymupdf", warnings)


def _extract_with_pypdf(path):
    try:
        import pypdf
    except ImportError as exc:
        raise EvidenceError("missing_pypdf: install pymupdf or pypdf in the selected Python runtime") from exc
    warnings = []

    class Capture(logging.Handler):
        def emit(self, record):
            warnings.append(record.getMessage())

    logger = logging.getLogger("pypdf")
    handler, propagate = Capture(logging.WARNING), logger.propagate
    logger.addHandler(handler)
    logger.propagate = False
    try:
        reader = pypdf.PdfReader(path)
        if reader.is_encrypted and not reader.decrypt(""):
            raise EvidenceError("encrypted_pdf_requires_unlocked_copy")
        pages = [page.extract_text() or "" for page in reader.pages]
    except EvidenceError:
        raise
    except Exception as exc:
        raise EvidenceError(f"pdf_extraction_failed:{type(exc).__name__}") from exc
    finally:
        logger.removeHandler(handler)
        logger.propagate = propagate
    if not pages:
        raise EvidenceError("empty_pdf")
    return pages, "pypdf-" + pypdf.__version__, extraction_diagnostics(pages, "pypdf", warnings)


def extract(path, pdf_backend="auto"):
    path = Path(path)
    if pdf_backend not in PDF_BACKENDS:
        raise EvidenceError(f"invalid_pdf_backend:{pdf_backend}")
    if path.suffix.lower() in (".txt", ".md"):
        return [path.read_text(encoding="utf-8-sig")], "utf-8", {}
    if path.suffix.lower() != ".pdf":
        raise EvidenceError(f"unsupported_file_type:{path.suffix}")
    if pdf_backend == "pypdf":
        return _extract_with_pypdf(path)
    if pdf_backend == "pymupdf":
        return _extract_with_pymupdf(path)
    try:
        return _extract_with_pymupdf(path)
    except EvidenceError as exc:
        if str(exc).startswith("encrypted_pdf"):
            raise
        fallback_reason = str(exc)
    pages, extractor, diagnostics = _extract_with_pypdf(path)
    diagnostics["fallback"] = {"from_backend": "pymupdf", "reason": fallback_reason}
    return pages, extractor, diagnostics


def quality(text):
    flat = canonical(text)
    if not flat:
        return "no_text"
    if len(flat) < 40:
        return "low_text"
    return "needs_visual" if text_anomalies(text) else "text"


def page_quality(text, stored):
    # Never relax an earlier extraction warning; also flag raw anomalies in v1 databases.
    return stored if stored != "text" else quality(text)


def manifest_sources(manifest, db_path):
    raw = read_json(manifest)
    raw = raw.get("sources") if isinstance(raw, dict) else raw
    if not isinstance(raw, list) or not raw:
        raise EvidenceError("manifest_needs_nonempty_sources")
    result, seen = [], set()
    for item in raw:
        if not isinstance(item, dict):
            raise EvidenceError("source_must_be_object")
        source_id = identifier(item.get("id"), "source_id")
        if source_id in seen:
            raise EvidenceError(f"duplicate_source_id:{source_id}")
        seen.add(source_id)
        path = Path(item["path"])
        if not path.is_absolute():
            path = Path(manifest).resolve().parent / path
        path = path.resolve()
        if path == Path(db_path).resolve():
            raise EvidenceError("source_cannot_be_database")
        if not path.is_file():
            raise EvidenceError(f"file_missing:{source_id}")
        if path.suffix.lower() not in (".pdf", ".md", ".txt"):
            raise EvidenceError(f"unsupported_file_type:{source_id}")
        scope, kind = item.get("scope"), item.get("kind")
        if scope not in SCOPES or kind not in KINDS:
            raise EvidenceError(f"explicit_scope_and_kind_required:{source_id}")
        policies = item.get("policy_ids")
        if not isinstance(policies, list) or not all(isinstance(p, str) and p for p in policies):
            raise EvidenceError(f"policy_ids_must_be_list:{source_id}")
        meta = {
            "id": source_id, "path": str(path), "title": item.get("title") or path.name,
            "scope": scope, "kind": kind, "policy_ids": sorted(set(policies)),
        }
        for key in ("observed_on", "effective_from", "effective_to"):
            value = item.get(key)
            if value is not None:
                if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
                    raise EvidenceError(f"invalid_date:{source_id}:{key}")
                meta[key] = value
        if meta.get("effective_from") and meta.get("effective_to"):
            if meta["effective_from"] > meta["effective_to"]:
                raise EvidenceError(f"invalid_date_range:{source_id}")
        result.append((meta, digest(path)))
    return result


def index_sources(db_path, manifest, pdf_backend="auto"):
    if pdf_backend not in PDF_BACKENDS:
        raise EvidenceError(f"invalid_pdf_backend:{pdf_backend}")
    entries = manifest_sources(manifest, db_path)
    warnings, added, reused = [], 0, 0
    with database(db_path, write=True, create=True) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        for meta, sha in entries:
            previous = conn.execute("SELECT * FROM sources WHERE source_id=?", (meta["id"],)).fetchone()
            if previous and previous["document_hash"] != sha:
                raise EvidenceError(f"source_content_changed_use_new_id:{meta['id']}")
            exists = conn.execute("SELECT 1 FROM documents WHERE sha256=?", (sha,)).fetchone()
            if exists:
                reused += 1
            else:
                texts, extractor, diagnostics = extract(Path(meta["path"]), pdf_backend)
                if digest(meta["path"]) != sha:
                    raise EvidenceError(f"source_changed_during_extraction:{meta['id']}")
                conn.execute("INSERT INTO documents VALUES (?,?,?,?)", (sha, len(texts), extractor, timestamp()))
                conn.execute("INSERT INTO extraction_diagnostics VALUES (?,?)", (sha, dump(diagnostics)))
                for number, text in enumerate(texts, 1):
                    state = quality(text)
                    if state == "text" and diagnostics.get("font_encoding_warning"):
                        state = "needs_visual"
                    cursor = conn.execute(
                        "INSERT INTO pages(document_hash,page_no,text,quality) VALUES (?,?,?,?)",
                        (sha, number, text, state),
                    )
                    conn.execute("INSERT INTO page_fts(rowid,tokens) VALUES (?,?)",
                                 (cursor.lastrowid, " ".join(tokens(text))))
                    if state != "text":
                        warnings.append({"source_id": meta["id"], "page": number, "quality": state})
                added += 1
            serialized = dump(meta)
            if previous is None:
                conn.execute("INSERT INTO sources VALUES (?,?,?)", (meta["id"], sha, serialized))
            elif previous["metadata"] != serialized:
                conn.execute("INSERT INTO source_events(source_id,at,before_json,after_json) VALUES (?,?,?,?)",
                             (meta["id"], timestamp(), previous["metadata"], serialized))
                conn.execute("UPDATE sources SET metadata=? WHERE source_id=?", (serialized, meta["id"]))
        counts = {name: conn.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
                  for name in ("sources", "documents", "pages", "facts")}
        warnings = []
        diagnostics_result = []
        for meta, sha in entries:
            warnings.extend({"source_id": meta["id"], "page": row["page_no"], "quality": state}
                            for row in conn.execute(
                                "SELECT page_no,text,quality FROM pages WHERE document_hash=?", (sha,))
                            if (state := page_quality(row["text"], row["quality"])) != "text")
            diagnostic = conn.execute("SELECT details FROM extraction_diagnostics WHERE document_hash=?", (sha,)).fetchone()
            if diagnostic and json.loads(diagnostic["details"]):
                diagnostics_result.append({"source_id": meta["id"], **json.loads(diagnostic["details"])})
        extractors = {
            row["extractor"]: row["n"]
            for row in conn.execute("SELECT extractor,count(*) AS n FROM documents GROUP BY extractor")
        }
    return {"status": "indexed", "new_documents": added, "reused_documents": reused,
            "counts": counts, "warnings": warnings, "extraction_diagnostics": diagnostics_result,
            "requested_pdf_backend": pdf_backend, "extractors": extractors,
            "existing_extractions_preserved": bool(reused), "version": VERSION}


def page_data(conn, source_id, page_no, cache=None):
    row = source_row(conn, source_id)
    state = freshness(row, cache)
    if state != "ok":
        raise EvidenceError(f"{state}:{source_id}")
    page = conn.execute("SELECT * FROM pages WHERE document_hash=? AND page_no=?",
                        (row["document_hash"], page_no)).fetchone()
    if page is None:
        raise EvidenceError(f"page_missing:{source_id}:{page_no}")
    meta = json.loads(row["metadata"])
    extractor = conn.execute("SELECT extractor FROM documents WHERE sha256=?",
                             (row["document_hash"],)).fetchone()[0]
    return {"source_id": source_id, "sha256": row["document_hash"], "page": page_no,
            "metadata": meta, "quality": page_quality(page["text"], page["quality"]),
            "extractor": extractor, "text_anomalies": text_anomalies(page["text"]), "text": page["text"]}


def verify_quote(conn, source_id, page_no, quote, cache=None):
    page = page_data(conn, source_id, page_no, cache)
    wanted = canonical(quote)
    result = {"valid": False, "source_id": source_id, "page": page_no, "sha256": page["sha256"],
              "semantic_support": "not_checked", "quality": page["quality"]}
    if len(wanted) < 6:
        return {**result, "reason": "quote_too_short"}
    text, positions = canonical_with_positions(page["text"])
    at = text.find(wanted)
    if at < 0:
        return {**result, "reason": "not_a_contiguous_quote"}
    start, end = positions[at], positions[at + len(wanted) - 1] + 1
    return {**result, "valid": True, "match": "exact" if quote in page["text"] else "layout_normalized",
            "original_excerpt": page["text"][start:end],
            "requires_visual_review": page["quality"] != "text"}


def snippet(text, terms, width=220):
    flat, positions = canonical_with_positions(text)
    matches = [flat.lower().find(canonical(term).lower()) for term in terms]
    matches = [at for at in matches if at >= 0]
    at = positions[min(matches)] if matches and positions else 0
    start = max(0, at - 40)
    return ("…" if start else "") + text[start:start + width] + ("…" if start + width < len(text) else "")


def search(conn, query, policy=None, source=None, scope="current", limit=8):
    terms = list(dict.fromkeys(tokens(query)))[:40]
    if not terms:
        return {"status": "no_match", "results": [], "retrieval_only": True}
    expression = " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)
    filters, params = [], [expression]
    if scope != "all":
        filters.append("json_extract(s.metadata,'$.scope')=?")
        params.append(scope)
    if source:
        filters.append("s.source_id=?")
        params.append(source)
    if policy:
        filters.append("EXISTS(SELECT 1 FROM json_each(s.metadata,'$.policy_ids') WHERE value=?)")
        params.append(policy)
    where = " AND " + " AND ".join(filters) if filters else ""
    rows = conn.execute(
        "SELECT p.*,s.source_id,s.metadata,bm25(page_fts) AS rank "
        "FROM page_fts JOIN pages p ON p.id=page_fts.rowid "
        "JOIN sources s ON s.document_hash=p.document_hash "
        "WHERE page_fts MATCH ?" + where + " ORDER BY rank LIMIT 2000", params,
    ).fetchall()
    found, blocked, cache = [], {}, {}
    minimum = min(2, len(terms))
    for row in rows:
        haystack = canonical(row["text"]).lower()
        overlap = sum(canonical(t).lower() in haystack for t in terms)
        if overlap < minimum:
            continue
        state = freshness(row, cache)
        if state != "ok":
            blocked[row["source_id"]] = state
            continue
        found.append((overlap, row["rank"], {
            "source_id": row["source_id"], "sha256": row["document_hash"],
            "page": row["page_no"], "metadata": json.loads(row["metadata"]),
            "quality": page_quality(row["text"], row["quality"]), "snippet": snippet(row["text"], terms),
            "matched_tokens": overlap,
        }))
    found.sort(key=lambda item: (-item[0], item[1], item[2]["source_id"], item[2]["page"]))
    return {"status": "matches" if found else "no_match", "retrieval_only": True,
            "scope": scope, "results": [item[2] for item in found[:max(1, min(limit, 30))]],
            "blocked_sources": blocked}


def normalized_fact(conn, raw):
    if not isinstance(raw, dict):
        raise EvidenceError("fact_must_be_object")
    fact_id = identifier(raw.get("id"), "fact_id")
    for key in ("policy_id", "field", "period"):
        if not isinstance(raw.get(key), str) or not raw[key].strip():
            raise EvidenceError(f"fact_missing_{key}:{fact_id}")
    if not isinstance(raw.get("value"), (str, int, float, bool)):
        raise EvidenceError(f"fact_needs_scalar_value:{fact_id}")
    cite = raw.get("citation")
    if not isinstance(cite, dict) or not isinstance(cite.get("quote"), str):
        raise EvidenceError(f"fact_needs_citation:{fact_id}")
    page = cite.get("page")
    if type(page) is not int or page < 1:
        raise EvidenceError(f"invalid_page:{fact_id}")
    verification = verify_quote(conn, cite.get("source_id"), page, cite["quote"])
    if not verification["valid"]:
        raise EvidenceError(f"invalid_quote:{fact_id}:{verification['reason']}")
    source = source_row(conn, cite["source_id"])
    meta = json.loads(source["metadata"])
    if raw["policy_id"] not in meta["policy_ids"]:
        raise EvidenceError(f"policy_source_mismatch:{fact_id}")
    if cite.get("sha256") and cite["sha256"] != source["document_hash"]:
        raise EvidenceError(f"citation_hash_mismatch:{fact_id}")
    for key in ("unit", "condition"):
        if not isinstance(raw.get(key, ""), str):
            raise EvidenceError(f"invalid_{key}:{fact_id}")
    result = {key: raw[key] for key in ("id", "policy_id", "field", "period", "value")}
    result.update(unit=raw.get("unit", ""), condition=raw.get("condition", ""))
    result["citation"] = {"source_id": cite["source_id"], "page": page, "quote": cite["quote"],
                          "sha256": source["document_hash"]}
    dump(result)
    return result


def add_facts(conn, raw):
    items = raw.get("facts") if isinstance(raw, dict) else raw
    if not isinstance(items, list) or not items:
        raise EvidenceError("nonempty_facts_array_required")
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        items = [normalized_fact(conn, item) for item in items]
        ids = [item["id"] for item in items]
        if len(set(ids)) != len(ids):
            raise EvidenceError("duplicate_fact_ids")
        added = 0
        for item in items:
            content = dump(item)
            existing = conn.execute("SELECT original_json FROM facts WHERE fact_id=?", (item["id"],)).fetchone()
            if existing:
                if existing["original_json"] != content:
                    raise EvidenceError(f"fact_exists_use_review:{item['id']}")
                continue
            conn.execute("INSERT INTO facts VALUES (?,?,?,'needs_review',0,?)",
                         (item["id"], content, content, timestamp()))
            added += 1
    return {"status": "saved", "added": added, "unchanged": len(items) - added,
            "review_status": "needs_review"}


def fact_records(conn, policy=None, scope="current", status=None):
    result = []
    for row in conn.execute("SELECT * FROM facts ORDER BY fact_id"):
        fact = json.loads(row["current_json"])
        if policy and fact["policy_id"] != policy:
            continue
        source = source_row(conn, fact["citation"]["source_id"])
        meta = json.loads(source["metadata"])
        if scope != "all" and meta["scope"] != scope:
            continue
        if status and row["status"] != status:
            continue
        result.append({"fact": fact, "status": row["status"], "revision": row["revision"],
                       "source_scope": meta["scope"], "observed_on": meta.get("observed_on"),
                       "source_integrity": freshness(source), "semantic_support": "not_checked"})
    return result


def conflicts(conn, policy=None, scope="current"):
    groups = defaultdict(list)
    for record in fact_records(conn, policy, scope):
        if record["status"] == "ignored" or record["source_integrity"] != "ok":
            continue
        fact = record["fact"]
        key = (fact["policy_id"], fact["field"], fact["condition"], fact["period"])
        groups[key].append(record)
    differences = []
    for key, records in groups.items():
        values = {dump([r["fact"]["value"], r["fact"]["unit"]]) for r in records}
        if len(values) > 1:
            differences.append({"policy_id": key[0], "field": key[1], "condition": key[2],
                                "period": key[3], "candidates": records})
    return {"status": "candidate_differences", "not_a_contract_decision": True, "groups": differences}


def review(conn, fact_id, revision, action, actor, reason, replacement=None):
    if not reason.strip():
        raise EvidenceError("review_reason_required")
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM facts WHERE fact_id=?", (fact_id,)).fetchone()
        if row is None:
            raise EvidenceError(f"fact_missing:{fact_id}")
        if row["revision"] != revision:
            raise EvidenceError(f"revision_conflict:expected_{row['revision']}")
        current = json.loads(row["current_json"])
        updated = current
        if action == "correct":
            if replacement is None:
                raise EvidenceError("correction_needs_replacement")
            updated = normalized_fact(conn, replacement)
            if updated["id"] != fact_id or updated["policy_id"] != current["policy_id"]:
                raise EvidenceError("correction_cannot_change_identity")
        elif replacement is not None:
            raise EvidenceError("replacement_only_for_correction")
        if action == "confirm":
            normalized_fact(conn, current)
        state = {"confirm": "confirmed", "correct": "confirmed",
                 "ignore": "ignored", "reopen": "needs_review"}[action]
        before = dump({"fact": current, "status": row["status"]})
        after = dump({"fact": updated, "status": state})
        conn.execute("INSERT INTO review_events(fact_id,revision,at,actor,action,reason,before_json,after_json) "
                     "VALUES (?,?,?,?,?,?,?,?)",
                     (fact_id, revision + 1, timestamp(), actor, action, reason, before, after))
        conn.execute("UPDATE facts SET current_json=?,status=?,revision=? WHERE fact_id=?",
                     (dump(updated), state, revision + 1, fact_id))
    return {"status": state, "fact_id": fact_id, "revision": revision + 1}


def audit(conn, fact_id):
    row = conn.execute("SELECT * FROM facts WHERE fact_id=?", (fact_id,)).fetchone()
    if row is None:
        raise EvidenceError(f"fact_missing:{fact_id}")
    events = []
    for event in conn.execute("SELECT * FROM review_events WHERE fact_id=? ORDER BY revision", (fact_id,)):
        item = dict(event)
        item["before"] = json.loads(item.pop("before_json"))
        item["after"] = json.loads(item.pop("after_json"))
        events.append(item)
    return {"original": json.loads(row["original_json"]), "current": json.loads(row["current_json"]),
            "status": row["status"], "revision": row["revision"], "events": events}


def check(conn):
    cache, problems = {}, []
    source_count = 0
    for row in conn.execute("SELECT * FROM sources ORDER BY source_id"):
        source_count += 1
        state = freshness(row, cache)
        if state != "ok":
            problems.append({"source_id": row["source_id"], "reason": state})
    facts = 0
    for row in conn.execute("SELECT * FROM facts"):
        facts += 1
        try:
            normalized_fact(conn, json.loads(row["current_json"]))
        except (EvidenceError, ValueError) as exc:
            problems.append({"fact_id": row["fact_id"], "reason": str(exc)})
    missing = conn.execute("SELECT count(*) FROM pages p LEFT JOIN page_fts f ON f.rowid=p.id "
                           "WHERE f.rowid IS NULL").fetchone()[0]
    orphaned = conn.execute("SELECT count(*) FROM page_fts f LEFT JOIN pages p ON p.id=f.rowid "
                            "WHERE p.id IS NULL").fetchone()[0]
    if missing or orphaned:
        problems.append({"reason": "index_mismatch", "missing": missing, "orphaned": orphaned})
    integrity = conn.execute("PRAGMA integrity_check").fetchall()
    if [r[0] for r in integrity] != ["ok"]:
        problems.append({"reason": "sqlite_integrity_failure"})
    if conn.execute("PRAGMA foreign_key_check").fetchall():
        problems.append({"reason": "foreign_key_failure"})
    counts = {name: conn.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
              for name in ("documents", "pages", "source_events", "review_events")}
    low = sum(page_quality(row["text"], row["quality"]) != "text"
              for row in conn.execute("SELECT text,quality FROM pages"))
    return {"ok": not problems, "sources_checked": source_count, "facts_checked": facts,
            "counts": counts, "pages_needing_visual_review": low, "problems": problems}


def parser():
    cli = argparse.ArgumentParser(description=__doc__)
    subs = cli.add_subparsers(dest="command", required=True)
    for name in ("index", "search", "page", "verify", "add-facts", "facts", "conflicts", "review", "audit", "check"):
        sub = subs.add_parser(name)
        sub.add_argument("--db", required=True)
        if name == "index":
            sub.add_argument("--manifest", required=True)
            sub.add_argument("--pdf-backend", choices=PDF_BACKENDS, default="auto")
        if name in ("search", "facts", "conflicts"):
            sub.add_argument("--policy")
            sub.add_argument("--scope", choices=("current", "all"), default="current")
        if name == "search":
            sub.add_argument("--query", required=True)
            sub.add_argument("--source")
            sub.add_argument("--limit", type=int, default=8)
        if name in ("page", "verify"):
            sub.add_argument("--source", required=True)
            sub.add_argument("--page", type=int, required=True)
        if name == "verify":
            sub.add_argument("--quote", required=True)
        if name == "add-facts":
            sub.add_argument("--file", required=True)
        if name == "facts":
            sub.add_argument("--status", choices=("needs_review", "confirmed", "ignored"))
        if name in ("review", "audit"):
            sub.add_argument("--fact", required=True)
        if name == "review":
            sub.add_argument("--revision", type=int, required=True)
            sub.add_argument("--action", choices=("confirm", "correct", "ignore", "reopen"), required=True)
            sub.add_argument("--actor", choices=("assistant", "user"), default="assistant")
            sub.add_argument("--reason", required=True)
            sub.add_argument("--replacement")
    return cli


def run(args):
    if args.command == "index":
        return index_sources(args.db, args.manifest, args.pdf_backend)
    write = args.command in ("add-facts", "review")
    with database(args.db, write=write) as conn:
        if args.command == "search":
            return search(conn, args.query, args.policy, args.source, args.scope, args.limit)
        if args.command == "page":
            return page_data(conn, args.source, args.page)
        if args.command == "verify":
            return verify_quote(conn, args.source, args.page, args.quote)
        if args.command == "add-facts":
            return add_facts(conn, read_json(args.file))
        if args.command == "facts":
            return {"facts": fact_records(conn, args.policy, args.scope, args.status)}
        if args.command == "conflicts":
            return conflicts(conn, args.policy, args.scope)
        if args.command == "review":
            replacement = read_json(args.replacement) if args.replacement else None
            return review(conn, args.fact, args.revision, args.action, args.actor, args.reason, replacement)
        if args.command == "audit":
            return audit(conn, args.fact)
        return check(conn)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = parser().parse_args()
    try:
        result = run(args)
        print(dump(result))
        return 1 if result.get("valid") is False or result.get("ok") is False else 0
    except (EvidenceError, OSError, ValueError, KeyError, sqlite3.Error) as exc:
        print(dump({"status": "error", "error": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
