"""Reference checker: extract PDF refs, validate, audit author lists.

Each ref gets one verdict:
    verified         — found in a DB, every cited author matches.
    has_issues       — found but with wrong/extra cited authors.
    url_ref          — verified by URL liveness only (blog, vendor doc, news).
    not_found        — looks like a paper but no DB knows it.
    parser_artifact  — title is an extraction artifact ("Accessed: ...").
"""
from __future__ import annotations

import json
import os
import re
import unicodedata
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from hallucinator import PdfExtractor, Validator, ValidatorConfig
from rapidfuzz import fuzz


# ---------- name normalization ----------

_SURNAME_PREFIXES = {"van", "von", "de", "del", "della", "di", "da", "al",
                     "el", "la", "le", "ben", "ibn", "mac", "mc", "o"}
_NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}
_ORG_TOKENS = {"openai", "meta", "google", "deepmind", "anthropic", "microsoft",
               "alibaba", "baidu", "tencent", "nvidia", "apple", "darpa",
               "ftc", "nasa", "nist", "ieee", "acm", "who", "oecd", "unesco"}


def _normalize_name(name: str) -> str:
    s = unicodedata.normalize("NFD", name)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = re.sub(r"\(.*?\)", " ", s)
    for ch in ",.'’":
        s = s.replace(ch, " ")
    # Any non-ASCII punctuation (incl. Unicode hyphens) → separator
    s = re.sub(r"[^A-Za-z0-9\s]", " ", s)
    return s.lower()


def _split_name(name: str) -> tuple[set[str], str | None, set[str]]:
    """(first_tokens, surname, first_initials). Handles "First Last", "F. L.",
    and comma "Last, First" formats."""
    if not name:
        return set(), None, set()

    if "," in name:
        before, after = name.split(",", 1)
        before_parts = [p for p in _normalize_name(before).split() if p]
        after_parts = [p for p in _normalize_name(after).split() if p]
        if before_parts and after_parts:
            last = next((t for t in reversed(before_parts) if len(t) >= 2), None)
            firsts = {p for p in after_parts if len(p) >= 2
                       and p not in _NAME_SUFFIXES and p not in _SURNAME_PREFIXES}
            initials = {p[0] for p in after_parts if p}
            if last:
                return firsts, last, initials

    parts = [p for p in _normalize_name(name).split()
             if p and p not in _NAME_SUFFIXES and p not in _SURNAME_PREFIXES]
    if not parts:
        return set(), None, set()
    last_idx = len(parts) - 1
    while last_idx > 0 and len(parts[last_idx]) < 2:
        last_idx -= 1
    last = parts[last_idx] if len(parts[last_idx]) >= 2 else None
    firsts_all = parts[:last_idx]
    firsts = {p for p in firsts_all if len(p) >= 2}
    initials = {p[0] for p in firsts_all if p}
    return firsts, last, initials


def _is_org(name: str) -> bool:
    n = unicodedata.normalize("NFD", name).strip().lower()
    n = "".join(c for c in n if unicodedata.category(c) != "Mn")
    return (n in _ORG_TOKENS or n.replace("-", "") in _ORG_TOKENS
            or n.endswith(" team"))


def _name_tokens(name: str) -> set[str]:
    return {t for t in _normalize_name(name).split()
            if len(t) >= 2 and t not in _NAME_SUFFIXES
            and t not in _SURNAME_PREFIXES}


def _name_match(ref_name: str, found_names: list[str]) -> bool:
    """Two authors match iff raw strings agree OR (surname identical AND
    first names are compatible). Conservative — distinct first names with
    the same surname (Gyudong Ko vs Gihyuk Ko) do NOT match."""
    r_raw = (ref_name or "").strip().lower()
    if r_raw and any(r_raw == (fn or "").strip().lower() for fn in found_names):
        return True

    r_firsts, r_last, r_initials = _split_name(ref_name)
    if r_last is None:
        return False
    for fn in found_names:
        f_firsts, f_last, f_initials = _split_name(fn)
        if f_last is None or r_last != f_last:
            continue
        if not r_firsts and not f_firsts:
            if not r_initials or not f_initials or (r_initials & f_initials):
                return True
            continue
        if not r_firsts or not f_firsts:
            full = r_firsts or f_firsts
            inits = r_initials or f_initials
            if any(ff[0] in inits for ff in full):
                return True
            continue
        if r_firsts & f_firsts:
            return True
    return False


# ---------- audit ----------

