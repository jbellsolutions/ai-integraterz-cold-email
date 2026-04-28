# AGI-1 Genome

This repo uses AGI-1, a self-healing AI development framework.

AGI-1 learns as it works. Learnings are stored in a **local genome** at:
`~/.claude/agi-1-genome/genome.json`

The genome is **yours** — it lives on your machine and is never automatically shared.

**What it contains:** Error patterns, fix strategies, and learning insights.
**What it never contains:** Your code, file paths, API keys, or secrets.

To inspect: `cat ~/.claude/agi-1-genome/genome.json`
To reset: `rm ~/.claude/agi-1-genome/genome.json && ./setup`
Full privacy details: [AGI-1 docs/genome-privacy.md](https://github.com/jbellsolutions/agi-1/blob/main/docs/genome-privacy.md)
