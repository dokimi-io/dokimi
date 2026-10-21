*** Settings ***
Documentation       Shared keywords for dokimi suites.
...
...                 A user-facing suite imports this resource, which imports the Python
...                 library. No PromQL and no Kubernetes objects appear in the suite
...                 itself (ADR-01-03, kept by ADR-04-05 as the Robot format's design).
...
...                 The library provides ``Run Network Test`` and the criterion keywords,
...                 such as ``Throughput Should Be``, which check what the test measured.

Library             ../libraries/DokimiLibrary.py
