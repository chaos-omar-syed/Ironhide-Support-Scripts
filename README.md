# Ironhide Support Scripts

Field and analysis tools for MRU radar units. Each tool lives in its own folder
with a README that explains what it does, how it works, and how to run it.

| Tool | Purpose |
|---|---|
| [aoa_bias_correct/](aoa_bias_correct/) | Measure the radar's angle-of-arrival (azimuth / elevation) bias against MAVLink drone GPS truth for a given MRU, run and job range, then show tracks before and after the correction. |

## Environment

Everything runs in the `sensorenv` micromamba environment, which carries the
`chaotic` package plus numpy, pandas, polars, matplotlib, plotly and pymongo:

```bash
micromamba run -n sensorenv python <tool>/<script>.py --help
```

No environment variables are needed. The scripts set `JAX_PLATFORMS=cpu` themselves
before importing chaotic, so they run the same on a GPU workstation, a field laptop,
or an MRU box.
