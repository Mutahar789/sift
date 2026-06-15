"""Sift — Streamlit entry. Two tabs: Reference checker, LaTeX diff."""
from __future__ import annotations

import datetime as _dt
import hashlib
import os
import sys
import tempfile
import uuid
from collections import deque
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).parent))

from check_ref import check_pdf, find_acl, find_dblp
from diff import ADD_COLORS, DEL_COLORS, DiffError, run_diff

ROOT = Path(__file__).parent
DB_DIR = ROOT / "databases"

st.set_page_config(page_title="Sift", page_icon="📑",
                   layout="wide", initial_sidebar_state="expanded")


# ---------- sidebar ----------

def _db_status(label: str, path: Path | None) -> str:
    if path is None:
        return f"{label}: ❌ online only"
    mtime = _dt.datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d")
    size_gb = path.stat().st_size / (1024 ** 3)
    return f"{label}: ✅ offline dump from {mtime} ({size_gb:.1f} GB)"


def sidebar():
    with st.sidebar:
        st.title("📑 Sift")
        st.caption("Pre-submission checks for academic papers.")

        st.markdown("### Tools")
        st.markdown("**Reference checker** — flags hallucinated citations and "
                     "wrong author names. Works on any PDF.")
        st.markdown("**LaTeX diff** — colored, strikethrough diff between two "
                     "paper versions. Upload two zips, get a `diff.pdf`.")

        st.markdown("---")
        st.markdown("### Your API keys")
        st.caption("Both are free. Without them, refs are slower and more may "
                    "show up as not_found.")
        s2 = st.text_input("Semantic Scholar API key",
                            value=st.session_state.get("s2_key", ""),
                            help="Free, requires approval. "
                                  "semanticscholar.org/product/api")
        oa = st.text_input("OpenAlex API key",
                            value=st.session_state.get("openalex_key", ""),
                            help="Free, instant. openalex.org/settings/api")
        st.session_state["s2_key"] = s2
        st.session_state["openalex_key"] = oa

        st.caption(_db_status("DBLP", find_dblp()))
        st.caption(_db_status("ACL", find_acl()))
        if find_acl() is None and (DB_DIR / "anthology.bib").exists():
            st.caption("ℹ️ anthology.bib found — build with "
                        "`hallucinator-cli update-acl databases/acl.db`.")

        st.markdown("---")
        st.markdown("**Inspired from:**")
        st.markdown(
            "- [hallucinator](https://github.com/gianlucasb/hallucinator) — "
            "reference validation\n"
            "- [git-latexdiff-web](https://github.com/am009/git-latexdiff-web) "
            "and [latexdiff.cn](https://latexdiff.cn/) — robust LaTeX diff"
        )


sidebar()


# ---------- shared ----------

def reset(prefix: str):
    for k in [k for k in st.session_state if k.startswith(prefix)]:
        del st.session_state[k]


# ---------- tab: reference checker ----------

