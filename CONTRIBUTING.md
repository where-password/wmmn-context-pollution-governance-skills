# Contributing

1. Open an Issue with a minimal reproduction and a redacted sample.
2. Never attach `.env`, API keys, cookies, private conversations, production databases, or Milvus credentials.
3. Keep the two Skill responsibilities separate: Cleaner proposes; Database independently validates and persists.
4. Add or update tests for behavior changes.
5. Run `scripts/run_all_tests.ps1` before opening a pull request.
6. Keep Milvus tests isolated and non-production.
7. Do not add automatic filesystem scanning, automatic final-answer synthesis, or automatic production alias changes.

Useful bug reports include OS, Python version, agent host, command, expected behavior, actual behavior, and a synthetic minimal input.
