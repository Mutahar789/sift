FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive

# texlive subset for latexdiff + pdflatex on a typical CS/USENIX paper.
RUN apt-get update && apt-get install -y --no-install-recommends \
        texlive-latex-base \
        texlive-latex-recommended \
        texlive-latex-extra \
        texlive-fonts-recommended \
        texlive-bibtex-extra \
        texlive-science \
        texlive-extra-utils \
        latexdiff \
        biber \
        perl \
        ca-certificates \
        curl \
        xz-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

EXPOSE 8080

CMD streamlit run app.py \
        --server.port=${PORT:-8080} \
        --server.address=0.0.0.0 \
        --server.headless=true \
        --browser.gatherUsageStats=false