_PARSE_ARTIFACT_PATTERNS = [
    re.compile(r"^accessed[\s:]+", re.I),
    re.compile(r"^generative ai usage", re.I),
    re.compile(r"^retrieved[\s:]+", re.I),
    re.compile(r"^see also", re.I),
    re.compile(r"^\[?https?://", re.I),
]
_URL_SOURCES = {"URL Check", "Wayback Machine"}


def _looks_like_paper(ref) -> bool:
    title = getattr(ref, "title", "") or ""
    if len(title.split()) < 5:
        return False
    authors = list(getattr(ref, "ref_authors", []) or [])
    return any(a and not _is_org(a) for a in authors)


def audit_result(r, raw_citation: str = "") -> list[dict]:
    issues = []
    title = (getattr(r, "title", "") or "").strip()

    if title:
        for pat in _PARSE_ARTIFACT_PATTERNS:
            if pat.search(title):
                issues.append({"code": "PARSE_ARTIFACT",
                               "detail": "title looks like an extraction artifact"})
                break

    # Status "mismatch" still gets audited — hallucinator flags benign DOI
    # variants as mismatch, but cited authors may be fine; we have final say.
    if r.status not in ("verified", "mismatch"):
        return issues

    ref_authors = [a for a in (r.ref_authors or []) if a and a.strip()]
    found_authors = [a for a in (r.found_authors or []) if a and a.strip()]
    if (not ref_authors or not found_authors
            or (r.source or "") in _URL_SOURCES
            or any(_is_org(a) for a in ref_authors + found_authors)):
        return issues

    unmatched = [i for i, a in enumerate(ref_authors)
                  if not _name_match(a, found_authors)]
    if unmatched:
        issues.append({"code": "AUTHOR_NOT_IN_DB",
                       "detail": f"cited authors absent from DB: "
                                  f"{[ref_authors[i] for i in unmatched]}"})

    for info_attr, code, label in [("doi_info", "DOI_TITLE_MISMATCH", "DOI"),
                                     ("arxiv_info", "ARXIV_TITLE_MISMATCH", "arXiv")]:
        info = getattr(r, info_attr, None)
        info_title = getattr(info, "title", "") if info else ""
        if info_title and title:
            score = fuzz.token_set_ratio(title.lower(), info_title.lower())
            if score < 70:
                issues.append({"code": code,
                               "detail": f"{label} title differs from cited title (fuzz {score})"})

    return issues


# ---------- verdict ----------

@dataclass
class Verdict:
    title: str
    cited_authors: list[str]
    found_authors: list[str]
    source: str
    paper_url: str
    status: str
    issues: list[dict] = field(default_factory=list)
    failed_dbs: list[str] = field(default_factory=list)
    raw_citation: str = ""


def _verdict_from_result(r, raw_citation: str) -> Verdict:
    issues = audit_result(r, raw_citation=raw_citation)
    src = r.source or ""

    if any(i["code"] == "PARSE_ARTIFACT" for i in issues):
        final = "parser_artifact"
    elif r.status in ("verified", "mismatch"):
        if src in _URL_SOURCES:
            final = "url_ref"
        elif issues:
            final = "has_issues"
        else:
            final = "verified"
    else:
        final = "not_found"

    return Verdict(
        title=r.title or "",
        cited_authors=list(r.ref_authors or []),
        found_authors=list(r.found_authors or []),
        source=src,
        paper_url=r.paper_url or "",
        status=final,
        issues=issues,
        failed_dbs=list(r.failed_dbs or []),
        raw_citation=raw_citation,
    )


# ---------- validator config ----------

ALL_DBS = ["CrossRef", "arXiv", "DBLP", "Semantic Scholar", "ACL Anthology",
           "Europe PMC", "PubMed", "DOI", "OpenAlex", "Open Library",
           "GovInfo", "IACR ePrint", "Standards"]

_PROJECT_DB_DIR = Path(__file__).parent / "databases"
_HOME_DB_DIR = Path.home() / ".local/share/hallucinator"
_VOLUME_DB_DIR = Path("/data")  # Railway persistent volume mount


def find_dblp() -> Path | None:
    for p in (_VOLUME_DB_DIR / "dblp.db",
              _PROJECT_DB_DIR / "dblp.db",
              _HOME_DB_DIR / "dblp.db"):
        if p.exists():
            return p
    return None


def find_acl() -> Path | None:
    for p in (_VOLUME_DB_DIR / "acl.db",
              _PROJECT_DB_DIR / "acl.db",
              _HOME_DB_DIR / "acl.db"):
        if p.exists():
            return p
    return None


