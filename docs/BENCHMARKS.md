# Benchmarks

These are checked-in outputs from actual runs on September 30, 2026 against one local Uvicorn worker on macOS with transcoding disabled to isolate synchronization and WebSocket capacity. They are not production promises.

## Network benchmark

The runner used three synthetic playback clients and real WebSocket/server measurement traffic. Delay was injected per direction at the application-message layer. Recovery is the elapsed time after an action until all three clients agree on play state and are within 200 ms.

### Steady-state synthetic-clock protocol simulation

This table measures a **synthetic-clock protocol simulation, not decoded browser video drift**. It excludes the three seconds after each playback action and validates the synchronization protocol's corrected-time projection.

| Scenario | Injected condition | Samples | Drift p50 | Drift p95 | Drift max |
| --- | --- | ---: | ---: | ---: | ---: |
| Good | 20 ms delay | 56 | 0.000 ms | 0.002 ms | 0.002 ms |
| Mediocre | 150 ms delay + 50 ms jitter | 41 | 0.000 ms | 0.001 ms | 0.001 ms |
| Bad | 300 ms delay + 2% loss | 32 | 0.001 ms | 0.002 ms | 0.002 ms |

### Session-wide protocol simulation

| Scenario | Samples | Drift p50 | Drift p95 | Drift max | Seek recovery | Pause recovery |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Good | 179 | 0.000 ms | 0.914 ms | 40,943.891 ms | 55.107 ms | 54.221 ms |
| Mediocre | 119 | 0.001 ms | 329.761 ms | 40,833.029 ms | 295.678 ms | 376.644 ms |
| Bad | 85 | 0.001 ms | 32,480.914 ms | 40,694.393 ms | 607.972 ms | 609.184 ms |

The very large session-wide maxima are genuine transition samples: one client has applied the 45-second benchmark seek while another client's delayed pre-seek report is still current. They remain visible in the session-wide metric rather than being discarded.

![Measured network drift](../results/network_drift.svg)

Run the scenarios and regenerate the CSV and chart:

```bash
WATCHPARTY_DISABLE_TRANSCODING=1 python -m uvicorn server:app --port 8000
python tools/network_benchmark.py --base-url http://127.0.0.1:8000 --output results
```

For manual browser testing through an impairment proxy:

```bash
python tools/ws_netem_proxy.py \
  --upstream ws://127.0.0.1:8000 \
  --port 9000 --delay-ms 150 --jitter-ms 50 --loss 0
```

## WebSocket capacity

The Locust capacity sweep ran both Uvicorn and Locust on one MacBook Pro with an Apple M4 (10 cores), 16 GB RAM, and macOS 15.7.3. It used 100 rooms, a spawn rate of 800 clients/second, 12-second stages, position reports, and WebSocket ping/pong event timing. It recorded no sync-event failures in these stages.

| Concurrent clients | Event latency p95 |
| ---: | ---: |
| 400 | 1 ms |
| 800 | 1 ms |
| 1,600 | 1 ms |
| 3,200 | 12 ms |
| 6,400 | 240 ms |

Under this specific local test, the requested 200 ms p95 limit was first exceeded at **6,400 concurrent clients**. The last tested capacity below the limit was **3,200 clients**. Raw and consolidated outputs are in [`results/`](../results/).

![Measured load latency](../results/load_latency.svg)

Reproduce the sweep:

```bash
python load/run_capacity.py \
  --base-url http://127.0.0.1:8000 \
  --users 400 800 1600 3200 6400 \
  --rooms 100 --spawn-rate 800 --duration 12s --output results
```

Use longer stages and a separate load generator for release qualification. The short checked-in run is reproducible evidence for this machine, not a universal capacity rating.
