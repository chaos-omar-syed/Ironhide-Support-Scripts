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
JAX_PLATFORMS=cpu micromamba run -n sensorenv python <tool>/<script>.py --help
```

`JAX_PLATFORMS=cpu` keeps chaotic's JAX import off the GPU path on workstations
without one.
