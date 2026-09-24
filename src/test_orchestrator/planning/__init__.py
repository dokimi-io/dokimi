"""Node resolution, matrix construction and the concurrency plan.

Phase 3. Bounded concurrency with node exclusivity is the default strategy, and the
scheduler stays here in Python rather than inside Robot's execution model
(ADR-04-01, ADR-04-04).
"""
