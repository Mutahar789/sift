"""LaTeX diff: two zips in, diff.pdf + diff.tex out.

Pipeline: extract zips → union-merge assets (old's .sty/.bib survive even if
new dropped them) → harvest OLD's cite/ref numbers via a side compile →
latexdiff --flatten → post-process diff.tex with colors + robust \\DIFdel +
undef-cite/ref fallbacks → pdflatex × 3 + bibtex.
"""
from __future__ import annotations

import io
import os
import re
import shutil
import signal
import stat
import subprocess
import threading
import time
import uuid
import zipfile
from collections import deque
from pathlib import Path
from typing import Callable

ROOT = Path("/tmp") / "slate-diff"
LATEXDIFF_TIMEOUT = 180
PDFLATEX_TIMEOUT = 120
_SEM = threading.Semaphore(2)

ADD_COLORS = ["blue", "teal", "green", "violet"]
DEL_COLORS = ["red", "magenta", "orange", "brown"]


class DiffError(RuntimeError):
    def __init__(self, message, log_tail=""):
        super().__init__(message)
        self.message = message
        self.log_tail = log_tail


# ---------- zip safety + extract ----------

def _is_skippable(name: str) -> bool:
    return (name.startswith("__MACOSX/") or name.startswith(".DS_Store")
            or name.endswith("/.DS_Store") or "/._" in name
            or name.startswith("._"))


def _safe_members(zf: zipfile.ZipFile, dest: Path):
    for info in zf.infolist():
        name = info.filename
        if _is_skippable(name):
            continue
        if name.startswith("/") or (len(name) >= 2 and name[1] == ":"):
            raise DiffError(f"zip rejected: absolute path {name!r}")
        norm = os.path.normpath(name)
        if norm.startswith("..") or "/.." in norm:
            raise DiffError(f"zip rejected: path traversal {name!r}")
        if stat.S_ISLNK(info.external_attr >> 16):
            raise DiffError(f"zip rejected: symlink {name!r}")
        if not str((dest / norm).resolve()).startswith(str(dest.resolve())):
            raise DiffError(f"zip rejected: escapes dest {name!r}")
        yield info


def _extract(zip_bytes: bytes, dest: Path):
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        zf.extractall(dest, members=list(_safe_members(zf, dest)))


def _find_main(root: Path) -> Path | None:
    direct = root / "main.tex"
    if direct.is_file():
        return direct
    subs = [p for p in root.iterdir()
            if p.is_dir() and not p.name.startswith(".")
            and not p.name.startswith("__")]
    if len(subs) == 1 and (subs[0] / "main.tex").is_file():
        return subs[0] / "main.tex"
    matches = sorted(root.rglob("main.tex"), key=lambda p: len(p.parts))
    return matches[0] if matches else None


# ---------- harvest OLD's cite/ref numbers ----------

_RE_BIBCITE = re.compile(r"\\bibcite\{([^}]+)\}\{\{?(\d+)\}?", re.M)
_RE_NEWLABEL = re.compile(r"\\newlabel\{([^}]+)\}\{\{([^}]*)\}\{(\d+)\}", re.M)


def _harvest_old_numbers(old_root: Path, build: Path) -> dict:
    """Pre-compile OLD's main.tex in a scratch dir; harvest cite/ref numbers
    from its .aux file. Used as a fallback in the diff compile so a removed
    reference still renders with its original number."""
    out = {"cites": {}, "refs": {}}
    scratch = build.parent / "old_aux_scratch"
    shutil.rmtree(scratch, ignore_errors=True)
    try:
        shutil.copytree(old_root, scratch, dirs_exist_ok=True, symlinks=False)
    except Exception:
        return out

    if not (scratch / "main.tex").exists():
        candidates = sorted(scratch.rglob("main.tex"), key=lambda p: len(p.parts))
        if not candidates:
            shutil.rmtree(scratch, ignore_errors=True)
            return out
        scratch = candidates[0].parent

    pdflatex = ["pdflatex", "-interaction=nonstopmode", "-halt-on-error",
                 "-file-line-error", "main.tex"]
    env = os.environ.copy()
    env.setdefault("openout_any", "p")
    try:
        for cmd in [pdflatex, ["bibtex", "main"], pdflatex]:
            subprocess.run(cmd, cwd=str(scratch), env=env, timeout=120,
                            capture_output=True)
        aux = (scratch / "main.aux").read_text(encoding="utf-8", errors="replace")
        out["cites"] = {m.group(1): m.group(2) for m in _RE_BIBCITE.finditer(aux)}
        out["refs"]  = {m.group(1): m.group(2) for m in _RE_NEWLABEL.finditer(aux)}
    except Exception:
        pass
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return out


