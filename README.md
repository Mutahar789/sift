# Sift

Two pre-submission checks for academic papers, in one Streamlit app:

- **Reference checker** — flags hallucinated citations and wrong author names
  in any PDF.
- **LaTeX diff** — colored, strikethrough diff between two `.zip` versions of
  a paper, with cite/ref numbers preserved from the old version.

## Run locally

```bash
pip install -r requirements.txt
OPENALEX_KEY=... S2_API_KEY=... streamlit run app.py
```

Both keys are optional but raise rate limits and unlock cross-checking.

## Offline databases (recommended)

Drop `dblp.db` and/or `acl.db` into `databases/`. Sift auto-detects them.
Without them, queries hit the public APIs and are slower / rate-limited.

```bash
curl -sSf https://hallucinator.science/install-cli.sh | sh
hallucinator-cli update-dblp databases/dblp.db   # ~2.5 GB, 20–30 min
hallucinator-cli update-acl  databases/acl.db    # ~60 MB
```

## Layout

```
slate/
├── app.py            # Streamlit entry, sidebar, tabs
├── check_ref.py      # extract + validate + audit + cross-check
├── diff.py           # zip extract + latexdiff + pdflatex pipeline
├── databases/        # offline DB dumps
├── requirements.txt
└── README.md
```

## Inspired from

- [hallucinator](https://github.com/gianlucasb/hallucinator) — reference
  validation across academic databases
- [git-latexdiff-web](https://github.com/am009/git-latexdiff-web) and
  [latexdiff.cn](https://latexdiff.cn/) — robust LaTeX diff
