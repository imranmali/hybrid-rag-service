// k6 load test for POST /v1/query. Three scenarios run one after another (SCENARIO=<name> keeps one):
//   retrieval  grid row 4, generation off   ramp 1 -> 20 rps over 30 s, hold 20 rps for 60 s
//   rerank     grid row 5, generation off   0.5 rps for 120 s
//   full       grid row 6, generation on    0.05 rps for 200 s
// 20 rps matches the HikariCP reader-pool sizing rationale (add-query-service design D3).
// The rerank and full rates are bounded by local CPU/LLM speed on the dev machine, so the thresholds
// below are this machine's budget, not a portable SLO. Load tests do not run in CI (only `k6 inspect`).
//
// Run: make load                       all scenarios; writes load/results/summary-all-<timestamp>.json
//      SCENARIO=retrieval make load     one scenario (retrieval | rerank | full)
import http from 'k6/http';
import exec from 'k6/execution';
import { check } from 'k6';
import { SharedArray } from 'k6/data';
import { Rate, Trend } from 'k6/metrics';
import { textSummary } from 'https://jslib.k6.io/k6-summary/0.1.0/index.js';

const TARGET = (__ENV.K6_TARGET || 'http://localhost:8080').replace(/\/+$/, '');
const ONLY = __ENV.SCENARIO || '';
// RETRIEVAL_PEAK_RPS lowers the retrieval scenario's peak rate (default 20) to find what the stack sustains.
const PEAK_RPS = Number(__ENV.RETRIEVAL_PEAK_RPS || 20);
const QUESTION_OFFSET = 42; // fixed offset into the dataset so the question order is deterministic

// ---------------------------------------------------------------------------------------------
// Pipelines, copied literally from the eval configs (contract C7). generation is switched off for
// the first two (retrieval and rerank latency are what those scenarios measure).
// A drift check runs in CI: load/check_pipelines.py compares these against evals/configs/*.yaml.
// ---------------------------------------------------------------------------------------------
// source: evals/configs/04-contextual.yaml (pipeline), generation.enabled overridden to false
const PIPELINE_RETRIEVAL = {
  retrieval: { vector: true, bm25: true, vector_k: 50, bm25_k: 50, fusion: { method: 'rrf', k: 60 }, return_k: 50 },
  contextual: true,
  filters: { enabled: true, extract_from_question: true, strategy: 'auto', selectivity_threshold: 0.25, overfetch_factor: 5, explicit: null },
  rerank: { enabled: false, candidates: 20 },
  routing: { enabled: false },
  generation: { enabled: false, context_k: 6 },
};
// source: evals/configs/05-rerank.yaml (pipeline), generation.enabled overridden to false
const PIPELINE_RERANK = {
  retrieval: { vector: true, bm25: true, vector_k: 50, bm25_k: 50, fusion: { method: 'rrf', k: 60 }, return_k: 50 },
  contextual: true,
  filters: { enabled: true, extract_from_question: true, strategy: 'auto', selectivity_threshold: 0.25, overfetch_factor: 5, explicit: null },
  rerank: { enabled: true, candidates: 20 },
  routing: { enabled: false },
  generation: { enabled: false, context_k: 6 },
};
// source: evals/configs/06-routing.yaml (pipeline), unchanged
const PIPELINE_FULL = {
  retrieval: { vector: true, bm25: true, vector_k: 50, bm25_k: 50, fusion: { method: 'rrf', k: 60 }, return_k: 50 },
  contextual: true,
  filters: { enabled: true, extract_from_question: true, strategy: 'auto', selectivity_threshold: 0.25, overfetch_factor: 5, explicit: null },
  rerank: { enabled: true, candidates: 20 },
  routing: { enabled: true },
  generation: { enabled: true, context_k: 6 },
};
const PIPELINES = { retrieval: PIPELINE_RETRIEVAL, rerank: PIPELINE_RERANK, full: PIPELINE_FULL };

// ---------------------------------------------------------------------------------------------
// Questions: evals/dataset/questions.v1.jsonl, mounted at /dataset by `make load`.
// ---------------------------------------------------------------------------------------------
function readDataset() {
  for (const path of ['/dataset/questions.v1.jsonl', '../evals/dataset/questions.v1.jsonl']) {
    try {
      return open(path);
    } catch (e) {
      // try the next location
    }
  }
  return ''; // `k6 inspect` (CI lint) has no dataset; setup() aborts real runs that have none
}

const questions = new SharedArray('questions', function () {
  return readDataset()
    .split('\n')
    .filter((line) => line.trim() !== '')
    .map((line) => JSON.parse(line).question);
});

export function setup() {
  if (questions.length === 0) {
    exec.test.abort('questions.v1.jsonl not found: mount evals/dataset at /dataset (make load does this)');
  }
}

