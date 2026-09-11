# 4. 실행 계획 — 어떻게 만들고 어떻게 돌리나

> 원칙 두 개.
> **(1) 클러스터 없이 되는 건 전부 먼저 한다.** 클러스터 시간은 비싸고 일정에 안 잡힌다.
> **(2) 실험은 무인으로 돈다.** 사람이 붙어 있어야 하는 절차는 안 끝난다.

---

## 4.1 지금 어디까지 돼 있나

| | 상태 |
|---|---|
| 누출 판정기 `quiet/harness/leak.py` | ✅ 완성. 시도/유입 분리, 테스트 16개 |
| 차단 함수 `blocks_action` | ✅ 있음. 다만 **실행 루프에 연결 안 됨** |
| write-once 실행 디렉토리 `rundir.py` | ✅ 완성. 완료 표시 = 중단/재개 가능 |
| 재현성 지문 `manifest.py` | ✅ 완성. 이미지 digest 포함 |
| 명시성 측정기 `quiet/probe/` | ✅ 완성 (보조로 사용) |
| **에이전트 클라이언트** | ❌ **없음.** 레포에 `anthropic` 의존성조차 없다 |
| **에이전트 실행 루프** | ❌ **없음.** `run_probe.py`는 프로브 전용 |
| **비용 추적** | ❌ **없음.** 업스트림이 `response.usage`를 버린다 |

테스트 176개가 클러스터·네트워크·API키 없이 통과한다.

## 4.2 D1 — 에이전트 클라이언트 (클러스터 불필요)

`vendor/AIOpsLab/clients/claude.py`. **업스트림 PR 후보이기도 하다** —
레포에 OpenAI·Azure·DeepSeek·Qwen·vLLM·OpenRouter·Groq는 있는데 Anthropic만 없다.

기존 클라이언트 관례를 그대로 따른다 (`__init__` / `init_context` / `async get_action`).
다만 셋을 더한다:

| 추가 | 왜 |
|---|---|
| `response.usage` 캡처 | 업스트림은 전부 버린다. 예산 상한이 작동하려면 필요 |
| `temperature=0` | 반복의 분산이 표집잡음이 아니라 환경잡음에서만 오게 |
| 캐시 기본 OFF | 업스트림 캐시는 키에 모델명이 없고, 1스텝이 항상 히트라 반복이 독립이 아니게 된다 |

**테스트는 녹화된 응답으로 한다.** API 호출 0, 키 불필요.

## 4.3 D2 — 캠페인 러너 (클러스터 불필요)

`quiet/harness/run_campaign.py`. **명령 하나로 60회가 무인으로 돈다.**

```bash
python -m quiet.harness.run_campaign --arm observe --repeats 6 --resume
```

루프 한 회차:

```
1. 리셋 (kind delete/create 또는 앱 재배포)
2. 리셋 검증 — 파드 Ready / restart 0 / chaos CR 0 / 플래그 전부 off
     실패 → discarded.jsonl 에 기록하고 재시도. N 에 산입하지 않음
3. 예산 확인 — 남은 예산 < 1회분이면 중단하고 거기까지를 결과로
4. 문제 초기화 + 에이전트 실행 (max_steps)
     block 팔이면 액션을 실행 전에 가로챔
5. 트레이스 누출 판정 → leak.json
6. write-once 디렉토리에 저장. run.json 을 마지막에 (완료 표시)
```

무인 운전에 필요한 네 가지:

| | 무엇 | 왜 |
|---|---|---|
| **재개** | `--resume`이 `run.json` 있는 회차를 건너뜀 | 밤새 돌다 끊겨도 이어서 |
| **예산 중단** | 매 회차 **전에** 확인 | 사후에 알면 늦다 ($5.89 → $40.18을 겪었다) |
| **실패 격리** | 한 문제가 안 뜨면 기록하고 다음으로 | 한 칸 때문에 59회를 잃지 않게 |
| **원자적 쓰기** | tmp + replace | 중간에 죽어도 반쯤 쓴 파일이 안 남게 |

## 4.4 D3 — 문서 정리 (클러스터 불필요)

