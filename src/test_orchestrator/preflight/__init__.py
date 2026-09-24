"""Run-level cluster checks, before any test workload is deployed.

Phase 4, inside the Job, after planning and before suite rendering. Blocking:
scheduling, CNI connectivity, DNS, image pull, node count against the planned matrix,
and Prometheus query access (ADR-05-07, ADR-01-08).
"""
