"""Kubernetes client wrappers, ownership labels and deadlines.

Every resource a run creates is labelled run-owned so cleanup can find it again, and
an owner reference makes the cluster's garbage collector a backstop (ADR-05-02).
"""
