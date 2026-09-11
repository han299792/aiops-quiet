# aiops-quiet — 조용한 장애는 텔레메트리에 얼마나 적혀 있나

AIOps 에이전트가 장애를 놓쳤을 때, 원인은 셋 중 하나다.

1. **데이터에 답이 없다** — 텔레메트리에 신호 자체가 없다
2. **축약이 답을 지웠다** — 원본엔 있으나 에이전트가 받는 뷰엔 없다
3. **보고도 못 알아봤다** — 신호가 컨텍스트에 들어왔는데 결론이 틀렸다

벤치마크 점수는 이 셋을 구분하지 않는다. 이 레포는 **1번을 따로 재는 자(尺)** 다.

> 벤치마크 점수는 남들도 낸다. **벤치마크 자체를 재는 건 거의 없다.**

## 무엇을 재나

장애를 **알려진 크기로** 주입하고, 그 장애가 텔레메트리에 얼마나 명시적으로
나타나는지를 채널별 0/1로 채점한다. 점수 0~3.

| 채널 | 통계량 |
|---|---|
| 이벤트 | 경고 이벤트 율비 z · 컨테이너 재시작 델타 · 파드 교체 |
| 로그 | ERROR급 줄 비율의 2표본 비율 z (파드별 최댓값) |
| 메트릭 | 에러 span 비율 z · 지연 Brunner–Munzel · 자원 Welch t |

임계값은 **고르지 않는다.** sham 대조군의 귀무분포에서 Bonferroni 보정된
분위수로 뽑는다 (`quiet/probe/calibrate.py`).

## 왜 sham 대조군인가

`OtelFaultInjector`는 주입 끝에 `flagd`를 rollout restart 한다. 반면
`NoopFaultInjector.inject_no_op`은 문자 그대로 `pass`다.

그래서 noop을 0% 대조군으로 쓰면 **이벤트 채널이 10~100%에서 전부 1, 0%만 0**이
나온다 — 장애 때문이 아니라 **주입 행위가 남긴 파드 교체** 때문에. 완벽하게 생긴
용량-반응 계단이 나오는데 전부 인공물이다.

0% 조건은 `variant="off"`로 **같은 configmap 쓰기와 같은 rollout restart를 수행**한다.
행동적 효과만 0이다. 그래야 주입 도구의 흔적이 귀무분포에 들어가 상쇄된다.

## 지금 상태 — 정직하게

| | |
|---|---|
| 측정기 | **완성.** 176개 테스트, 클러스터·네트워크·API키 없이 통과 |
| 실행 | **0회.** `runs/`에 `_archaeology/`밖에 없다 |
| 테스트 | 전부 **합성 픽스처** 기준 (`tests/synth.py`) |
| 창 길이 | **아직 미확정** — 아래 참조 |

**"측정기를 만들었다"는 참이고 "실험을 수행했다"는 거짓이다.**

## 검정력 분석이 먼저 알려준 것

클러스터에 가기 전에 합성 시뮬레이션을 돌렸고(`POWER.md`), 결론은
**창 길이를 아직 확정할 수 없다** 였다. 곡선의 모양이 측정되지 않은 두 값에 좌우된다:

- `charge_fraction` (payment span 중 결제 요청 비율) — 무릎이 **10~25% 사이**.
  25%면 10% 용량도 이미 명시적이고, 5%면 100% 용량조차 조용하다
- 기저 에러율 — 5%를 넘으면 10% 용량이 묻힌다

그래서 `PREREG.md`는 창 길이 확정을 **측정 이후로 미뤘다.** 한 푼 쓰기 전에 알아낸 것이다.

## 쓰는 법

```bash
git clone --recurse-submodules https://github.com/han299792/aiops-quiet.git
cd aiops-quiet
python3.11 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest tests -q          # 176, 클러스터 불필요

# 클러스터가 있다면 — 먼저 2분짜리 사전 점검
.venv/bin/python -m quiet.harness.preflight --namespace astronomy-shop

# 통과하면 캡처 (LLM 비용 0)
.venv/bin/python -m quiet.harness.run_probe payment_dose_100-detection-1 \
    --window 1800 --warmup 300 --settle 90
```

`vendor/AIOpsLab`은 [내 포크](https://github.com/han299792/AIOpsLab)를 커밋으로 핀한
서브모듈이다. 다른 체크아웃을 쓰려면 `AIOPSLAB_ROOT`로 덮어쓴다.

## 설계 — 왜 3단계인가

```
sources/  →  RawWindow        원본 payload 그대로      [클러스터 필요]
parse.py  →  WindowSnapshot   통계 요약                [순수]
score.py  →  Verdict          채널별 0/1               [순수]
```

원본을 **한 번만** 뜨면 파싱과 채점을 노트북에서 무한히 고칠 수 있고, 파서 버그를
고치면 과거 회차 전부에 소급 적용된다. span을 개별 타임스탬프와 함께 보관하는 이유도
같다 — 긴 창 하나를 뜬 뒤 **오프라인에서 잘라** 짧은 창을 만들 수 있다.
명시성은 "장애 크기 × 관측량"의 함수라 창 길이가 두 번째 축이고, 그 축을
클러스터 비용 0으로 훑는다.

`parse`/`score`/`calibrate`/`leak`은 `aiopslab`을 **절대 import 하지 않는다.**
`tests/test_import_purity.py`가 서브프로세스로 강제한다 — `aiopslab.paths`가 import
시점에 gitignore된 config를 읽고, `aiopslab.observer`가 kubeconfig를 로드하고,
`AstronomyShop.__init__`이 네임스페이스를 만들기 때문이다.

## 하는 김에 찾은 벤치마크 결함

`UPSTREAM.md`에 재현 방법과 함께 정리. 요지:

- **`TraceAPI.get_traces`가 `end_time`을 받아서 버린다.** `lookback = now - start_time`으로만
  조회하므로 **이미 끝난 시간창을 가져올 수 없다** → 정상 vs 장애 비교가 원천적으로 불가능
- **LLM 캐시 키에 모델명이 없다** → 한 모델로 채운 캐시가 다른 모델에 답을 배달한다
- **`PrometheusAPI`가 forward한 포트와 다른 포트에 질의한다**
- **원격 Helm 차트에 버전 핀이 없다** → vendor된 0.37.2가 아니라 최신(0.41.0)이 설치된다
- **에이전트가 `exec_shell`로 정답 configmap을 직접 읽을 수 있다** (측정 예정, `quiet/harness/leak.py`)

앞의 넷은 `vendor/AIOpsLab`에서 고쳤고 업스트림 PR 후보다.

## 문서

| | |
|---|---|
| `PREREG.md` | 사전등록 — 가설·대조군·분석 규칙. **데이터 보기 전 커밋** |
| `POWER.md` | 검정력 분석과 그 결론(창 길이 확정 보류) |
| `RUNBOOK.md` | 클러스터 첫 세션 절차 |
| `UPSTREAM.md` | 벤치마크 결함, 재현 방법 포함 |
| `runs/_archaeology/FINDINGS.md` | 이전 실험이 왜 동작한 적 없는지 |