// ---------------------------------------------------------------------------------------------
// Scenarios and thresholds
// ---------------------------------------------------------------------------------------------
const ALL_SCENARIOS = {
  retrieval: {
    executor: 'ramping-arrival-rate',
    exec: 'retrieval',
    startTime: '0s',
    startRate: 1,
    timeUnit: '1s',
    stages: [
      { target: PEAK_RPS, duration: '30s' },
      { target: PEAK_RPS, duration: '60s' },
    ],
    preAllocatedVUs: 40,
    maxVUs: 80,
    gracefulStop: '30s',
  },
  rerank: {
    executor: 'constant-arrival-rate',
    exec: 'rerank',
    startTime: '105s', // retrieval ends at 90 s (+15 s gap)
    rate: 1,
    timeUnit: '2s', // 0.5 rps
    duration: '120s',
    preAllocatedVUs: 4,
    maxVUs: 20,
    gracefulStop: '30s',
  },
  full: {
    executor: 'constant-arrival-rate',
    exec: 'full',
    startTime: '240s', // rerank ends at 225 s (+15 s gap)
    rate: 1,
    timeUnit: '20s', // 0.05 rps
    duration: '200s',
    preAllocatedVUs: 4,
    maxVUs: 10,
    gracefulStop: '70s',
  },
};
const ALL_THRESHOLDS = {
  retrieval: { 'http_req_duration{scenario:retrieval}': ['p(95)<500'] },
  rerank: { 'http_req_duration{scenario:rerank}': ['p(95)<12000'] },
  full: { 'http_req_duration{scenario:full}': ['p(95)<60000'] },
};

if (ONLY !== '' && !(ONLY in ALL_SCENARIOS)) {
  throw new Error(`unknown SCENARIO '${ONLY}'; expected one of: ${Object.keys(ALL_SCENARIOS).join(', ')}`);
}
const selected = Object.keys(ALL_SCENARIOS).filter((name) => ONLY === '' || ONLY === name);

const scenarios = {};
const thresholds = { http_req_failed: ['rate<0.01'] };
for (const name of selected) {
  scenarios[name] = ALL_SCENARIOS[name];
  Object.assign(thresholds, ALL_THRESHOLDS[name]);
}

export const options = {
  scenarios,
  thresholds,
  summaryTrendStats: ['avg', 'min', 'med', 'p(90)', 'p(95)', 'max'],
  // Free-form key: `k6 inspect` prints it, so load/check_pipelines.py can compare the request
  // pipelines with the eval configs without running the load.
  ext: { hybridrag: { pipelines: PIPELINES } },
};

// ---------------------------------------------------------------------------------------------
// Metrics
// ---------------------------------------------------------------------------------------------
const degraded = new Rate('rag_degraded'); // share of responses with a non-empty `degraded` list
const STAGES = [
  'route', 'sql.generate', 'sql.execute', 'filter.estimate', 'embed',
  'retrieve.vector', 'retrieve.bm25', 'fuse', 'rerank', 'generate', 'total',
];
const stageTrends = {};
for (const key of STAGES) {
  stageTrends[key] = new Trend(`rag_stage_${key.replace(/\./g, '_')}`, true);
}

function send(pipeline, timeout) {
  const question = questions[(exec.scenario.iterationInTest + QUESTION_OFFSET) % questions.length];
  const res = http.post(`${TARGET}/v1/query`, JSON.stringify({ question, pipeline }), {
    headers: { 'Content-Type': 'application/json' },
    timeout,
    tags: { name: 'POST /v1/query' },
  });
  const ok = check(res, { 'status is 200': (r) => r.status === 200 });
  if (!ok) {
    return;
  }
  let body;
  try {
    body = res.json();
  } catch (e) {
    return;
  }
  degraded.add(Array.isArray(body.degraded) && body.degraded.length > 0);
  const timings = body.timings_ms || {};
  for (const key of STAGES) {
    if (typeof timings[key] === 'number') {
      stageTrends[key].add(timings[key]);
    }
  }
}

export function retrieval() {
  send(PIPELINE_RETRIEVAL, '10s');
}

export function rerank() {
  send(PIPELINE_RERANK, '30s');
}

export function full() {
  send(PIPELINE_FULL, '120s');
}

export function handleSummary(data) {
  const label = ONLY === '' ? 'all' : ONLY;
  const stamp = new Date().toISOString().replace(/\.\d+Z$/, 'Z').replace(/:/g, '-');
  return {
    [`/load/results/summary-${label}-${stamp}.json`]: JSON.stringify(data, null, 2),
    stdout: textSummary(data, { indent: ' ', enableColors: false }),
  };
}
