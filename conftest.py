"""Root conftest: its presence puts the repo root on sys.path.

That makes the shipped Python modules (review/) importable from the tests
without packaging metadata, which T1 deliberately avoids adding.
"""