def refcheck_tab():
    st.header("Reference checker")
    st.caption("Upload a PDF. Sift extracts every reference, validates it "
                "against academic databases, applies a strict author audit, "
                "and cross-checks DBLP gaps against OpenAlex and Semantic Scholar.")

    pdf = st.file_uploader("Paper PDF", type=["pdf"], key="rc-up")

    if pdf is not None:
        fp = hashlib.sha1(pdf.getvalue()[:1 << 20]).hexdigest()[:12]
        if st.session_state.get("rc-file-fp") != fp:
            reset("rc-job-")
            st.session_state["rc-file-fp"] = fp

    run = st.button("Run check", type="primary",
                     disabled=pdf is None, key="rc-run")

    if pdf is None:
        st.info("Drop a PDF above to start.")
        return

    if run:
        reset("rc-job-")
        st.session_state["rc-job-id"] = uuid.uuid4().hex
        st.session_state["rc-job-status"] = "running"
        st.session_state["rc-file-fp"] = fp

    if (st.session_state.get("rc-job-status") != "running"
            and "rc-job-summary" not in st.session_state):
        return

    if st.session_state.get("rc-job-status") == "running":
        tmp = Path(tempfile.gettempdir()) / f"sift-rc-{uuid.uuid4().hex[:8]}.pdf"
        tmp.write_bytes(pdf.getvalue())

        with st.status("Validating references…", expanded=True) as status:
            head = st.empty()
            log_box = st.empty()
            log: deque[str] = deque(maxlen=80)
            counts = {"verified": 0, "not_found": 0, "cross_checks": 0}

            def render_log():
                if log:
                    log_box.code("\n".join(log), language=None)

            def cb(ev):
                k = ev.get("kind")
                if k == "extracted":
                    log.append(f"extracted {ev['n_refs']} refs")
                    head.markdown(f"**Extracted** {ev['n_refs']} references.")
                elif k == "checking":
                    head.markdown(
                        f"**Pass {ev['pass']}** · `{ev['index']}/{ev['total']}` "
                        f"— *{ev['title'][:80]}*")
                elif k == "result":
                    s = ev.get("status", "")
                    counts[s] = counts.get(s, 0) + 1
                    if s == "verified":
                        log.append(f"[pass {ev['pass']}] {ev['index']}/{ev['total']} "
                                    f"verified via {ev.get('source','?')}")
                    elif s == "not_found":
                        log.append(f"[pass {ev['pass']}] {ev['index']}/{ev['total']} "
                                    f"not found")
                elif k == "rate_limit":
                    log.append(f"rate-limit wait on {ev['db_name']}")
                elif k == "cross_check":
                    counts["cross_checks"] += 1
                    log.append(f"cross-check ({', '.join(ev.get('sources', []))}) "
                                f"for '{ev['title'][:60]}'")
                render_log()

            try:
                summary = check_pdf(
                    tmp,
                    s2_key=st.session_state.get("s2_key", ""),
                    openalex_key=st.session_state.get("openalex_key", ""),
                    progress=cb,
                    use_cache=False,
                )
                st.session_state["rc-job-summary"] = summary
                st.session_state["rc-job-status"] = "done"
                status.update(
                    label=(f"Done — verified: {counts.get('verified',0)} · "
                            f"not_found: {counts.get('not_found',0)} · "
                            f"cross-checks: {counts.get('cross_checks',0)}"),
                    state="complete", expanded=False)
            except Exception as e:
                st.session_state["rc-job-error"] = str(e)
                st.session_state["rc-job-status"] = "error"
                status.update(label="Failed.", state="error", expanded=True)
            finally:
                tmp.unlink(missing_ok=True)

    if st.session_state.get("rc-job-status") == "error":
        st.error(f"❌ {st.session_state['rc-job-error']}")
        return

    summary = st.session_state.get("rc-job-summary")
    if not summary:
        return

    counts = summary["counts"]
    cols = st.columns(5)
    cols[0].metric("Total", summary["total"])
    cols[1].metric("Verified", counts.get("verified", 0))
    cols[2].metric("Issues", counts.get("has_issues", 0))
    cols[3].metric("Not found", counts.get("not_found", 0))
    cols[4].metric("URL refs", counts.get("url_ref", 0))

    by_status = summary["by_status"]
    if by_status.get("has_issues"):
        with st.expander(f"⚠️ Issues found ({len(by_status['has_issues'])})",
                          expanded=True):
            for v in by_status["has_issues"]:
                _render(v, show_issues=True)
    if by_status.get("not_found"):
        with st.expander(f"❌ Not found ({len(by_status['not_found'])}) — "
                          "potential hallucinations", expanded=True):
            st.caption("Manually verify each on Google Scholar before action.")
            for v in by_status["not_found"]:
                _render(v)
    if by_status.get("url_ref"):
        with st.expander(f"🌐 URL references ({len(by_status['url_ref'])})"):
            st.caption("Vendor/news links — verified by liveness, not authors.")
            for v in by_status["url_ref"]:
                _render(v, compact=True)
    if by_status.get("verified"):
        with st.expander(f"✅ Verified ({len(by_status['verified'])})"):
            for v in by_status["verified"]:
                _render(v, compact=True)
    if by_status.get("parser_artifact"):
        with st.expander(f"🔧 Parser artifacts ({len(by_status['parser_artifact'])}) "
                          "— extraction issues, ignore"):
            for v in by_status["parser_artifact"]:
                _render(v, compact=True)


def _render(v, *, show_issues: bool = False, compact: bool = False):
    title = v.title or "(no title)"
    if v.paper_url:
        st.markdown(f"**{title}** · [{v.source or 'link'}]({v.paper_url})")
    else:
        st.markdown(f"**{title}** · {v.source or '—'}")
    if compact:
        return
    if v.cited_authors or v.found_authors:
        c1, c2 = st.columns(2)
        c1.markdown("*Cited authors*")
        c1.write(v.cited_authors or ["(none)"])
        c2.markdown("*DB authors*")
        c2.write(v.found_authors or ["(none)"])
    if show_issues:
        for issue in v.issues:
            st.markdown(f"- `{issue['code']}` — {issue['detail']}")
    st.divider()


# ---------- tab: latex diff ----------

