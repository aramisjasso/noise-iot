"""Runs once on every reset, before main.py.

Keep this file minimal: low-level setup only (e.g. silencing debug output).
Network, time sync and the measurement loop belong to main.py so that a
failure there does not leave the board unreachable from the REPL.
"""
