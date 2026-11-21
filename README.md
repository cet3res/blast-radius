# blast-radius

Entry for the RWX Blast Radius track of the Builders Buildathon 2026. For each patch to sqlglot (pinned at `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`), print which tests to run and how to split them across up to 8 workers.

Track page: https://www.joinbuilders.org/buildathon/tracks/blast-radius

## Interface

- Input: `/data/commits/<id>.patch` and `/data/tests.json`
- Output: one JSON line per commit on stdout, e.g. `{"commit": "k7q2", "workers": [[12, 845], [3]]}`
- `"workers": []` runs nothing
- Logs go to stderr only

## Scoring

Simulated CI seconds, lower is better: busiest worker seconds + 2 s per worker + 300 s per missed failing commit + CPU seconds. Baseline (every test on 3 workers) is 1614.1 s.

## Local run

1. Download `blast-radius-sample.tar.gz` from the track page
2. Extract only the sample data and scorer: `tar xzf blast-radius-sample.tar.gz sample score.py`
3. `make sample` builds the image, runs it under the scorer limits and scores the output
4. `make check` validates the output format only

## Status

- v0: starter selector, runs every test on 3 workers
- TODO: select tests from the patch, tune the worker count

## Code brought in

- Organizer starter kit: `Dockerfile`, `Makefile`, `selector.py`, `requirements.lock` (`score.py` and `sample/` are used locally and not committed)
- sqlglot at the pinned commit is the only other code involved

## License

MIT