def _format_old_lookup(old_nums: dict) -> str:
    if not old_nums["cites"] and not old_nums["refs"]:
        return ""
    lines = ["", "% Sift: cite/ref numbers harvested from OLD version"]
    for key, num in old_nums["cites"].items():
        lines.append(r"\expandafter\def\csname Sift@OldCite@"
                      + key + r"\endcsname{" + num + "}")
    for label, num in old_nums["refs"].items():
        lines.append(r"\expandafter\def\csname Sift@OldRef@"
                      + label + r"\endcsname{" + num + "}")
    return "\n".join(lines) + "\n"


# ---------- bib merge ----------

_RE_BIB_ENTRY = re.compile(r"@\w+\s*\{\s*([^,\s]+)\s*,", re.I)


def _bib_keys(text: str) -> set[str]:
    return {m.group(1) for m in _RE_BIB_ENTRY.finditer(text)}


def _merge_bibs(old_root: Path, new_root: Path, build: Path):
    """Union OLD + NEW .bib entries so cite keys removed from new still
    resolve (no `[?]` for entries that existed in old)."""
    bib_names = {p.name for r in (old_root, new_root) for p in r.rglob("*.bib")}
    for name in bib_names:
        old_p = next(iter(old_root.rglob(name)), None)
        new_p = next(iter(new_root.rglob(name)), None)
        if not old_p:
            continue
        new_text = new_p.read_text(encoding="utf-8", errors="replace") if new_p else ""
        old_text = old_p.read_text(encoding="utf-8", errors="replace")
        extras = _bib_keys(old_text) - _bib_keys(new_text)
        if not extras:
            continue
        appended = ["\n\n%% Sift: appended from OLD's " + name + "\n"]
        for m in re.finditer(r"(@\w+\s*\{\s*([^,\s]+)\s*,)", old_text, flags=re.I):
            if m.group(2) not in extras:
                continue
            start = m.start()
            depth = 0
            for i, ch in enumerate(old_text[start:], start=start):
                if ch == "{": depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        appended.append(old_text[start:i + 1] + "\n")
                        break
        target = build / name
        if target.exists():
            target.write_text(target.read_text(encoding="utf-8", errors="replace")
                               + "".join(appended), encoding="utf-8")
        else:
            target.write_text(new_text + "".join(appended), encoding="utf-8")


# ---------- diff.tex post-processing ----------

_RE_ADDTEX = re.compile(r"\\providecommand\{\\DIFaddtex\}\[1\]\{[^\n]*", re.M)
_RE_DELTEX = re.compile(r"\\providecommand\{\\DIFdeltex\}\[1\]\{[^\n]*", re.M)
_RE_BEGIN_DOC = re.compile(r"^\s*\\begin\{document\}", re.M)

# Robust \DIFdel + \DIFdelFL via \DeclareRobustCommand (so the \let chain
# survives \protected@edef inside float captions). Inside a deletion,
# \ref / \cite are let to helpers that print OLD's number or ?/??.
_EXTRAS = r"""
\makeatletter
\protected\def\DIFnoarg#1{}
\protected\def\Sift@delref#1{%
  \@ifundefined{Sift@OldRef@#1}{??}{%
    \csname Sift@OldRef@#1\endcsname}}
\protected\def\Sift@delcite#1{%
  \begingroup
    [\let\@citea\@empty
    \@for\Sift@@key:=#1\do{%
      \@citea\def\@citea{,\,}%
      \edef\Sift@@key{\expandafter\@firstofone\Sift@@key\@empty}%
      \@ifundefined{Sift@OldCite@\Sift@@key}{?}{%
        \csname Sift@OldCite@\Sift@@key\endcsname}%
    }]%
  \endgroup
}
\let\DIFoldDIFdel\DIFdel
\DeclareRobustCommand{\DIFdel}[1]{%
  \begingroup
    \let\cite\Sift@delcite \let\nocite\DIFnoarg
    \let\citep\Sift@delcite \let\citet\Sift@delcite
    \let\citeauthor\Sift@delcite \let\citeyear\Sift@delcite
    \let\citeyearpar\Sift@delcite
    \let\Citep\Sift@delcite \let\Citet\Sift@delcite
    \let\citealp\Sift@delcite \let\citealt\Sift@delcite
    \let\ref\Sift@delref \let\Ref\Sift@delref
    \let\cref\Sift@delref \let\Cref\Sift@delref
    \let\autoref\Sift@delref \let\eqref\Sift@delref
    \let\pageref\Sift@delref \let\nameref\Sift@delref
    \let\label\DIFnoarg \let\footnote\DIFnoarg \let\footnotemark\relax
    \DIFoldDIFdel{#1}%
  \endgroup
}
\@ifundefined{DIFdelFL}{}{\let\DIFdelFL\@undefined}
\DeclareRobustCommand{\DIFdelFL}[1]{\DIFdel{#1}}
\makeatother
"""

