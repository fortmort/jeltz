# Jeltz: Claude Code Hooks

Claude Code is consistently inconsistent:

- **IOUs**: Declarations without delivery
  - `TODO: handle edge cases` (never handled)
  - `FIXME: this might break` (it already did)
  - Placeholder functions like `def process(): pass` with no logic
  - Comments that promise tests, docs, or cleanup—but deliver none
- **Hallucinations**: Imagined implementation posing as progress
  - Functions returning variables that were never defined
  - `return {"status": "success"}` in code that doesn’t even succeed
  - References to nonexistent modules, classes, or APIs
  - Docstrings describing behavior that the code doesn’t implement
- **Slop**: Emoji glitter, formatting roulette, and newline chaos
  - `print("✨ Done!")`
  - Mixed tabs and spaces, random blank lines
  - Files missing EOF newlines

These hooks and skills try to make Claude Code more deterministic: less Vogon poetry, more reproducible prose.
