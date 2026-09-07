# VolunteerDB

## Use the LSP, not grep

An eglot LSP server is live in this project's Python buffers: `ty server` (Astral's
type checker), one server per project, attached to `python-ts-mode`. It understands
this codebase's types, imports and call graph. **Reach for it before reaching for
`grep`, `rg` or `find` on Python symbols** — grep matches strings, the LSP matches
bindings, so it does not miss aliased imports and does not report matches in
comments, docstrings or unrelated identifiers that merely share a substring.

Grep remains the right tool for non-symbol work: text in Markdown/YAML/SQL, log
lines, template strings, `git grep` over history, or a first sweep when you don't
yet know the symbol's name.

### Diagnostics — `mcp__ide__getDiagnostics`

Pass a `file:///` URI. Returns ty's real type errors tagged `"source":"eglot-check"`.
Line and character are **0-based** — add 1 to get the editor's line number.

Prefer this over running a type checker in the shell. `flycheck-mode` and
`python-flymake` are also active, so a response may mix sources; check the `source`
field. Run this on any Python file you edit before you call the work done.

### References — `mcp__emacs-tools__claude-code-ide-mcp-xref-find-references`

Routes through `eglot-xref-backend`, so it gives whole-project LSP references.

**Gotcha: it is point-dependent and its `identifier` argument is ignored.** Eglot's
`xref-backend-references` method discards the identifier string and dispatches on
wherever point happens to be, so the tool returns "No references found" unless point
already sits on the symbol. The working sequence is:

1. `mcp__ide__executeCode` to move point onto the symbol, e.g.
   `(with-current-buffer (get-file-buffer "/abs/path.py")
      (goto-char (point-min)) (re-search-forward "^class Volunteer") (forward-char -2))`
2. call the xref tool
3. `mcp__ide__executeCode` again to restore point to where the user had it —
   `mcp__emacs-tools__current-buffer-info` reports the original line and column.

If the file isn't open, `find-file-noselect` it first and let eglot attach.

### Anything else — `mcp__ide__executeCode` + jsonrpc

`(jsonrpc-request (eglot-current-server) :textDocument/hover (eglot--TextDocumentPositionParams))`
and any other LSP method. This server advertises: definition, declaration,
typeDefinition, references, rename, documentSymbol, workspaceSymbol, typeHierarchy,
semanticTokens, codeAction, signatureHelp, foldingRange, selectionRange,
executeCommand.

Read capabilities with `eglot--capabilities`, not `eglot--server-capabilities`
(void in this version).

### Known standing diagnostics, and the ceiling over them

`ty check src/` reports 95 diagnostics, and `make types` (CI's lint job runs the
same `scripts/typecheck.py`) fails the build above that number. So a diagnostic
in a file you edited is **not** automatically pre-existing: check it against the
ceiling before assuming it was already there, and if your change adds one, fix
the type rather than raising the ceiling. Fewer is a ratchet — lower `CEILING`
in the same commit.

The remainder is roughly half SQLAlchemy's declarative surface (model `__init__`
overloads, Enum column descriptors) and half real `X | None` narrowing gaps in
`query_lang.py` and `services/elections.py`.

The three in `src/volunteerdb/models.py` on `Volunteer.__table__`,
`Team.__table__` and `Membership.__table__` — `Argument to function
_make_history_table is incorrect: Expected Table, found FromClause` — are the
clearest of the not-a-bug kind: SQLAlchemy declares `DeclarativeBase.__table__`
as `FromClause`, and the code is correct. Not something to "fix" unasked.
