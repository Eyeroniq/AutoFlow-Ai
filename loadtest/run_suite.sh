#!/usr/bin/env bash
# Runs the whole load-test suite against the running Docker stack and saves every raw
# result under loadtest/results/<UTC timestamp>/. From the repo root:
#
#   bash loadtest/run_suite.sh
#
# Scenarios (Locust, from a container on the Compose network, so http://api:8000):
#   a-low-ocr      5 API users,  2 OCR runs in flight, worker-ocr x1, 2 min
#   b-high-ocr1   50 API users, 12 OCR runs in flight, worker-ocr x1, 2 min
#   b-high-ocr3   the same with worker-ocr x3
#   c-low-llm      5 API users,  2 LLM runs in flight (LOADTEST_LLM_PROVIDER), 1 min
# Then run throughput: THROUGHPUT_RUNS OCR runs queued at once, worker-ocr x1 vs x3.
set -euo pipefail
export MSYS_NO_PATHCONV=1  # Git Bash on Windows: don't rewrite /mnt/... arguments

cd "$(dirname "$0")/.."
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
OUT=loadtest/results/$STAMP
mkdir -p "$OUT"
RUNS=${THROUGHPUT_RUNS:-36}
DURATION=${SCENARIO_DURATION:-2m}

scale_ocr() {
  docker compose up -d --no-build --no-recreate --scale worker-ocr="$1" >/dev/null 2>&1
  for _ in $(seq 1 60); do
    [ "$(docker compose ps worker-ocr --format '{{.Health}}' | grep -c healthy)" = "$1" ] && return 0
    sleep 3
  done
  echo "worker-ocr didn't reach $1 healthy replicas" >&2
  return 1
}

scenario() {  # name users inflight mix duration
  echo "== $1: $2 users ($3 $4 runs in flight), $5" | tee -a "$OUT/summary.txt"
  LOADTEST_INFLIGHT=$3 LOADTEST_MIX=$4 docker compose --profile loadtest run --rm locust \
    --headless -u "$2" -r 10 -t "$5" --only-summary --csv "/mnt/loadtest/results/$STAMP/$1" \
    >"$OUT/$1.log" 2>&1
  sed -n '/Response time percentiles/,$p' "$OUT/$1.log" >>"$OUT/summary.txt"
}

{
  echo "started (UTC): $STAMP"
  echo "git: $(git rev-parse --short HEAD) (+ uncommitted changes: $(git status --porcelain | wc -l | tr -d ' ') files)"
  if command -v powershell.exe >/dev/null; then
    powershell.exe -NoProfile -Command '$c = Get-CimInstance Win32_Processor | Select-Object -First 1; $m = Get-CimInstance Win32_ComputerSystem; $o = Get-CimInstance Win32_OperatingSystem; "host: $($c.Name), $($c.NumberOfCores) cores / $($c.NumberOfLogicalProcessors) threads, {0:N1} GB RAM, $($o.Caption) $($o.Version)" -f ($m.TotalPhysicalMemory/1GB)' | tr -d '\r'
  else
    echo "host: $(uname -a)"
  fi
  docker info --format 'docker: {{.ServerVersion}}, VM {{.NCPU}} CPUs, {{.MemTotal}} bytes memory, {{.OperatingSystem}}'
  echo "api: 1 uvicorn process (the dev server with --reload); postgres 16, redis 7, all on the same machine"
  echo "workers: worker-default x1 (concurrency ${WORKER_DEFAULT_CONCURRENCY:-4}), worker-llm x1 (${WORKER_LLM_CONCURRENCY:-8}), worker-ocr x1/x3 (${WORKER_OCR_CONCURRENCY:-2} each, OMP_THREAD_LIMIT=1)"
  echo "ocr run: samples/scanned-invoice.pdf, 2 pages, 300 dpi, Tesseract eng"
  echo "llm run: ${LOADTEST_LLM_PROVIDER:-groq} default model, one short prompt"
  echo "executions in the database before: $(docker compose exec -T postgres psql -U flowforge -d flowforge -tAc 'select count(*) from workflow_executions')"
} | tee "$OUT/conditions.txt"

scale_ocr 1
scenario a-low-ocr 7 2 ocr "$DURATION"
scenario b-high-ocr1 62 12 ocr "$DURATION"
scale_ocr 3
scenario b-high-ocr3 62 12 ocr "$DURATION"
scale_ocr 1
scenario c-low-llm 7 2 llm 1m

sleep 20  # let the queues drain
for replicas in 1 3; do
  scale_ocr "$replicas"
  echo "== throughput: $RUNS OCR runs, worker-ocr x$replicas" | tee -a "$OUT/summary.txt"
  python loadtest/throughput.py --runs "$RUNS" --label "worker-ocr x$replicas" --out "$OUT/throughput.jsonl" \
    | tee -a "$OUT/summary.txt"
done
scale_ocr 1
echo "finished (UTC): $(date -u +%Y%m%dT%H%M%SZ)" | tee -a "$OUT/conditions.txt"
echo "results in $OUT"
