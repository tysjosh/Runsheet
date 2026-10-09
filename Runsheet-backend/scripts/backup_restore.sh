#!/usr/bin/env bash
#
# Postgres backup / verify / restore for the Runsheet source-of-truth.
#
# Why this is a script and not a paragraph in a runbook: the only reason to have
# a backup is a restore, and a restore procedure nobody has executed is a guess.
# `verify` and `drill` exist so the procedure can be exercised on demand and in
# CI rather than for the first time during an incident.
#
# Usage:
#   backup_restore.sh dump   <DATABASE_URL> [outfile]
#   backup_restore.sh verify <dumpfile>
#   backup_restore.sh restore <DATABASE_URL> <dumpfile>
#   backup_restore.sh drill  <DATABASE_URL> <scratch-db-name>
#
# DATABASE_URL accepts the app's SQLAlchemy form
# (postgresql+psycopg://...) as well as plain postgresql://; the +psycopg
# driver suffix is stripped, because libpq does not understand it and the
# resulting error ("invalid URI scheme") is not obviously about that.
set -euo pipefail

usage() { sed -n '3,20p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }

# ---------------------------------------------------------------------------
# libpq does not accept SQLAlchemy's dialect+driver scheme.
# ---------------------------------------------------------------------------
libpq_url() {
    printf '%s' "$1" | sed -E 's#^postgresql\+[a-z0-9_]+://#postgresql://#'
}

# Every table the app owns, so a count comparison covers the whole schema
# rather than the three tables someone happened to think of.
row_counts() {
    psql "$1" -Atq -c "
        SELECT relname, n_live_tup
        FROM pg_stat_user_tables
        ORDER BY relname;
    "
}

cmd_dump() {
    local url="${1:?DATABASE_URL required}"
    local out="${2:-runsheet-$(date -u +%Y%m%dT%H%M%SZ).dump}"
    url=$(libpq_url "$url")

    # --format=custom so pg_restore can be selective and parallel, and so the
    # dump is compressed. --no-owner because the restore target's role names
    # are not guaranteed to match the source's.
    pg_dump --format=custom --no-owner --file "$out" "$url"
    echo "wrote $out ($(du -h "$out" | cut -f1))"

    # A dump is not a backup until it has been read back. pg_restore --list
    # parses the whole archive's table of contents, so a truncated or corrupt
    # file fails here rather than during the incident.
    cmd_verify "$out" >/dev/null
    echo "verified: archive table-of-contents is readable"
}

cmd_verify() {
    local dump="${1:?dumpfile required}"
    [ -s "$dump" ] || { echo "❌ $dump is empty or missing"; exit 1; }
    local entries
    entries=$(pg_restore --list "$dump" | grep -cE '^[0-9]+;' || true)
    if [ "${entries:-0}" -lt 1 ]; then
        echo "❌ $dump contains no restorable entries"
        exit 1
    fi
    echo "$dump: $entries restorable entries"
}

cmd_restore() {
    local url="${1:?DATABASE_URL required}"
    local dump="${2:?dumpfile required}"
    url=$(libpq_url "$url")
    cmd_verify "$dump" >/dev/null

    # --clean --if-exists drops the objects the dump is about to recreate.
    # This DESTROYS the current contents of the target database.
    echo "⚠️  restoring $dump into $url — existing objects will be dropped"
    pg_restore --clean --if-exists --no-owner --dbname "$url" "$dump"
    echo "restored. row counts now:"
    row_counts "$url"
}

# ---------------------------------------------------------------------------
# drill — prove a restore works, without touching the live database.
#
# Dumps the source, restores into a scratch database, compares per-table row
# counts, then drops the scratch. This is the step that turns "we have backups"
# into a claim with evidence behind it.
# ---------------------------------------------------------------------------
cmd_drill() {
    local url="${1:?DATABASE_URL required}"
    local scratch="${2:?scratch database name required}"
    url=$(libpq_url "$url")

    local admin_url="${url%/*}/postgres"
    local scratch_url="${url%/*}/$scratch"
    local dump
    dump=$(mktemp -u)/drill.dump
    mkdir -p "$(dirname "$dump")"

    echo "=== 1. dump the source ==="
    pg_dump --format=custom --no-owner --file "$dump" "$url"
    cmd_verify "$dump"

    echo "=== 2. create the scratch database ==="
    psql "$admin_url" -q -c "DROP DATABASE IF EXISTS \"$scratch\";"
    psql "$admin_url" -q -c "CREATE DATABASE \"$scratch\";"

    echo "=== 3. restore into it ==="
    pg_restore --no-owner --dbname "$scratch_url" "$dump" >/dev/null

    echo "=== 4. compare row counts table by table ==="
    # ANALYZE first: n_live_tup comes from the statistics collector and is zero
    # on a freshly restored database until it has been gathered, which would
    # make every table look empty and the drill look like a failure.
    psql "$scratch_url" -q -c "ANALYZE;"
    local src dst
    src=$(row_counts "$url")
    dst=$(row_counts "$scratch_url")

    # A drill over an empty database compares zeros to zeros and proves nothing
    # about data fidelity — it would pass just as happily if pg_restore silently
    # copied no rows. Refuse to report success on a dataset that cannot
    # distinguish those cases.
    local total
    total=$(printf '%s\n' "$src" | awk -F'|' '{ s += $2 } END { print s + 0 }')
    if [ "${total:-0}" -eq 0 ] && [ "${ALLOW_EMPTY_DRILL:-0}" != "1" ]; then
        echo "❌ every table in the source is empty — this drill would compare"
        echo "   zero to zero and tell you nothing. Seed some rows first, or set"
        echo "   ALLOW_EMPTY_DRILL=1 to assert only that the schema round-trips."
        psql "$admin_url" -q -c "DROP DATABASE IF EXISTS \"$scratch\";"
        rm -rf "$(dirname "$dump")"
        return 1
    fi

    if [ "$src" = "$dst" ]; then
        echo "$dst" | sed 's/^/  /'
        echo "✅ restore drill passed — $total row(s) across $(printf '%s\n' "$src" | wc -l | tr -d ' ') table(s) match"
        local rc=0
    else
        echo "❌ row counts differ (source | restored)"
        diff <(echo "$src") <(echo "$dst") || true
        local rc=1
    fi

    echo "=== 5. drop the scratch database ==="
    psql "$admin_url" -q -c "DROP DATABASE IF EXISTS \"$scratch\";"
    rm -rf "$(dirname "$dump")"
    return "$rc"
}

case "${1:-}" in
    dump)    shift; cmd_dump "$@" ;;
    verify)  shift; cmd_verify "$@" ;;
    restore) shift; cmd_restore "$@" ;;
    drill)   shift; cmd_drill "$@" ;;
    *)       usage ;;
esac
