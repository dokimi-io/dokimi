"""Local CLI entry point.

Phase 1, outside the cluster: validates input, renders manifests, submits the one-shot
Job, streams logs and can reattach. ``--dry-run`` stops here (ADR-01-06).
"""
