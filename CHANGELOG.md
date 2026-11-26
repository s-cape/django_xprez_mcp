# Changelog

## Unreleased

Fixes from a review of the library.

Upgrading: a tool raising Django's `ValidationError` with a dict now returns
`{"error", "fields"}` instead of the bare messages, and a tool whose type
hints cannot become a schema now fails `manage.py check`
(`xprez_mcp.E006`); before, it broke `tools/list` at runtime.

- Write tools of `register_admin` providers no longer disappear when two
  threads build the catalog at once (the first requests of a threaded
  worker).
- The system check `xprez_mcp.E006` reports a tool whose input schema cannot
  be built.
- `ProtectedError` / `RestrictedError` and other `IntegrityError`s are
  readable tool errors instead of "Internal error."; the database's details
  are logged, not sent.
- A `ValidationError` with a dict keeps its field names.
- `AdminProvider`: `update` also takes the fields of the admin's change form
  (with `UserAdmin` it took the username only). `mcp_inspect` notes a
  `create` whose add form requires fields that are not model fields; the new
  hook `write_field_names(partial)` picks the fields per action.
- `NaN` and `Infinity` in a request are a parse error, and in a result
  become `null`: responses are always valid JSON.
- A non-string tool or prompt name and malformed `completion/complete`
  params are invalid params (`-32602`), not internal errors.
- A `datetime` argument ending in `Z` works on Python 3.9 and 3.10.

## 0.9.0 (2026-11-26)

Helpers for providers of hand-written tools, all opt-in.

Upgrading: `annotated_types` markers that tools already have (as pydantic
users write them: `Annotated[int, Ge(0)]`) used to be ignored. Their number
and length limits are now part of the schema and checked; markers that do
not fit the type are still ignored. `mcp_oauth_cleanup` is now
`mcp_cleanup`, and `XPREZ_MCP["OAUTH"]["BASE_URL"]` is now
`XPREZ_MCP["BASE_URL"]`. Nothing else changes until a project uses the new
features.

- `Lookup` arguments: `Annotated[Model, Lookup(resolver, **options)]` takes a
  reference (`type=str` by default) and passes the tool
  `resolver(ctx, value, **options)`; a string names a provider method. Works
  optional and in lists and dicts. To adopt: move the "find it and check
  access" lines at the top of each tool into a resolver, raising `ToolError`
  (or `PermissionDenied` / `ObjectDoesNotExist`), and annotate the parameter.
- Transactions per write tool: `XPREZ_MCP["ATOMIC_WRITES"]` /
  `MCPServer(atomic_writes=...)` (`True` or a database alias; default `False`)
  runs every tool that is not `read_only` in `transaction.atomic()`, rolled
  back on any error result; `@tool(atomic=True | False | "alias")` overrides
  it; the system check `xprez_mcp.E005` reports an unknown alias. To adopt:
  set `ATOMIC_WRITES = True`, check your read tools are marked
  `read_only=True`, and drop the hand-written `atomic()` blocks.
- `Range(min, max)` and `Length(min, max)` in `Annotated` add `minimum` /
  `maximum`, `minLength` / `maxLength` or `minItems` / `maxItems` to the
  schema and are checked with readable messages; `annotated_types` markers
  (`Ge`, `Gt`, `Le`, `Lt`, `Interval`, `MultipleOf`, `MinLen`, `MaxLen`,
  `Len`) work too, without a dependency. To adopt: replace hand-written range checks with
  `Annotated[int, Range(1, 52)]`.
- `mcp_cleanup` replaces `mcp_oauth_cleanup`: one command that cleans up
  every installed app that has an `AppConfig.mcp_cleanup(now)` method (the
  OAuth app today), optionally `--only` some of them. To adopt: replace
  `mcp_oauth_cleanup` in your schedule with `mcp_cleanup`, run hourly.
  `mcp_oauth_cleanup` is removed.
- One-time upload URLs (`xprez_mcp.contrib.uploads`): a tool calls
  `prepare_uploads(ctx, files)` and returns per-file `PUT` URLs; the client
  sends the files from disk (curl, in parallel); a later call takes them as
  a file argument `{"upload": {"batch", "path"}}` (the new `"upload"`
  source) or reads the whole batch with `get_batch()`. Keys travel in the
  `Authorization` header and are stored hashed; content is checked by its
  bytes; uploads are consumed when the call succeeds, inside its
  transaction. To adopt: add the app to `INSTALLED_APPS`, `migrate`, set
  `XPREZ_MCP["UPLOADS"]["STORAGE"]` to private storage, schedule
  `mcp_cleanup` hourly, return `prepare_uploads()` from a tool, and accept
  `"upload"` in `file_argument(...)` or a provider's `file_sources`.
- `FileValue` has `size`, `sha256`, `content_type`, `read()` and `open()`.
- `CallContext.on_call_end(func)` runs `func(ok)` when a tool call ends,
  inside its transaction on success; `ctx.tool_database` is the alias the
  call runs in a transaction on.
- `XPREZ_MCP["BASE_URL"]` replaces `XPREZ_MCP["OAUTH"]["BASE_URL"]` and is
  used for every absolute URL. To adopt: move the setting up a level.
- `xprez_mcp.testing.McpTestCase` with `rpc()`, `call()` and
  `assertToolError()`, signing users in through the server's OAuth, stored
  token or session backend, and `xprez_mcp.contrib.oauth.testing.access_token`.
  To adopt: subclass `McpTestCase` instead of your own JSON-RPC helpers.

## 0.8.0 (2026-11-24)

- `FileArgumentsMixin` (`xprez_mcp.providers`) holds the file argument
  handling of `ModelProvider` (`resolve_files`, `resolve_file`,
  `stored_file`, `allows_file_path`, `fetch_file`,
  `removing_uploads_on_error`, `file_sources`, `max_file_size`,
  `file_fetch_timeout`), so a plain `Provider` writing file fields through
  its own forms can inherit it. `ModelProvider` and `AdminProvider` behave
  as before.
- `FileArgumentsMixin.file_value(value, field, path)` converts a file in an
  untyped argument (`fields: dict[str, Any]`) as a typed file argument does.
- `form_files`, `closing_stored_files` and `check_cleared` are public, in
  `xprez_mcp.providers.files` (still importable from
  `xprez_mcp.providers.model`).
