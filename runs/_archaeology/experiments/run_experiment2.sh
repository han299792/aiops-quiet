#!/bin/bash
# 실험 2: E_noreset 실행 스크립트
# reset 1회만 하고 incident 2회 연속 실행

echo "=========================================="
echo "실험 2: E_noreset 실행"
echo "=========================================="
echo ""

# 기본값 설정
PROBLEM_ID="${1:-pod_kill_hotel_res-detection-1}"
AGENT="${2:-gpt}"
RUNS="${3:-5}"

echo "Problem ID: $PROBLEM_ID"
echo "Agent: $AGENT"
echo "Runs: $RUNS"
echo ""

python3 experiments/reset_comparison_experiment.py \
    --problem-id "$PROBLEM_ID" \
    --agent "$AGENT" \
    --experiment e_noreset \
    --runs "$RUNS"

echo ""
echo "=========================================="
echo "실험 2 완료"
echo "=========================================="