`README.md`·`RUNBOOK.md`가 아직 옛 주제(명시성 카탈로그)를 설명한다. v6로 맞춘다.

## 4.5 D4~D7 — sb1

### D4. 격리 환경 (반나절)

**다른 건 절대 안 건드린다.** sb1의 도커 위에 `kind`를 따로 띄운다.
kind 클러스터는 sb1의 도커 컨테이너라 기존 k8s API 서버가 존재를 모른다 —
CRD·웹훅·DaemonSet·StorageClass가 전부 그 안에 갇히고, `kind delete cluster` 한 줄로 사라진다.

**왜 네임스페이스 격리로 안 되나**: 오케스트레이터가 회차마다
`kubectl patch storageclass openebs-hostpath ... is-default-class: true` 를 하고
끝에 `kubectl delete sc openebs-hostpath openebs-device` 를 한다.
**클러스터 기본 StorageClass를 지웠다 만든다.** 60회면 60번이다.

사전 확인 (읽기만):
```bash
docker info >/dev/null 2>&1 && echo "docker OK" || echo "DinD 파드 필요"
free -g | awk '/Mem:/{print "여유 램:", $7"GB"}'    # 16GB 이상
df -h /var/lib/docker | tail -1                      # 50GB 이상
```

검증: `noop_detection_astronomy_shop-1` 1회 완주. **장애 주입을 빼고 배포 경로만**
먼저 확인한다 — 실패했을 때 원인이 갈리지 않게.

### D5. 파일럿 (2~3시간)

5문제 × 1회, observe 팔만. 목적은 결과가 아니라 **실측**:

- 회차당 토큰 → 총비용 추정 × 1.5 → **$40 넘으면 시작 전에 조건을 줄인다**
- 회차당 소요 → 60회 벽시계 추정
- `max_steps` 확정 → `PREREG` §5에 기록
- 누출 판정기가 실제 트레이스에서 동작하는지

### D6~D7. 본실행 (대부분 대기)

```bash
python -m quiet.harness.run_campaign --arm observe --repeats 6   # 먼저
python -m quiet.harness.run_campaign --arm block   --repeats 6   # 나중
```

**관측 팔을 먼저 다 돌린다.** 차단부터 돌리면 자연 누출률을 영영 못 잰다.

### D8. 집계 (클러스터 불필요)

- 누출률 + Wilson CI (관측 팔 30회 풀링)
- 시도율(ACTION) 대 유입률(OBSERVATION) 따로
- 팔별 탐지 정확도, 차이의 CI. **0을 포함하면 무판정**
- `noop` 거짓양성률, 탐지율 보정 전후
- 회차 순서 대비 성공률 추세 (독립성 점검)
- 검열 전후 두 벌 (§3-PREREG 4.1)

## 4.6 위험과 대응

| 위험 | 징후 | 대응 |
|---|---|---|
| sb1 접속 불가 | 지금 이 상태 | 공개키 등록 필요. 그동안 D1~D3 진행 |
| docker 없음 | `docker info` 실패 | DinD 파드 (privileged 필요) |
| 램 부족 | 파드 Pending/OOMKilled | astronomy-shop을 빼고 hotel-reservation만. 조용한 문제가 줄어 H3 검정력이 떨어지지만 실험은 성립 |
| 비용 초과 | 파일럿 토큰이 추정의 2배 | 문제 수를 줄인다. **N은 줄이지 않는다** |
| 누출률 0% | — | 그대로 보고 (§3-PREREG 8). 실패가 아니다 |
| 앱이 안 뜸 | 누적 6시간 | hotel-reservation만으로 축소 |

## 4.7 최소 성공선

**D5(파일럿)까지만 가도 말할 게 있다**:
*"벤치마크에서 정답 누출 경로를 찾아 측정 도구를 만들고 파일럿을 돌렸다."*

D7까지 가면 숫자가 붙는다:
*"관측 N회 중 M회에서 에이전트가 실제로 정답을 읽었다."*

D8의 차단 팔 비교까지 가면 결론이 붙는다:
*"그 경로를 막으면 점수가 X만큼 떨어진다 / 떨어지지 않는다."*

각 단계가 **그 자체로 완결**이라 중간에 멈춰도 손실이 제한된다.