# Undefined-cite/ref handlers used OUTSIDE deletions. Look up OLD's number
# from the harvested table; fall back to vanilla "?" / "??".
_UNDEF_FALLBACK = r"""
\AtBeginDocument{%
  \makeatletter
  \def\Sift@undefcite#1{%
    \@ifundefined{Sift@OldCite@#1}{\reset@font\bfseries ?}{%
      \csname Sift@OldCite@#1\endcsname}}
  \def\Sift@undefref#1{%
    \@ifundefined{Sift@OldRef@#1}{\reset@font\bfseries ??}{%
      \csname Sift@OldRef@#1\endcsname}}
  \def\@citex[#1]#2{%
    \let\@citea\@empty
    \@cite{\@for\@citeb:=#2\do
      {\@citea\def\@citea{,\penalty\@m\ }%
       \edef\@citeb{\expandafter\@firstofone\@citeb\@empty}%
       \if@filesw\immediate\write\@auxout{\string\citation{\@citeb}}\fi
       \@ifundefined{b@\@citeb}{%
         \mbox{\Sift@undefcite{\@citeb}}%
         \G@refundefinedtrue
         \@latex@warning{Citation `\@citeb' on page \thepage\space undefined}%
       }{\hbox{\csname b@\@citeb\endcsname}}}}{#1}}
  \def\@setref#1#2#3{%
    \ifx#1\relax
      \mbox{\Sift@undefref{#3}}%
      \G@refundefinedtrue
      \@latex@warning{Reference `#3' on page \thepage\space undefined}%
    \else
      \expandafter#2#1\null
    \fi}
  \makeatother
}
"""


def _post_process(tex: str, *, add_color: str, del_color: str,
                   strikethrough: bool,
                   old_numbers: dict | None = None) -> str:
    addtex = (r"\providecommand{\DIFaddtex}[1]{{\protect\color{" + add_color
              + r"}#1}}")
    body = r"\protect\sout{#1}" if strikethrough else r"#1"
    deltex = (r"\providecommand{\DIFdeltex}[1]{{\protect\color{" + del_color
              + r"}" + body + r"}}")

    tex = _RE_ADDTEX.sub(lambda _m: addtex, tex, count=1)
    tex = _RE_DELTEX.sub(lambda _m: deltex, tex, count=1)
    tex = re.sub(r"\\usepackage\{ulem\}",
                  lambda _m: r"\usepackage[normalem]{ulem}", tex, count=1)

    m = _RE_BEGIN_DOC.search(tex)
    if m:
        lookup = _format_old_lookup(old_numbers) if old_numbers else ""
        injection = lookup + _UNDEF_FALLBACK + (_EXTRAS if strikethrough else "")
        tex = tex[:m.start()] + injection + tex[m.start():]
    return tex


# ---------- pdflatex runner ----------

def _run(cmd, cwd: Path, timeout: int, cb, stage: str, tail: deque) -> int:
    env = os.environ.copy()
    env.setdefault("openout_any", "p")
    env.setdefault("openin_any", "p")
    proc = subprocess.Popen(cmd, cwd=str(cwd), env=env, text=True, bufsize=1,
                             start_new_session=True,
                             stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT)

    def reader():
        assert proc.stdout
        for line in iter(proc.stdout.readline, ""):
            tail.append(line.rstrip("\n"))
            _emit(cb, {"kind": "log", "stage": stage, "line": line.rstrip("\n")})
        proc.stdout.close()

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    deadline = time.time() + timeout
    timed_out = False
    while proc.poll() is None:
        if time.time() > deadline:
            timed_out = True
            try: os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError: pass
            break
        time.sleep(0.1)
    proc.wait(timeout=5)
    t.join(timeout=2)
    if timed_out:
        raise DiffError(f"{stage} timed out after {timeout}s.",
                         log_tail="\n".join(list(tail)[-30:]))
    return proc.returncode


def _emit(cb, ev):
    if cb is None:
        return
    try:
        cb(ev)
    except Exception:
        pass


# ---------- main entry ----------

