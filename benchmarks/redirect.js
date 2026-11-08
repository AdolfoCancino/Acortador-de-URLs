import http from 'k6/http';
import { check } from 'k6';
import { Trend, Rate } from 'k6/metrics';

const baseUrl = (__ENV.BASE_URL || 'http://localhost:8000').replace(/\/$/, '');
const apiKey = __ENV.API_KEY || 'dev-api-key';
const target = __ENV.TARGET_URL || 'https://example.com/';
const mode = __ENV.CACHE_MODE || 'warm';
const latency = new Trend('redirect_latency', true);
const failed = new Rate('redirect_failures');

export const options = {
  vus: Number(__ENV.VUS || 10),
  duration: __ENV.DURATION || '30s',
  discardResponseBodies: true,
  summaryTrendStats: ['avg', 'min', 'p(50)', 'p(95)', 'p(99)', 'max'],
  thresholds: {
    redirect_failures: ['rate==0'],
    checks: ['rate==1'],
  },
  tags: { cache_mode: mode },
};

export function setup() {
  if (!['warm', 'disabled'].includes(mode)) {
    throw new Error('CACHE_MODE must be warm or disabled; it labels the run, not server configuration.');
  }
  const response = http.post(`${baseUrl}/api/links`, JSON.stringify({ target_url: target }), {
    headers: { 'Content-Type': 'application/json', 'X-API-Key': apiKey },
    responseType: 'text',
    tags: { name: 'create_benchmark_link' },
  });
  if (response.status !== 201) {
    throw new Error(`Cannot create benchmark link: HTTP ${response.status}; ${response.body}`);
  }
  const code = response.json('code');
  if (!code) throw new Error('The create response is missing code');

  // Prime once outside measured iterations; with CACHE_TTL_SECONDS=0 every
  // measured request still goes to PostgreSQL.
  const prime = http.get(`${baseUrl}/${code}`, {
    redirects: 0,
    tags: { name: 'prime_benchmark_link' },
  });
  if (prime.status !== 302) throw new Error(`Prime failed: HTTP ${prime.status}`);
  return { code };
}

export default function (data) {
  const response = http.get(`${baseUrl}/${data.code}`, {
    redirects: 0,
    headers: { Referer: 'https://benchmark.example/run' },
    tags: { name: 'redirect' },
  });
  latency.add(response.timings.duration);
  const ok = check(response, {
    'redirect is HTTP 302': (r) => r.status === 302,
    'redirect has expected destination': (r) => r.headers.Location === target,
  });
  failed.add(!ok);
}

export function handleSummary(data) {
  const values = data.metrics.redirect_latency?.values;
  const count = data.metrics.iterations?.values.count || 0;
  const errors = data.metrics.redirect_failures?.values.rate;
  const report = {
    mode,
    base_url: baseUrl,
    vus: options.vus,
    duration: options.duration,
    redirects: count,
    measurement: count > 0 && values ? 'measured' : 'no_measured_requests',
    error_rate: count === 0 || errors === undefined ? null : errors,
    latency_ms: values
      ? { p50: values['p(50)'], p95: values['p(95)'], p99: values['p(99)'] }
      : null,
    k6: data,
  };
  return {
    stdout: `${JSON.stringify({ ...report, k6: undefined }, null, 2)}\n`,
    [__ENV.SUMMARY_PATH || `benchmarks/results/${mode}.json`]: JSON.stringify(report, null, 2),
  };
}
