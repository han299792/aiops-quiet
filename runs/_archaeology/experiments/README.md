# Reset Comparison Experiment

이 실험은 reset 전략이 incident 복구 성능에 미치는 영향을 비교합니다.

## 실험 설계

### 조건 비교

- **E_single**: 각 run마다 reset 후 incident A 1회 실행
- **E_noreset**: reset 1회만 하고 incident A를 2회 연속 실행 (중간 reset 없음)

### 판정 기준

- **성공**: `p95 latency < L` AND `error_rate < e`가 연속 W초 유지
- **실패**: timeout(T초) 내 성공 조건 미달

### 기대 결과

- E_single에서는 성공률이 높음
- E_noreset에서는 2번째 A에서 실패(복구 실패/복구 지연 급증) 발생

## 사용법

### 실험 선택 실행

실험을 개별적으로 실행할 수 있습니다:

#### 실험 1만 실행 (E_single)

```bash
python3 experiments/reset_comparison_experiment.py \
    --problem-id pod_kill_hotel_res-detection-1 \
    --agent gpt \
    --experiment e_single \
    --runs 5
```

또는 쉘 스크립트 사용:

```bash
./experiments/run_experiment1.sh pod_kill_hotel_res-detection-1 gpt 5
```

#### 실험 2만 실행 (E_noreset)

```bash
python3 experiments/reset_comparison_experiment.py \
    --problem-id pod_kill_hotel_res-detection-1 \
    --agent gpt \
    --experiment e_noreset \
    --runs 5
```

또는 쉘 스크립트 사용:

```bash
./experiments/run_experiment2.sh pod_kill_hotel_res-detection-1 gpt 5
```

#### 두 실험 모두 실행

```bash
python3 experiments/reset_comparison_experiment.py \
    --problem-id pod_kill_hotel_res-detection-1 \
    --agent gpt \
    --experiment both \
    --runs 5
```

### 기본 실행 (두 실험 모두)

```bash
python3 experiments/reset_comparison_experiment.py \
    --problem-id pod_kill_hotel_res-detection-1 \
    --agent gpt \
    --runs 5
```

### 파라미터 조정

```bash
python3 experiments/reset_comparison_experiment.py \
    --problem-id pod_kill_hotel_res-detection-1 \
    --agent gpt \
    --runs 10 \
    --latency-threshold 500.0 \
    --error-rate-threshold 0.005 \
    --success-window 120 \
    --timeout 900 \
    --max-steps 30
```

### 파라미터 설명

- `--problem-id`: 실행할 문제 ID (예: `pod_kill_hotel_res-detection-1`, `network_delay_hotel_res-detection-1`)
- `--agent`: 사용할 agent 이름 (예: `gpt`, `react`)
- `--runs`: 각 조건당 실행 횟수 (기본값: 5)
- `--latency-threshold`: p95 latency 임계값 (ms, 기본값: 1000.0)
- `--error-rate-threshold`: Error rate 임계값 (기본값: 0.01 = 1%)
- `--success-window`: 연속 성공 유지 시간 (초, 기본값: 60)
- `--timeout`: 타임아웃 (초, 기본값: 600)
- `--max-steps`: Agent 최대 스텝 수 (기본값: 20)

## 결과

결과는 `experiments/results/` 디렉토리에 JSON 형식으로 저장됩니다.

### 결과 파일 형식

```json
{
  "config": {
    "problem_id": "...",
    "agent_name": "...",
    "latency_threshold": 1000.0,
    "error_rate_threshold": 0.01,
    ...
  },
  "results": [
    {
      "run_id": 1,
      "condition": "E_single",
      "incident_attempt": 1,
      "success": true,
      "recovery_time": 45.2,
      "final_latency_p95": 250.5,
      "final_error_rate": 0.001,
      "failure_reason": null,
      "timestamp": "2026-01-09T..."
    },
    ...
  ]
}
```

## 디버깅

### Pod 상태 확인

실험 중 pod가 Ready 상태가 되지 않는 경우, 다음 스크립트로 상태를 확인할 수 있습니다:

```bash
python3 experiments/debug_pods.py test-hotel-reservation
```

이 스크립트는 다음 정보를 출력합니다:

- 각 pod의 phase와 상태
- 컨테이너별 Ready 상태 및 대기/실행/종료 상태
- 컨테이너 재시작 횟수
- Pod conditions 및 events
- Ready 상태 요약

### 일반적인 문제 해결

1. **ImagePullBackOff**: 이미지 pull 실패

   - 이미지가 존재하는지 확인
   - 이미지 pull 정책 확인

2. **CrashLoopBackOff**: 컨테이너 반복 실패

   - Pod 로그 확인: `kubectl logs <pod-name> -n <namespace>`
   - 이전 컨테이너 로그: `kubectl logs <pod-name> -n <namespace> --previous`

3. **Pending**: Pod 스케줄링 실패

   - 리소스 부족 확인: `kubectl describe pod <pod-name> -n <namespace>`
   - Node 상태 확인: `kubectl get nodes`

4. **Init Container 실패**: Init 컨테이너가 완료되지 않음
   - Init 컨테이너 로그 확인

## 분석

실험 결과를 분석하여 다음을 확인할 수 있습니다:

1. E_single과 E_noreset의 성공률 비교
2. E_noreset에서 1번째와 2번째 incident의 성공률 차이
3. 복구 시간 비교
4. 실패 원인 분석
