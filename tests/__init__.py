"""Test-suite package.

Password hashing uses 600k PBKDF2 rounds in production; the tests create many
accounts, so the work factor is lowered here (the hashes stay fully functional).
"""
import stock_dataload.auth as _auth

_auth.PBKDF2_ITERATIONS = 2_000
