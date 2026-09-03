# Project skills

Skills in this folder are **shared with everyone who clones the repo**: any
coding agent opened on this checkout discovers them automatically, no
per-machine setup. They are for procedures the repo cannot express in code —
the sequence of files to touch, the invariant to preserve, the check to run
before calling something done.

## What belongs here

A skill, not a doc, when the task is **recurring, multi-file and easy to do
half-way**. The test: if someone doing this task for the second time would
still get the order or the invariant wrong, write a skill.

- `update-tutorial/` — the in-app guided tour and "what's new"
  ([src/stocks/web/onboarding.py](../../src/stocks/web/onboarding.py)) only
  stays true to the app if it is updated when a feature ships: a step in the
  registry, copy in every locale, an entry in `RELEASES`.

What does **not** belong here: how the code works (that is what the modules'
docstrings are for — they are unusually thorough in this repo and are the
first thing to read), one-off instructions, or anything with a secret in it.

## Adding one

```
.claude/skills/<kebab-case-name>/SKILL.md
```

`SKILL.md` starts with YAML frontmatter:

```yaml
---
name: my-skill
description: "What it does, and — spelled out — when to use it. This line is
  the only thing an agent sees before deciding to load the skill, so name the
  triggers explicitly: the files, the words a person would use, the situations."
---
```

Then the body: the steps, in order, with the paths written out and the
verification command at the end. Reference repo files by relative path so the
links resolve from the skill's own directory (`../../src/...`).

Supporting files (templates, examples, longer references) live beside
`SKILL.md` in the same directory and are linked from it.

## What is not shared

Two things in `.claude/` stay per-machine and are gitignored:

- `settings.local.json` — personal permissions and preferences.
- `skills/developing-with-streamlit` — a symlink into `.venv`, shipped by the
  installed Streamlit package. It only exists where that package is installed,
  which is why `/.claude/` used to be ignored wholesale.

Shared agent configuration, if we ever need it, goes in a versioned
`.claude/settings.json` next to this folder.
