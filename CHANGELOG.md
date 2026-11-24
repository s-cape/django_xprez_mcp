# Changelog

## 0.8.0 (unreleased)

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
