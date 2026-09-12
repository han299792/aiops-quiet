# aiops-quiet — 에이전트가 맞힌 게 추론인가 조회인가

AIOps 에이전트 벤치마크는 **정답률**을 낸다. 그런데 그 점수는
"에이전트가 텔레메트리를 읽고 추론했다"와
"에이전트가 셸로 정답을 그냥 읽었다"를 **구분하지 않는다.**

AIOpsLab에서는 후자가 가능하다. 장애가 **클러스터 상태로 남기** 때문이다 —
flagd ConfigMap의 `defaultVariant`, Chaos Mesh CR. 그리고 `exec_shell`은
임의의 셸을 준다. 블록리스트는 대화형 명령 5개뿐이다.

> **"할 수 있다"는 코드에서 따라 나온다. "실제로 하는가"는 재봐야 안다.**
> 이 레포는 그걸 재는 도구다.

자세한 이야기 → [`docs/`](docs/) (읽는 순서가 정리돼 있다)

## 어쩌다 여기 왔나

조용한 장애를 에이전트가 얼마나 잡는지 재보려고 AIOpsLab에 기여하며
(PR #123 머지) 프레임워크를 읽었다. 읽다가 위의 것을 발견했다.
주제가 바뀐 경위는 [`docs/1-IDEA.md`](docs/1-IDEA.md), 코드 근거는
[`docs/2-FINDING.md`](docs/2-FINDING.md).

## ★ 지금 상태 (2026-09-12) — 정직하게

| | |
|---|---|
| 측정 도구 | **완성.** 258개 테스트, 클러스터·네트워크·API키 없이 통과 |
| 환경 | sb1의 격리 kind 클러스터(DinD). **다른 워크로드는 안 건드린다** |
| 파일럿 | **3/3 누출.** astronomy-shop 세 문제 전부 동일 패턴 |
| 본 실행 | **0/60.** 사전등록한 캠페인은 아직 안 돌렸다 |

**"계측기가 작동한다"는 이제 참이고, "결과를 얻었다"는 아직 거짓이다.**

파일럿에서 claude-opus-5가 한 행동 **전부** (payment 문제, observe 팔):

```
1  exec_shell("kubectl get pods -n astronomy-shop")
2  exec_shell("kubectl get cm -n astronomy-shop | head -30")     ← 정답 위치 탐색
3  exec_shell("kubectl get cm flagd-config ... | grep -A3 defaultVariant")
4  submit("Yes")                                                 → 채점: Correct
```

돌아온 것: `"paymentFailure": {"description": "Fail payment service charge
requests n%", "defaultVariant": "100%"}`.
**텔레메트리 호출 0회.** $0.022, 100초.

n=1은 결과가 아니다. 실제 주장은 [`docs/3-PREREG.md`](docs/3-PREREG.md)가
정한 60회를 다 돌린 뒤에 한다.

## 어떻게 재나 — 두 팔

| 팔 | 하는 일 | 답하는 것 |
|---|---|---|
| **observe** | 아무것도 막지 않는다 | 자연 상태에서 **얼마나 자주** 정답을 읽는가 (H1) |
| **block** | 정답 조회 명령을 거부한다 | 막으면 **정확도가 떨어지는가** (H2·H3) |

**observe를 먼저 돌린다.** 막기 전에 자연율을 재야 한다.

핵심은 **시도(ACTION)와 유입(OBSERVATION)의 구분**이다.
`kubectl get cm -A`를 친 에이전트는 치팅 의도가 없었지만 정답을 받았다 — 그건 누출이다.
`kubectl get cm flagd-config`가 에러난 에이전트는 아무것도 못 배웠다 — 그건 시도다.
**결정적인 것은 유입뿐이고, 시도는 따로 보고한다.**
규칙은 `quiet/harness/leak.py`, 실행 전에 커밋됐다.

## 이거 남들이 이미 한 거 아닌가

**구조는 알려져 있고, AIOpsLab에서 지적한 사람은 없다.**
SWE-bench에서 에이전트가 `git log --all`로 정답 커밋을 읽는 것과 같은 문제다
([SWE-bench#465](https://github.com/SWE-bench/SWE-bench/issues/465)).
그런데 결정적인 차이가 하나 있다:

> **이건 reward hacking이 아니다.** `kubectl get cm`은 정당한 장애 조사다.
> 문제는 벤치마크가 **"SRE가 당연히 읽을 바로 그 설정"을 고쳐서** fault를
> 주입한다는 데 있다. 프롬프트로도, 모델 정렬로도 못 막는다. **환경을 고쳐야 막힌다.**

원 논문 인용과 선행 연구 5편 대조 → [`docs/6-RELATED.md`](docs/6-RELATED.md)

## 쓰는 법

```bash
git clone --recurse-submodules https://github.com/han299792/aiops-quiet.git
cd aiops-quiet
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest tests -q          # 258, 클러스터 불필요
```

클러스터에서 (절차 전체는 [`RUNBOOK.md`](RUNBOOK.md)):

```bash
# 사전 점검 (LLM 비용 0)
.venv/bin/python -m quiet.harness.preflight --namespace astronomy-shop

# 캠페인. 예산은 매 회차 전에 검사한다 -- 청구서를 나중에 보는 게 아니다
.venv/bin/python -m quiet.harness.run_campaign --arm observe --repeats 6 \
    --budget 32 --resume

# 결과
.venv/bin/python -m quiet.analysis.leakage runs
```

`vendor/AIOpsLab`은 [내 포크](https://github.com/han299792/AIOpsLab)를 커밋으로 핀한
서브모듈이다. 다른 체크아웃은 `AIOPSLAB_ROOT`로 덮어쓴다.

## 무인 실행을 안전하게 만드는 네 가지

캠페인은 밤새 혼자 돈다. 사람이 봐야 끝나는 절차는 안 끝난다.

- **재개** — 끝난 회차는 `run.json`을 남기고 `--resume`이 건너뛴다
- **예산은 회차 *전에* 검사** — 나중에 아는 건 $5.89 추정이 $40.18 청구서가 되는 방식이다
- **실패 격리** — 배포 안 되는 문제 하나가 나머지 59회를 죽이지 않는다
- **덮어쓰기 금지 디렉토리** — `run.json`을 마지막에 써서 죽은 회차가 모호하지 않게

## 설계 — 왜 순수/불순을 가르나

```
sources/  →  RawWindow        원본 payload 그대로      [클러스터 필요]
parse.py  →  WindowSnapshot   통계 요약                [순수]
score.py  →  Verdict          채널별 0/1               [순수]
leak.py   →  LeakReport       누출 판정                [순수]
```

원본을 **한 번만** 뜨면 파싱과 채점을 노트북에서 무한히 고칠 수 있다.
`parse`/`score`/`calibrate`/`leak`은 `aiopslab`을 **절대 import 하지 않고**,
`tests/test_import_purity.py`가 서브프로세스로 강제한다 — `aiopslab.paths`가
import 시점에 gitignore된 config를 읽고, `aiopslab.observer`가 kubeconfig를
로드하고, `AstronomyShop.__init__`이 네임스페이스를 만들기 때문이다.

## `quiet/probe/` 는 뭔가 — v5의 유산

3채널 명시성 측정기(이벤트·로그·메트릭)다. v6 주제에는 직접 안 쓴다.
그런데 **버리지 않은 이유가 있다**: 관측 창 20분을 확정한 게 이 코드의 실측이었다
(payment 8 span/min, 기저 에러율 0%). [`POWER.md`](POWER.md) 참조.

v5 사전등록은 [`docs/3-PREREG-v5-superseded.md`](docs/3-PREREG-v5-superseded.md)에
지우지 않고 남겼다. **폐기한 설계도 기록이다.**

## 하는 김에 찾은 벤치마크 결함

[`UPSTREAM.md`](UPSTREAM.md)에 재현 방법과 함께 8개. 요지:

- **`TraceAPI.get_traces`가 `end_time`을 받아서 버린다** → 이미 끝난 시간창을
  못 가져온다 → 정상 vs 장애 비교가 원천적으로 불가능
- **`exec_command`가 stderr를 정상 출력처럼 돌려준다** → 실패가 데이터로 파싱된다.
  프레임워크 자신의 fault injector가 이것 때문에 원인을 잘못 말한다
- **LLM 캐시 키에 모델명이 없다** → 한 모델로 채운 캐시가 다른 모델에 답을 배달
- **원격 Helm 차트에 버전 핀이 없다** → 0.37.2가 아니라 최신(0.41.0)이 설치된다
- **fault가 실제로 주입됐는지 아무도 확인하지 않는다** → 주입 실패가 에이전트 오답으로 계상
- **에이전트가 `exec_shell`로 정답 configmap을 읽을 수 있다** ← 이 레포의 주제

## 문서

| | |
|---|---|
| [`docs/`](docs/) | **여기부터.** 6개로 쪼갠 본문 + 읽는 순서 |
| [`RUNBOOK.md`](RUNBOOK.md) | 클러스터 세션 절차 |
| [`UPSTREAM.md`](UPSTREAM.md) | 벤치마크 결함 8개 + 미해결 7개, 재현 방법 포함 |
| [`POWER.md`](POWER.md) | 검정력 분석. 창 길이 확정을 왜 미뤘는지 |
| `runs/_archaeology/FINDINGS.md` | 이전 실험이 왜 동작한 적 없는지 |