def run_diff(old_zip: bytes, new_zip: bytes, *,
             add_color: str = "blue",
             del_color: str = "red",
             strikethrough: bool = True,
             progress: Callable[[dict], None] | None = None) -> dict:
    if add_color not in ADD_COLORS or del_color not in DEL_COLORS:
        raise ValueError("invalid color")

    workdir = ROOT / f"job-{uuid.uuid4().hex[:10]}"
    old_only = workdir / "_old"
    new_only = workdir / "_new"
    build = workdir / "build"
    for d in (old_only, new_only, build):
        d.mkdir(parents=True, exist_ok=True)

    _emit(progress, {"kind": "stage", "stage": "extract",
                     "message": "extracting zips"})
    _extract(old_zip, old_only)
    _extract(new_zip, new_only)

    old_main = _find_main(old_only)
    new_main = _find_main(new_only)
    if not old_main:
        raise DiffError("Old zip: no main.tex found.")
    if not new_main:
        raise DiffError("New zip: no main.tex found.")

    # Union into build/ at main.tex's level. OLD first so its .sty/.cls/.bib
    # survive even if NEW dropped them; NEW overrides on conflicts.
    shutil.copytree(old_main.parent, build, dirs_exist_ok=True, symlinks=False)
    shutil.copytree(new_main.parent, build, dirs_exist_ok=True, symlinks=False)
    _merge_bibs(old_main.parent, new_main.parent, build)

    _emit(progress, {"kind": "stage", "stage": "harvest_old",
                     "message": "harvesting OLD's cite/ref numbers"})
    old_numbers = _harvest_old_numbers(old_main.parent, build)

    _emit(progress, {"kind": "stage", "stage": "latexdiff",
                     "message": f"diff {old_main.name} (old) → {new_main.name} (new)"})
    proc = subprocess.run(
        ["latexdiff", "--flatten",
         "--exclude-textcmd=section,subsection,subsubsection",
         "--config=PICTUREENV=(?:picture|DIFnomarkup|tabular|tabularx|threeparttable)[\\w\\d*@]*",
         str(old_main.resolve()), str(new_main.resolve())],
        capture_output=True, text=True, timeout=LATEXDIFF_TIMEOUT,
    )
    if proc.returncode != 0:
        raise DiffError("latexdiff failed.", log_tail=(proc.stderr or "")[-1500:])

    diff_tex = build / "diff.tex"

    def _compile(strike: bool) -> tuple[bool, deque]:
        diff_tex.write_text(
            _post_process(proc.stdout, add_color=add_color, del_color=del_color,
                           strikethrough=strike, old_numbers=old_numbers),
            encoding="utf-8")
        for ext in (".aux", ".bbl", ".blg", ".log", ".out", ".toc"):
            (build / f"diff{ext}").unlink(missing_ok=True)
        tail: deque = deque(maxlen=400)
        pdf = ["pdflatex", "-interaction=nonstopmode", "-halt-on-error",
                "-file-line-error", "diff.tex"]
        for stage, cmd, timeout in [
            ("pdflatex-1", pdf, PDFLATEX_TIMEOUT),
            ("bibtex",     ["bibtex", "diff"], 60),
            ("pdflatex-2", pdf, PDFLATEX_TIMEOUT),
            ("pdflatex-3", pdf, PDFLATEX_TIMEOUT),
        ]:
            _emit(progress, {"kind": "stage", "stage": stage,
                             "message": f"running {stage}"})
            rc = _run(cmd, build, timeout, progress, stage, tail)
            if rc != 0:
                if stage == "bibtex":
                    _emit(progress, {"kind": "warning",
                                     "message": "bibtex failed; bibliography may be empty"})
                    continue
                return False, tail
        return True, tail

    # Strikethrough first; on failure retry without (a few package combos
    # can break \sout in odd captions).
    with _SEM:
        ok, tail = _compile(strikethrough)
        if not ok and strikethrough:
            _emit(progress, {"kind": "warning",
                             "message": "compile failed with strikethrough — "
                                         "retrying without"})
            ok, tail = _compile(False)
            if ok:
                strikethrough = False
        if not ok:
            raise DiffError("pdflatex failed even after strikethrough fallback.",
                             log_tail="\n".join(list(tail)[-50:]))

    diff_pdf = build / "diff.pdf"
    if not diff_pdf.is_file():
        raise DiffError("Compile finished but diff.pdf was not produced.",
                         log_tail="\n".join(list(tail)[-40:]))

    return {"diff_pdf": str(diff_pdf), "diff_tex": str(diff_tex),
            "workdir": str(workdir), "strikethrough_used": strikethrough}
