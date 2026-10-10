# docs/img

`trace-waterfall.png` is the screenshot embedded in the README "Trace" section. Capture it from a running stack:

1. `make up` and `make index`, then send one full-pipeline query: `make query Q="How many cs.IR papers were published in 2025?"` for the SQL route, or a factoid question for the vector route.
2. Open Grafana at <http://localhost:3000> (anonymous admin access is enabled in the compose file).
3. Go to **Explore**, choose the **Tempo** data source, switch to **Search**, set service name to the Kotlin service and run the search. Pick a trace whose root is the `06-routing` request (it has `route`, `retrieve.vector`, `retrieve.bm25`, `fuse`, `rerank` and `generate` spans).
4. Capture the waterfall view with all six stage spans visible, crop to the waterfall and save it as `docs/img/trace-waterfall.png` (about 1400 px wide, under 300 KB).
