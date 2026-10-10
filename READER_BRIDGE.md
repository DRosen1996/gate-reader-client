# Authenticated read-only ledger bridge

This optional reader extension exposes two GET endpoints after the existing Bearer check. It does not expose the review UI, note writes, SQL, arbitrary paths, or a database download. Existing Gate routes and authentication remain unchanged.

- `/review-summary?limit=20&offset=0` returns server-ledger provenance, sync cutoffs, observed time, completeness, cash categories, cycles, gaps, unallocated items and orphan notes.
- `/review-trade?id=cycle:<64 lowercase hex characters>&limit=20&offset=0` returns a stable cycle with versioned notes, fills, fees, order observations and flows.
- Each list has independent items/total/next_offset; the requested offset applies to each list. Client.query and CLI --id support these routes. Gate Client.pages does not apply to this nested pagination.
- Unknown/duplicate parameters are rejected. limit is 1–100 and offset 0–10000. Responses are no-store and limited to512KiB; a too-large single record is rejected without silently truncating it. Errors contain only fixed messages.

The server uses a fixed container path `/review-data/ledger.sqlite`, SQLite mode=ro, query_only, trusted_schema=OFF and one read transaction per request. It accepts only schema2/DELETE and never calls the writable Ledger initializer or migrations. DDL, ATTACH and writes are denied. No immutable/nolock flags are used.

Capacity limits: regular nonsymlink file at most64MiB, about5million SQLite VM instructions, at most2 concurrent projections per process; source row caps observations5000/evidence10000/sightings20000/notes2000/ranges2000/gaps2000/cycle_aliases5000. Exceeding a limit fails closed, with no partial success response. These limits also bound Python projection work; SQL instruction limits alone do not.

Deployment requires separately verified image layout, running identity and a read-only mount of a dedicated ledger directory. Do not weaken private data permissions or change the reader identity without approval. The extension adds no service, port, credential or collection schedule. Do not publish actual deployment configuration or private acceptance data. Keep the original reader/image for rollback; the bridge does not modify the database.

Synthetic verification:87 tests including missing/wrong/duplicate authentication, write methods, original account route, notes, parameter/ID/pagination limits, file/row/concurrency limits, WAL refusal, schema/missing/locked/corrupt databases, DDL/ATTACH refusal, unchanged hash/mtime, malformed requests and sanitized internal errors. Python compileall, JavaScript syntax and existing browser replay checks also pass locally. No real-server deployment or real-data publication is claimed.


## Optional manual installer

`tools/install_reader_bridge.py` is invoked only after deployment approval, from a pinned clean checkout. It accepts explicit reader/ledger directories, service, approved non-root UID/GID and expected running image ID. It checks the uploaded reader source hash, build layout, permissions, read-only database, loopback port and runtime isolation before creating files. It refuses existing overrides/recovery state; there is no automatic repeat installation.

The installer leaves original app.py/Dockerfile/compose.yaml untouched, backs them up privately, preserves the old image with a local tag, assigns a distinct bridge image tag so the original Compose image tag is not overwritten, and builds from a separate private code-only context. Only app.py and three review modules are copied; no .env or private ledger enters the build context. It then creates compose.override.yaml (JSON is valid YAML) so normal default Compose operations keep the approved user and read-only mount. Explicit `-f compose.yaml` alone omits that override and must not be used after installation.

Build failure leaves the current service/default configuration unchanged. Recreate or bounded unauthenticated health failure rolls back. Private recovery state remains to guard retries; `--rollback <private-state-directory>` uses the tagged old image, removes only the unchanged installer override and recreates only the selected service. Original files remain available in the private backup; override changes require manual review rather than deletion. No secret, account response or full configuration is printed. Authenticated ledger acceptance remains a separate check.


When Docker access requires sudo, authenticate with `sudo -v` in the terminal, then invoke the installer as the ordinary approved owner with `--sudo-docker` (including rollback). Only Docker child commands become `sudo -n docker ...`; Python file operations and database reads remain unprivileged. No sudo password is read, stored or forwarded by the script. A missing/expired sudo ticket fails closed with a sanitized error. Do not run the entire installer under sudo.

## Read-only diagnostic

`python3 -B tools/diagnose-reader-bridge.py` accepts the same required reader/ledger directory, service, UID/GID and expected-image values as installation, plus optional --sudo-docker. It reports only recovery-directory existence and fixed preflight check/error codes. It calls preflight directly, never install/rollback, and additionally allowlists only Docker config/ps/inspect/info reads. No state, backup, configuration or container is changed; bytecode writing is disabled. sudo uses -n and never prompts. Failure is diagnostic evidence, not a request to repeat installation.

The diagnostic is covered by synthetic success/failure snapshots proving unchanged file hashes/mtime, sanitized command errors and explicit rejection of mutating Docker commands. Total synthetic tests:87.


The installer preserves the local original Dockerfile byte-for-byte as the staged prefix and only appends the required module COPY. Its validated single-stage layout now supports the existing optional ENV, EXPOSE and HEALTHCHECK instructions in their established positions, each at most once. Values are never read back to public artifacts or logs, and cannot be supplied as new installer arguments. Default single-backslash continuations (including comment lines) are parsed as one logical instruction. Custom syntax/escape directives, unsupported instructions, duplicate/reordered layout and dangling or repeated trailing escape characters fail closed with fixed errors. Synthetic fixtures cover ENV/HEALTHCHECK preservation, CRLF bytes and multiline continuations. Existing ENV remains in the rebuilt image as in the original image; it is not a new credential store.
