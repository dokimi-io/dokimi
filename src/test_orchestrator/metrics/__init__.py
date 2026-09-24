"""Exporter reads and the Prometheus client.

Phase 7. The exporter's own endpoint is read directly for the verdict, and Prometheus
scrapes the same sidecar for history; a disagreement beyond tolerance is surfaced
rather than silently resolved (ADR-02-02, ADR-03-01, ADR-03-02).
"""