def _cfg(*, disable, s2_key, openalex_key, dblp_path, acl_path,
         crossref_mailto, db_timeout):
    cfg = ValidatorConfig()
    cfg.num_workers = 4
    cfg.db_timeout_secs = db_timeout
    cfg.db_timeout_short_secs = max(8, db_timeout // 2)
    cfg.max_rate_limit_retries = 8
    cfg.crossref_mailto = crossref_mailto or "slate@example.org"
    cfg.url_match = True
    cfg.openalex_key = openalex_key or ""
    cfg.s2_api_key = s2_key or ""
    if dblp_path and Path(dblp_path).exists():
        cfg.dblp_offline_path = str(dblp_path)
    if acl_path and Path(acl_path).exists():
        cfg.acl_offline_path = str(acl_path)
    cfg.disabled_dbs = list(disable)
    return cfg


# ---------- cross-check (second-opinion) ----------

def _http_json(url: str, *, timeout: int = 10, headers: dict | None = None) -> dict | None:
    try:
        req = urllib.request.Request(url, headers=headers or {})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def _openalex_authors(title: str, *, key: str = "") -> list[str]:
    if not title or len(title) < 12:
        return []
    params = {"search": title, "per-page": 3}
    if key:
        params["api_key"] = key
    data = _http_json("https://api.openalex.org/works?"
                       + urllib.parse.urlencode(params),
                       headers={"User-Agent": "Sift/1.0"})
    if not data:
        return []
    for w in data.get("results", []):
        if fuzz.token_set_ratio((w.get("title") or "").lower(), title.lower()) >= 75:
            return [a.get("author", {}).get("display_name", "")
                    for a in (w.get("authorships") or []) if a.get("author")]
    return []


def _s2_authors(title: str, *, key: str = "") -> list[str]:
    if not title or len(title) < 12:
        return []
    headers = {"User-Agent": "Sift/1.0"}
    if key:
        headers["x-api-key"] = key
    data = _http_json(
        "https://api.semanticscholar.org/graph/v1/paper/search?"
        + urllib.parse.urlencode({"query": title, "limit": 3,
                                   "fields": "title,authors"}),
        headers=headers)
    if not data:
        return []
    for p in data.get("data", []):
        if fuzz.token_set_ratio((p.get("title") or "").lower(), title.lower()) >= 75:
            return [a.get("name", "") for a in (p.get("authors") or []) if a.get("name")]
    return []


def _merge_authors(found: list[str], extra: list[str]) -> list[str]:
    if not extra:
        return found
    found_tok = [_name_tokens(a) for a in found]
    out = list(found)
    for name in extra:
        tok = _name_tokens(name)
        if tok and not any(tok & ft for ft in found_tok):
            out.append(name)
            found_tok.append(tok)
    return out


def _cross_check_enrich(title: str, found_authors: list[str], *,
                         openalex_key: str, s2_key: str,
                         cb=None) -> tuple[list[str], list[str]]:
    sources = []
    enriched = list(found_authors)

    oa = _openalex_authors(title, key=openalex_key)
    if oa:
        before = len(enriched)
        enriched = _merge_authors(enriched, oa)
        if len(enriched) > before or set(enriched) != set(found_authors):
            sources.append("OpenAlex")
    if s2_key:
        s2 = _s2_authors(title, key=s2_key)
        if s2:
            before = len(enriched)
            enriched = _merge_authors(enriched, s2)
            if len(enriched) > before:
                sources.append("Semantic Scholar")
    if cb and sources:
        cb({"kind": "cross_check", "title": title[:80], "sources": sources})
    return enriched, sources


# ---------- main entry ----------

CACHE_DIR = Path(__file__).parent / ".cache"


def _content_hash(path: Path) -> str:
    import hashlib
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def check_pdf(pdf_path: str | Path,
              *,
              s2_key: str = "",
              openalex_key: str = "",
              dblp_path: str | Path | None = None,
              acl_path: str | Path | None = None,
              crossref_mailto: str = "",
              db_timeout: int = 30,
              progress: Callable[[dict], None] | None = None,
              use_cache: bool = True) -> dict:
    """Two-pass validate + strict author audit + cross-check enrichment."""
    pdf_path = Path(pdf_path)
    if dblp_path is None:
        dblp_path = find_dblp()
    if acl_path is None:
        acl_path = find_acl()

    cache_file = None
    if use_cache:
        CACHE_DIR.mkdir(exist_ok=True)
        cache_file = CACHE_DIR / f"{_content_hash(pdf_path)}.json"
        if cache_file.exists():
            with open(cache_file) as f:
                payload = json.load(f)
            _emit(progress, {"kind": "cache_hit", "path": str(cache_file)})
            return _from_cache(payload)

    ext = PdfExtractor()
    refs = list(ext.extract(str(pdf_path)).references)
    _emit(progress, {"kind": "extracted", "n_refs": len(refs)})

    # Pass 1: all DBs except Semantic Scholar (it's rate-limited).
    cfg1 = _cfg(disable=["Semantic Scholar"], s2_key=s2_key,
                openalex_key=openalex_key, dblp_path=dblp_path,
                acl_path=acl_path, crossref_mailto=crossref_mailto,
                db_timeout=db_timeout)
    results = list(Validator(cfg1).check(refs, progress=_rust_cb(progress, 1)))

    # Pass 2: only Semantic Scholar, only on not-founds.
    not_found = [(i, ref) for i, (ref, r) in enumerate(zip(refs, results))
                 if r.status == "not_found"]
    if s2_key and not_found:
        cfg2 = _cfg(disable=[d for d in ALL_DBS if d.lower() != "semantic scholar"],
                    s2_key=s2_key, openalex_key=openalex_key,
                    dblp_path=dblp_path, acl_path=acl_path,
                    crossref_mailto=crossref_mailto, db_timeout=db_timeout + 5)
        retry = list(Validator(cfg2).check([r for _, r in not_found],
                                            progress=_rust_cb(progress, 2)))
        by_title = {r.title: r for r in retry}
        for idx, ref in not_found:
            new = by_title.get(ref.title)
            if new and new.status != "not_found":
                results[idx] = new

    verdicts = []
    for ref, r in zip(refs, results):
        rc = getattr(r, "raw_citation", None) or getattr(ref, "raw_citation", None) or ""
        v = _verdict_from_result(r, rc)

        # Cross-check: if author audit fires, see if OpenAlex/S2 fills gaps.
        if v.status == "has_issues" and any(
                i["code"] in {"AUTHOR_NOT_IN_DB", "AUTHOR_MISSING_COAUTHOR"}
                for i in v.issues):
            enriched, sources = _cross_check_enrich(
                v.title, v.found_authors,
                openalex_key=openalex_key, s2_key=s2_key, cb=progress)
            if sources:
                class _S: pass
                s = _S()
                for a in ("title", "ref_authors", "source", "status",
                          "doi_info", "arxiv_info", "paper_url"):
                    setattr(s, a, getattr(r, a, None))
                s.found_authors = enriched
                v.found_authors = enriched
                v.issues = audit_result(s, raw_citation=rc)
                if not v.issues:
                    v.status = "verified"

        if v.status == "not_found":
            urls = list(getattr(ref, "urls", []) or [])
            if urls and not _looks_like_paper(v):
                v.status = "url_ref"
                if not v.paper_url:
                    v.paper_url = urls[0]
        verdicts.append(v)

    summary = _build_summary(str(pdf_path), verdicts)
    if cache_file is not None:
        with open(cache_file, "w") as f:
            json.dump(_to_cache(summary), f, indent=2)
    _emit(progress, {"kind": "done", "summary": _summary_counts(summary)})
    return summary


# ---------- helpers ----------

def _emit(progress, ev):
    if progress is None:
        return
    try:
        progress(ev)
    except Exception:
        pass


def _rust_cb(progress, pass_num):
    def cb(ev):
        et = ev.event_type
        if et == "checking":
            _emit(progress, {"kind": "checking", "pass": pass_num,
                             "index": ev.index, "total": ev.total,
                             "title": ev.title})
        elif et == "result":
            r = ev.result
            _emit(progress, {"kind": "result", "pass": pass_num,
                             "index": ev.index, "total": ev.total,
                             "status": r.status, "source": r.source or ""})
        elif et == "rate_limit_wait":
            _emit(progress, {"kind": "rate_limit", "db_name": ev.db_name})
    return cb


def _build_summary(pdf_path, verdicts: list[Verdict]) -> dict:
    by_status: dict[str, list[Verdict]] = {}
    for v in verdicts:
        by_status.setdefault(v.status, []).append(v)
    return {
        "pdf": pdf_path,
        "total": len(verdicts),
        "counts": {k: len(vs) for k, vs in by_status.items()},
        "verdicts": verdicts,
        "by_status": by_status,
    }


def _summary_counts(summary) -> dict:
    return {"total": summary["total"], "counts": summary["counts"]}


def _to_cache(summary) -> dict:
    return {"pdf": summary["pdf"],
            "verdicts": [v.__dict__ for v in summary["verdicts"]]}


def _from_cache(payload) -> dict:
    verdicts = [Verdict(**v) for v in payload["verdicts"]]
    return _build_summary(payload["pdf"], verdicts)
