#!/bin/bash
# 실험 1: E_single 실행 스크립트
# 각 run마다 reset 후 incident 1회 실행

echo "=========================================="
echo "실험 1: E_single 실행"
echo "=========================================="
echo ""
a
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
    --experiment e_single \
    --runs "$RUNS"

echo ""
echo "=========================================="
echo "실험 1 완료"
echo "=========================================="
