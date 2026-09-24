"""Authoring format plugins: ``parse()`` and ``render()``.

Robot and YAML are peer authoring formats, each a plugin against the canonical model
(ADR-04-05). Robot Framework's *parsing* API may be imported here, inside the Robot
format plugin only — never by the rest of the library (ADR-01-02, ADR-01-08 phase 2).
"""
