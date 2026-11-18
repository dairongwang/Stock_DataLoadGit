# Scheduling

Everything the dashboard does can run from a scheduler (cron, Windows Task
Scheduler) with the venv's Python. Commands return **0** on success, **1**
when a file / procedure failed, **2** on a configuration error.

```bash
# every evening after the deliveries arrived: convert, load the folder, promote
.venv/bin/python -m stock_dataload convert-folder
.venv/bin/python -m stock_dataload load-folder /deliveries/$(date +%Y) --promote

# or the loading folder only
.venv/bin/python -m stock_dataload load && .venv/bin/python -m stock_dataload promote all

# every December: next year's no-trading days
.venv/bin/python -m stock_dataload no-trading-days update --year 2027

# monthly: free the space of promoted staging rows
.venv/bin/python -m stock_dataload db purge-stage --vacuum
```

Logs go to `logs/stock_dataload.log` (rotating) and to `App_Log`; failures
are visible on the Monitoring pages and via `python -m stock_dataload errors`.

On Windows:

```
C:\...\Stock_DataLoadGit\.venv\Scripts\python.exe -m stock_dataload load-folder D:\deliveries --promote
```
