# 발굴 기록 (archaeology)

2026-09-09 작성. 이 디렉토리는 **찾은 그대로** 보존한다. 고치지 않는다.

## 배경

NetAI 연구실에서 AIOpsLab + Chaos Mesh를 이미 돌린 적이 있으나 재현 가능한
기록이 남지 않았다. 이 프로젝트는 같은 실험을 버전 고정·사전등록·원자료 보존이
되는 형태로 다시 구성하는 것이고, 착수 전에 남아 있는 것을 먼저 찾았다.

## 이 노트북에서 찾은 것

| 확인 항목 | 결과 |
|---|---|
| 다른 AIOpsLab 클론 | 없음. 이 클론 하나뿐 |
| `data/results/` 세션 JSON | **없음** |
| kubeconfig (`~/.kube`) | **없음** — 이 노트북은 클러스터에 붙은 적이 없다 |
| 이전 실험 코드 | `experiments/` (git untracked, ~1.2k LOC, 한국어 주석) |

원래 돌렸던 클러스터·노드 쪽 발굴(`helm list -A`, 잔존 네임스페이스, chaos CR,
Prometheus 보존 기간 내 메트릭, 노드의 `~/.bash_history`)은 **아직 하지 않았다.**
연구실 머신에 처음 접속할 때 수행하고 결과를 여기에 추가한다.

## 판정: 이 코드는 동작한 적이 없다

`experiments/`는 reset 전략 비교 실험(E_single vs E_noreset)의 하네스다.
설계 문서(`experiments/README.md`)는 멀쩡하지만, 코드는 두 가지 이유로
**한 번도 성공적으로 실행된 적이 없다.**

### 1. import 단계에서 죽는다

`reset_comparison_experiment.py:26`:

```python
from aiopslab.clients.registry import AgentRegistry
```

`aiopslab/clients/`라는 패키지는 존재하지 않는다. 실제 경로는 레포 루트의
`clients/registry.py`다. 따라서 이 모듈은 import 자체가 `ModuleNotFoundError`로
실패하며, 이를 import하는 `find_suitable_problem.py`도 함께 죽는다.

### 2. 설령 import가 됐어도 판정 함수가 항상 실패를 반환한다

`MetricMonitor.check_success_condition`이 던지는 PromQL:

```promql
histogram_quantile(0.95, sum(rate(istio_request_duration_milliseconds_bucket{namespace="…"}[1m])) by (le))
sum(rate(istio_requests_total{namespace="…",response_code=~"5.."}[1m])) / sum(rate(istio_requests_total{namespace="…"}[1m]))
```

**이 스택에는 Istio가 없다.** AIOpsLab이 배포하는 Prometheus의 스크레이프 잡은
cAdvisor + node-exporter + prometheus 자신뿐이고(`aiopslab/observer/prometheus/prometheus/values.yaml`),
`aiopslab/observer/metric_api.py`의 `istio_metrics` 목록도 정의만 되어 있을 뿐
`export_all_metrics`의 istio 블록은 통째로 주석 처리되어 있다.

따라서 두 쿼리 모두 빈 결과를 반환하고, `check_success_condition`은
`p95_latency is None or error_rate is None` 분기를 타서 **항상
`(False, None, None)`** 을 돌려준다. 즉 모든 run이 무조건 "실패"로 기록된다.

## 이 발견이 바꾸는 것

서술이 한 단계 정확해진다. "기록을 안 남겼다"가 아니라
**"측정이 성립하지 않는 상태였고, 그걸 몰랐다."**

이건 더 정직하고, 이번 설계의 직접적인 동기다:

- **널 대조군이 필요한 이유** — 장애가 없을 때 측정기가 무엇을 반환하는지
  먼저 확인했다면, 위 두 쿼리가 항상 빈 결과라는 걸 첫날에 알았을 것이다.
- **측정기를 먼저 검증해야 하는 이유** — 시끄러운 장애(`pod_kill`)에서
  측정기가 반응하는지 확인하는 절차가 있었다면 마찬가지다.
- **재현성이 부산물이 아니라 목적인 이유** — 원자료가 남아 있었다면
  이 진단을 코드를 읽지 않고 데이터로 할 수 있었다.

## 남겨두는 이유

고쳐서 쓰지 않고 그대로 둔다. 이 디렉토리는 결과물이 아니라 **증거**다.
새 하네스는 이 레포(`aiops-quiet`)에 따로 쓴다.

다만 재사용할 만한 것은 있다:

- `debug_pods.py` / `debug_deployment.py` — 네임스페이스 파드 상태·이벤트 덤프.
  리셋 검증(`harness/reset.py`)의 참고가 된다.
- `check_path.py` — `TARGET_MICROSERVICES` / `K8S Deploy Path` 해석 검증.
- `README.md`의 실험 설계(성공 조건을 "연속 W초 유지"로 잡은 것)는 그대로 쓸 만하다.
