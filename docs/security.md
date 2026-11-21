# Security checklist

An MCP server gives an agent the caller's permissions. An agent with a
token can do anything the user could do through the exposed tools,
repeatedly and quickly. Go through this list before exposing a server.

## Access

- [ ] The server rule is `allow_staff` or a project function (which may
  check a project permission); `allow_authenticated` or `True`
  only when that is really the audience.
- [ ] `manage.py check` passes (`xprez_mcp.E001`: a server without a
  permission).
- [ ] `./manage.py mcp_inspect --user <user>` was run for a typical user and
  for a staff user, and the visible items are the intended ones.
- [ ] `allow_anonymous=True` is used only on servers meant to be public.

## Tokens

- [ ] The endpoint is served over HTTPS only; the bearer token is the whole
  credential.
- [ ] Tokens have a `name` (where they are used) and an `expires` date where
  possible. `last_used` shows stale ones to delete.
- [ ] Users know a key is shown once and a lost key means a new token.
- [ ] A custom `TOKEN_RESOLVER` compares in constant time, stores only
  hashes and rejects inactive users, like `xprez_mcp.contrib.tokens`.

## Writes

- [ ] Each `actions` list with `create`, `update` or `delete` is deliberate.
- [ ] `create` has a permission (it is not limited by `get_queryset`).
- [ ] `admin_actions` lists only actions that are safe to run without a
  confirmation page.
- [ ] `ALLOW_WRITES` lists only third-party apps whose write tools were
  reviewed.
- [ ] Destructive custom tools are marked `@tool(destructive=True)`, so
  clients can ask the user before calling them.

## Data

- [ ] `fields` / `serializer` leave out secrets (API keys, tokens, internal
  notes). Use `hidden_fields` on admin providers.
- [ ] Models holding credentials set `mcp_exposed = False`.
- [ ] Docstrings and results are treated as untrusted by the agent: record
  contents can contain prompt injections, so a write tool reachable from a
  read of user-supplied text deserves a second look.

## Browsers

- [ ] `ALLOWED_ORIGINS` lists only origins that should reach the endpoint
  from a browser. Requests with any other `Origin` are refused (DNS
  rebinding protection).
- [ ] Session authentication (`xprez_mcp.auth.session`) is enabled only when
  needed; the JSON content type and Origin check stop cross-site form posts.

## Load

The library bounds page sizes (`limit` up to `max_page_size`, 100 by
default) but has no rate limiting and no query timeouts yet; see Hardening
in [design.md](design.md). Until then:

- [ ] Large tables have indexes on their `search_fields`, filters and
  ordering, or a cheaper `search()` override.
- [ ] Rate limiting, if needed, is done in front of Django (reverse proxy)
  or in middleware.
- [ ] On PostgreSQL, a `statement_timeout` for the database role or
  connection bounds slow queries.
