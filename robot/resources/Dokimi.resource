*** Settings ***
Documentation       Shared keywords for dokimi suites.
...
...                 A user-facing suite imports this resource, which imports the Python
...                 library. No PromQL and no Kubernetes objects appear in the suite
...                 itself.
...
...                 The library provides ``Run Network Test`` and the criterion keywords,
...                 such as ``Throughput Should Be``, which check what the test measured.

Library             ../libraries/DokimiLibrary.py