def diff_tab():
    st.header("LaTeX diff")
    st.caption("Upload two `.zip` files, each containing a `main.tex`. Sift "
                "runs `latexdiff --flatten`, harvests OLD's cite/ref numbers, "
                "and compiles with `pdflatex` + `bibtex`. Files present only "
                "in old (e.g. older `.sty`) are kept so compile succeeds.")

    c1, c2 = st.columns(2)
    old_zip = c1.file_uploader("Old (.zip)", type=["zip"], key="dx-old")
    new_zip = c2.file_uploader("New (.zip)", type=["zip"], key="dx-new")

    c1, c2, c3 = st.columns(3)
    add_color = c1.selectbox("Addition color", ADD_COLORS, key="dx-add")
    del_color = c2.selectbox("Deletion color", DEL_COLORS, key="dx-del")
    strike = c3.toggle("Strikethrough deletions", value=True, key="dx-strike")

    run = st.button("Run diff", type="primary",
                     disabled=(old_zip is None or new_zip is None),
                     key="dx-run")

    if old_zip is None or new_zip is None:
        st.info("Upload both zips to enable diffing.")
        return

    if run:
        reset("dx-job-")
        st.session_state["dx-job-id"] = uuid.uuid4().hex
        st.session_state["dx-job-status"] = "running"
        st.session_state["dx-job-old"] = old_zip.getvalue()
        st.session_state["dx-job-new"] = new_zip.getvalue()
        st.session_state["dx-job-opts"] = (add_color, del_color, strike)

    if st.session_state.get("dx-job-status") not in {"running", "done", "error"}:
        return

    if st.session_state.get("dx-job-status") == "running":
        opts = st.session_state["dx-job-opts"]
        with st.status("Building diff…", expanded=True) as status:
            stage_box = st.empty()
            log_lines: list[str] = []

            def cb(ev):
                k = ev.get("kind")
                if k == "stage":
                    stage_box.markdown(f"**{ev['stage']}** — "
                                        f"{ev.get('message','')}")
                elif k == "log":
                    log_lines.append(ev["line"])
                elif k == "warning":
                    log_lines.append(f"[warning] {ev['message']}")

            try:
                result = run_diff(
                    st.session_state["dx-job-old"],
                    st.session_state["dx-job-new"],
                    add_color=opts[0], del_color=opts[1],
                    strikethrough=opts[2],
                    progress=cb,
                )
                st.session_state["dx-job-result"] = result
                st.session_state["dx-job-status"] = "done"
                st.session_state["dx-job-log"] = log_lines
                status.update(label="Diff ready.", state="complete",
                                expanded=False)
            except DiffError as e:
                st.session_state["dx-job-error"] = e
                st.session_state["dx-job-log"] = log_lines
                st.session_state["dx-job-status"] = "error"
                status.update(label="Failed.", state="error", expanded=True)

    if st.session_state.get("dx-job-status") == "error":
        e = st.session_state["dx-job-error"]
        st.error(f"❌ {e.message}")
        if e.log_tail:
            with st.expander("Log tail"):
                st.code(e.log_tail, language="text")
        return

    if st.session_state.get("dx-job-status") == "done":
        result = st.session_state["dx-job-result"]
        pdf_bytes = result["pdf_bytes"]
        tex_bytes = result["tex_bytes"]

        st.success(f"diff.pdf ready ({len(pdf_bytes)/1024:.0f} KB).")

        c1, c2 = st.columns(2)
        c1.download_button("Download diff.pdf", pdf_bytes,
                            file_name="diff.pdf", mime="application/pdf",
                            use_container_width=True)
        c2.download_button("Download diff.tex", tex_bytes,
                            file_name="diff.tex", mime="text/x-tex",
                            use_container_width=True)

        st.markdown("---")
        _preview(pdf_bytes)


def _preview(pdf_bytes: bytes, max_pages: int = 25, scale: float = 1.4):
    try:
        import pypdfium2 as pdfium
    except ImportError:
        st.info("Install `pypdfium2` to enable in-page preview "
                 "(or download above).")
        return
    try:
        doc = pdfium.PdfDocument(pdf_bytes)
    except Exception as e:
        st.warning(f"Could not open PDF for preview: {e}")
        return
    n_pages = len(doc)
    show_n = min(n_pages, max_pages)
    st.caption(f"Preview — first {show_n} of {n_pages} pages.")
    for i in range(show_n):
        st.image(doc[i].render(scale=scale).to_pil(),
                 caption=f"page {i+1}", use_container_width=True)


# ---------- main ----------

tab1, tab2 = st.tabs(["📄 Reference checker", "🔀 LaTeX diff"])
with tab1:
    refcheck_tab()
with tab2:
    diff_tab()
