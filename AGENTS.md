# astrbot_plugin_koharu_plus Project Rules

Document only durable, repository-specific constraints here. Do not record current file
layouts, temporary paths, or implementation details that may change during a refactor.
Normal Python, AstrBot plugin, and Git practices are assumed.

## Change Policy

- `master` only receives released, tested code. All development happens on `dev` (or
  feature branches off `dev`), and is merged to `master` after verification.
- Keep `metadata.yaml` and `README.md` in sync with the actual behavior and version.
  Bump `version` in `metadata.yaml` on every release; `astrbot_version` declares the
  minimum AstrBot version the plugin is tested against.
- Every release documents its user-visible changes in `CHANGELOG.md` (Keep a Changelog
  format, Chinese). `README.md` is usage documentation only — never add version-change
  markers like "v1.4.0 新增" / "new in v1.x.y" to it.
- Do not break the AstrBot config contract silently. Renaming or removing keys in
  `_conf_schema.json` requires a migration note in the release commit and README.
- Never add backward compatibility shims for the Koharu HTTP API; update the client when
  the API changes.

## Code Conventions

- Handlers, hooks, and client methods are `async def`; blocking work (image compression)
  stays in module-level helper functions.
- Public methods and hook signatures carry type hints.
- `main.py` is the entrypoint and orchestrates; HTTP/API logic lives in
  `koharu_client.py` — do not open raw httpx sessions elsewhere.
- No hardcoded URLs, tokens, or secrets. Everything user-facing is configurable through
  `_conf_schema.json` and read from `AstrBotConfig`.
- Use the plugin KV storage interface (`put_kv_data`/`get_kv_data`/`delete_kv_data`) for
  lightweight state instead of new files where appropriate.
- User-facing strings go into `.astrbot-plugin/i18n/` (`zh-CN.json` and `en-US.json`),
  not inline in code.

## Type Checking

- The plugin is checked with pyright in `strict` mode (`pyrightconfig.json`);
  run `.venv/bin/pyright` from the plugin directory after changes. No
  `# type: ignore` / `# pyright: ignore` comments and no `Any` are allowed in
  plugin code. Composite types must be named (dataclass / TypedDict /
  Protocol), not written inline.
- Two rules are relaxed at config level because the AstrBot SDK ships without
  `py.typed` and its plugin-facing surface is untyped. Keep the rationale:
  - `reportMissingTypeStubs`: astrbot.* packages have no stub files.
  - `reportUnknownMemberType`: SDK members (e.g. `Image.fromFileSystem`) and
    decorators (`filter.command`, `Star.__init__`'s `AstrBotConfig` param) are
    untyped; unknown values derived from them are still caught by
    `reportUnknownVariableType` / `reportUnknownArgumentType`.
- SDK values that must be consumed are cast exactly once at the module
  boundary (e.g. `AstrBotConfig` → `PluginConfig`, OneBot payloads → named
  TypedDicts in `onebot_client.py`); plugin logic never touches untyped dicts.

## Koharu API Boundary

- All Koharu HTTP interactions go through `KoharuClient`; translate errors into
  `KoharuApiError`/`KoharuTimeoutError` rather than leaking httpx exceptions to handlers.
- Treat the koharu-docker repo (`../koharu-docker`, https://github.com/ternurarl/koharu-docker/tree/dev) as
  the source of truth for the API contract. When endpoints or payloads change, update the
  client, and verify against a running instance.

## What Not to Commit

- Generated translation outputs (`data/plugin_data/astrbot_plugin_koharu_plus/outputs/`),
  model weights, or machine-specific artifacts.
- `.venv/`, `venv/`, `__pycache__/`, `.env`, IDE configs (already covered by `.gitignore`).

## Verification

- Install the AstrBot SDK (`pip install -U astrbot`) in the local venv and use it for
  signature lookup and IDE completion; the local repo code wins over package docs when
  they disagree.
- Run the smallest relevant check when iterating. Debug against a local Koharu instance;
  only run end-to-end chat tests when the user explicitly asks.
