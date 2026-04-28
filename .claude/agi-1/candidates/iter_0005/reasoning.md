# iter_0005 — Deploy + Walkthrough

Targeted: G-Stack Workflow Coverage (was 5/15, missing Dockerfile +5).

Added:
- Dockerfile — minimal Python 3.13-slim image with Node + Smartlead CLI + forge,
  watchdog as CMD, healthcheck on daemon presence
- WALKTHROUGH.md — operator install/run/troubleshoot doc with the AGI-1
  trigger line `> **To install:** Open Claude Code in this folder and type \`set this up for me\` or \`/walkthrough\``

G-Stack now at 100/100. Workflow Coverage moved from 5/15 to 10/15 (still missing
pre-commit which we have at .pre-commit-config.yaml — actually that's already
counted in iter_0002 +5 = 10. Now Dockerfile takes us to the cap of the deploy slot).
Recheck: Workflow had Planning(5)+Pre-commit(5)+Deploy(5)=15 total possible.
After iter5: 5 (TODOS) + 5 (pre-commit) + 5 (Dockerfile) = 15/15. So G-Stack = 100/100.
