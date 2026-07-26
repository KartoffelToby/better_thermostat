# Better Thermostat translations

Better Thermostat uses Home Assistant's native custom-integration localization.
Home Assistant selects the catalog from the user's frontend language; no locale
switch or Python registration table is needed.

## Catalog layout

- `en.json` is the canonical runtime catalog and English fallback.
- `<language-tag>.json` contains one complete translation. File names use BCP 47
  language tags, for example `ru.json`, `de.json`, or `pt-BR.json`.
- `../strings.json` mirrors `en.json` for repository tooling and must remain
  byte-for-byte equivalent after JSON parsing.
- `../../../project.inlang.json` lists every catalog for the Inlang editor.

The catalogs cover config and options flows, repairs, services, selectors,
device automations, and entity names. Python entities reference these strings by
stable `translation_key`; do not add user-facing `_attr_name` strings to entity
classes.

## Add a language

1. Copy `en.json` to `<language-tag>.json`.
2. Translate values only. Preserve object keys, Markdown, line breaks, and every
   placeholder such as `{name}`, `{trv_name}`, or `{docs_url}` exactly.
3. Add the language tag to `project.inlang.json` so it appears in the Inlang
   editor.
4. Run:

   ```bash
   pytest tests/test_translations.py
   ```

The validation checks JSON structure, complete key coverage, unknown keys,
placeholder parity, non-empty values, entity translation coverage, and the
Inlang catalog list. A developer can therefore add another language without
changing the integration's Python logic.

## Add or change a source string

1. Edit the English value in `en.json` and mirror the same structure in
   `../strings.json`.
2. Update every language catalog. Until a translator supplies localized copy,
   use the English value rather than omitting the key, so Home Assistant never
   exposes a raw translation key.
3. When adding an entity name, add its key below `entity.<platform>` and set the
   entity's `_attr_translation_key` to that stable key.
4. Run the translation tests before opening a pull request.
