# Default config – Fast Paper Trader Momentum

Use this when you want to **set config back to default** (e.g. ask the AI: "set it back to default").

| Setting | Default |
|--------|--------|
| **VWAP** | `MAX_DISTANCE_FROM_VWAP_PCT = 3.0` |
| **Bars** | `BAR_MINUTES = 2`, `INTRABAR_MIN_AGE_SECONDS = 60` |
| **Signal** | `MIN_PCT_CHANGE_1D = 1.5`, `MIN_RELATIVE_VOLUME = 1.2` |
| **Target/stop** | `TARGET_PCT = 0.04`, `STOP_PCT = 0.03` |
| **Trailing** | `TRAIL_BREAKEVEN_MFE_PCT = 2.0`, `TRAIL_ACTIVATE_MFE_PCT = 3.0`, `TRAIL_DISTANCE_PCT = 1.5` |
| **Limits** | `MAX_SIGNALS_PER_DAY = 20`, `MAX_POSITIONS = 10`, `ENTRY_ORDER_TIMEOUT_SECONDS = 900` |

The AI rule in `.cursor/rules/default-config.mdc` uses this as the canonical default when you say "set back to default".
