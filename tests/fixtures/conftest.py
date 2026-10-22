"""Golden fixture trees are data, not tests.

The files under this directory are synthetic repositories that the golden
fixture suite builds into real git repositories. Several of them are named
`test_*.py` on purpose — the test-weakening detection has to see real test
files — so the collector must be told to leave this tree alone.
"""
collect_ignore_glob = ["*"]
