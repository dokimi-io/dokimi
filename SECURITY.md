# Security policy

## Reporting a vulnerability

Please don't open a public issue for a security problem. Report it privately through GitHub
instead: on the repository's **Security** tab, choose **Report a vulnerability**. Only the
maintainers see the report, and we can discuss and fix it there before anything is public.

Useful in a report:

- what an attacker can do, and what they need first (cluster access, a crafted test definition,
  a position on the pod network);
- the version or commit, and how to reproduce it;
- what you think the fix is, if you have an idea.

We aim to acknowledge a report within a week and to agree with you on when it becomes public.

## Supported versions

dokimi has no release yet. Until 0.1, fixes go to `main` only. From then on, the latest release
gets security fixes.

## Scope

A run executes inside your cluster with the permissions in `deploy/rbac.yaml`, so reports about
those are especially welcome: a grant that is wider than a run needs, a way for a test definition
or a test pod to reach beyond the run's own objects, or a way to read or change another run's
results. Weaknesses in Kubernetes, Prometheus, Robot Framework or iperf2 themselves belong to
those projects; tell us too if dokimi makes one easier to exploit.
