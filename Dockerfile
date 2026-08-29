# The full application — search, sign-in, and sync — as a hosted website.
#
# The static GitHub Pages build cannot sign in to openCaselist: Pages serves
# files only, and openCaselist's session cookie is SameSite=Lax, which a
# browser will not attach to requests from another site. Running the app on a
# real host fixes that, because the login happens in Python, server-side,
# where those cookie rules do not apply — exactly as it does on a laptop.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        libreoffice-writer \
    && rm -rf /var/lib/apt/lists/*
# libreoffice-writer is only for converting legacy .doc uploads (spec §3.4).

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY carddb/ ./carddb/
COPY templates/ ./templates/
COPY static/ ./static/
COPY scripts/ ./scripts/
COPY data/topics.json ./data/topics.json
COPY config.toml ./config.toml
COPY config/ ./config/

# Hosts route to $PORT; 7860 is the Hugging Face Spaces default.
ENV PORT=7860
EXPOSE 7860

# Bind 0.0.0.0 because a container's loopback is not reachable from outside.
# The /connect sign-in page normally refuses non-loopback binds (it takes a
# password); CARDDB_ALLOW_REMOTE_LOGIN is the deliberate opt-in for a
# deployment you control and serve over HTTPS.
ENV CARDDB_ALLOW_REMOTE_LOGIN=0

CMD ["sh", "-c", "python -m carddb serve --host 0.0.0.0 --port ${PORT} --no-open"]
