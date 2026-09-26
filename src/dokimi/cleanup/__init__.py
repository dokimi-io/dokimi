"""Run-scoped teardown.

Phase 11. Deletes run-owned resources on success, retains them on failure, and is
independently invocable so a lost connection mid-run never strands resources
(ADR-05-02).
"""
