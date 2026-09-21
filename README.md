# Ironhide Support Scripts

Field and analysis tools for MRU radar units. Each tool lives in its own folder
with a README that explains what it does, how it works, and how to run it.

| Tool | Purpose |
|---|---|
| [aoa_bias_correct/](aoa_bias_correct/) | Measure the radar's angle-of-arrival (azimuth / elevation) bias against MAVLink drone GPS truth for a given MRU, run and job range, then show tracks before and after the correction. |
| [skills/seawall-post-analysis/](skills/seawall-post-analysis/) | **Claude Code skill**: the full post-analysis of a radar test day or campaign (dump → flight manifest → conditioning → analyses → day reports + rollup → narrative), built from the Seawall Week Report pipeline. The scripts do the deterministic work; the skill carries the judgment rules for the messy parts (track steals, frozen truth, duplicate ids, false passes). |
| [ironhide_dashboard/](ironhide_dashboard/) | **Live radar dashboard** (Streamlit): connect to an MRU by number, watch MAVLink truth vs radar tracks on a satellite map with track-error, velocity, separation / CPA and measurement-space cards (target and interceptor), replay saved flights, save a live window to a replayable archive (auto-trimmed to the airborne time). Self-contained; optional chaos-spa grader. See its README for install/run and "Reading the figures". |

## Installing the skill

Claude Code loads personal skills from `~/.claude/skills/<name>/SKILL.md`. Point it at
the folder in this repo (symlink keeps it in sync with `git pull`):

```bash
mkdir -p ~/.claude/skills
ln -s "$(pwd)/skills/seawall-post-analysis" ~/.claude/skills/seawall-post-analysis
```

Then ask Claude to "post-process this test day" or "build the campaign report" and it
will follow `SKILL.md`. The skill's own scripts run in `sensorenv`; ffmpeg is needed
only for the seeker-FOV video player.

## Environment

Everything runs in the `sensorenv` micromamba environment, which carries the
`chaotic` package plus numpy, pandas, polars, matplotlib, plotly and pymongo:

```bash
micromamba run -n sensorenv python <tool>/<script>.py --help
```

No environment variables are needed. The scripts set `JAX_PLATFORMS=cpu` themselves
before importing chaotic, so they run the same on a GPU workstation, a field laptop,
or an MRU box.
