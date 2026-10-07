# SQLite 3.7.0 through 3.51.2 can corrupt a WAL database when connections in different
# processes write or checkpoint at the same instant (https://sqlite.org/wal.html#walresetbug),
# and web and worker are such processes. Debian trixie, under python:3.12-slim, ships 3.46.1,
# so the image builds its own library from the checksummed release.
FROM python:3.12-slim AS sqlite

ARG SQLITE_YEAR=2026
ARG SQLITE_VERSION=3530400
ARG SQLITE_SHA3_256=454e45f61c6bd75b7420e7190732dea03ce6639c63ada47bbc592f67fc340338

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
RUN curl -fsSLO "https://sqlite.org/${SQLITE_YEAR}/sqlite-autoconf-${SQLITE_VERSION}.tar.gz" \
    && python -c "import hashlib, sys; digest = hashlib.sha3_256(open(sys.argv[1], 'rb').read()).hexdigest(); sys.exit(0 if digest == sys.argv[2] else 'SQLite source checksum mismatch: ' + digest)" \
        "sqlite-autoconf-${SQLITE_VERSION}.tar.gz" "${SQLITE_SHA3_256}" \
    && tar xzf "sqlite-autoconf-${SQLITE_VERSION}.tar.gz" \
    && cd "sqlite-autoconf-${SQLITE_VERSION}" \
    && CFLAGS="-O2 -DSQLITE_SECURE_DELETE -DSQLITE_ENABLE_COLUMN_METADATA -DSQLITE_ENABLE_UNLOCK_NOTIFY -DSQLITE_LIKE_DOESNT_MATCH_BLOBS -DSQLITE_MAX_VARIABLE_NUMBER=250000" \
        ./configure --prefix=/usr/local --disable-static --disable-readline --fts5 --fts3 --rtree --dbstat \
    && make -j"$(nproc)" \
    && make install

FROM python:3.12-slim

ARG APP_VERSION=unknown

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_VERSION=${APP_VERSION}

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*

# /usr/local/lib is searched before Debian's library directories; the build fails here unless
# Python's sqlite3 module really loads the fixed library with FTS5 trigram search.
COPY --from=sqlite /usr/local/lib/libsqlite3.so* /usr/local/lib/
RUN ldconfig \
    && python -c "import sqlite3; v = sqlite3.sqlite_version_info; assert v >= (3, 51, 3), sqlite3.sqlite_version; c = sqlite3.connect(':memory:'); c.execute(\"CREATE VIRTUAL TABLE t USING fts5(x, tokenize='trigram')\"); print('SQLite', sqlite3.sqlite_version)"

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p /app/data/blobs /app/data/backups

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/live', timeout=4)" || exit 1

CMD ["python", "cli.py", "serve"]
