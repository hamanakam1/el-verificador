# Pull Request Template

## Description
<!-- What does this change, and why? -->

## Type of Change
- [ ] Bug fix
- [ ] New feature
- [ ] Breaking change
- [ ] Documentation update
- [ ] Refactoring (no functional change)
- [ ] Test updates

## Related Issues
<!-- Fixes #123 -->

## Verification
<!-- The gates this change must pass before it is reviewed. -->

- [ ] `python -m pytest` passes
- [ ] `python -m unittest discover -s tests` passes (CI runs both)
- [ ] `ruff check .` is clean
- [ ] `mypy el_verificador` is clean under `--strict`
- [ ] The golden fixture suite still reaches every known verdict
- [ ] The test suite leaves `~/.el-verificador/` exactly as it found it

## If this changes the decision path
<!-- A change that alters what passes or fails needs evidence. -->

- [ ] A golden fixture was added or updated under `tests/fixtures/golden/`
- [ ] The fixture's `expected.json` names the expected decision **and** the
      expected violated obligations
- [ ] The fixture was checked against the real code, not against the intent
- [ ] The change to what passes or fails is called out in the description

A change to the decision path without a fixture is not finished: the diff
engine had no test file at all for four commits, and the engine test mocked it
out, so a diff engine that could not see a rewritten function body shipped
green.

## If this touches the sandbox, the commit path or the record
<!-- These are the boundaries that protect the operator's machine. -->

- [ ] No acceptance command can run on the host
- [ ] The commit is still scoped to the verified change set
- [ ] The receipt's canonical bytes still match EdgeRecord's fingerprint tool

## Privacy & Security
- [ ] No secrets or credentials committed
- [ ] No real transcripts, customer data or private repositories referenced
- [ ] The fixtures are synthetic
- [ ] Local-first and opt-in behaviour preserved

## Checklist
- [ ] Code follows `AGENTS.md`
- [ ] Self-review completed
- [ ] Documentation updated where behaviour changed
- [ ] `CHANGELOG.md` updated
