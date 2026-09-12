# 4. 실행 계획 — 어떻게 만들고 어떻게 돌리나

> 원칙 두 개.
> **(1) 클러스터 없이 되는 건 전부 먼저 한다.** 클러스터 시간은 비싸고 일정에 안 잡힌다.
> **(2) 실험은 무인으로 돈다.** 사람이 붙어 있어야 하는 절차는 안 끝난다.

---

## 4.1 지금 어디까지 돼 있나 (2026-09-12 11:10 갱신)

| 단계 | 상태 |
|---|---|
| D1 에이전트 클라이언트 `clients/claude.py` | ✅ 완료. `usage` 캡처, 프롬프트 캐시 ON, 응답 캐시 OFF |
| D2 캠페인 러너 `run_campaign.py` | ✅ 완료. 재개·예산·실패격리·원자적 쓰기 |
| D3 문서 v6 정리 | ✅ 완료. `README.md`·`RUNBOOK.md` 재작성, `6-RELATED.md` 신규 |
| D4 격리 환경 (sb1 DinD) | ✅ 완료. 3번 실패 후 성공, 전부 $0.00 |
| D5 파일럿 5문제 × 1회 | ✅ 완료. **5/5, 폐기 0, $0.14** |
| D6 observe 팔 30회 | 🔄 **진행 중** (11:10 시작) |
| D7 block 팔 30회 | ⬜ observe 끝난 뒤 |
| D8 집계 `analysis/leakage.py` | ✅ 코드 완료. 데이터 대기 |

테스트 **249개**가 클러스터·네트워크·API키 없이 통과한다.

### D5가 실제로 알려준 것

| 문제 | 턴 | 누출 | 채점 | $/회 |
|---|---:|---|---|---:|
| payment (조용) | 4 | **○** | Correct | 0.022 |
| kafka (조용) | 4 | **○** | Correct | 0.021 |
| image_slow_load (조용) | 4 | **○** | Correct | 0.025 |
| pod_kill (시끄러움) | 2 | ✗ | Correct | 0.014 |
| noop (대조) | 8 | ✗ | **Incorrect** | 0.060 |

- **`max_steps` = 20 확정** (§3-PREREG 5·9). observe 팔은 4턴에 끝나 상한이
  안 걸리지만 block 팔은 걸릴 수 있고, 그 차이가 재려는 값이다
- **비용 추정이 7배 틀렸다** — 스텝당 단가가 아니라 *스텝을 다 쓴다는 가정*이
  틀렸다. 정답을 읽으니 조사할 게 없었다 → [5-COST.md](5-COST.md) §5.6
- **계측기 결함 3개를 잡았다** — 파일럿의 진짜 소득이다. §4.8

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

## 4.4 D3 — 문서 정리 (완료)

`README.md`·`RUNBOOK.md`가 옛 주제(명시성 카탈로그)를 설명하고 있었다. 둘 다
v6로 재작성했고, `RUNBOOK.md`는 **계획이 아니라 기록**으로 바꿨다 — 실제로
돌아간 절차와 그 전에 실패한 세 경로를 원인과 함께 적는다.

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

## 4.8 ★ 파일럿의 진짜 소득 — 계측기 결함 3개

**결과보다 이게 파일럿의 값어치다.** 셋 다 60회를 오염시켰을 것이다.

### (1) 리셋 검사가 주입 뒤에 돌았다

`verify_clean`이 `init_problem`(배포 + 주입) **뒤에** 호출됐다. 그래서
"flagd 플래그가 켜져 있다 → 오염"이라고 판정했다 — **방금 자기가 주입한
fault를** 오염으로 신고한 것이다. 파드 준비 검사도 같은 병이었다: 주입 뒤
시스템은 *아파야 정상*이므로, 그 검사는 **성공한 회차를 골라서 버린다.**

`verify_reset`(주입 전, 클러스터 스코프)과 `verify_fault`(주입 후,
**fault가 실제로 걸렸는지**)로 나눴다. 후자는 업스트림에도 없는 사후 조건이다
— 주입이 no-op이어도 회차가 채점되고, 그 실패가 에이전트 오답으로 계상된다.

### (2) 에러 문구의 단어 수를 데이터로 셌다

Chaos Mesh 없는 클러스터에서 `kubectl get podchaos`는
`error: the server doesn't have a resource type "podchaos"` — 정확히 **9 단어**.
`exec_command`가 stderr를 정상 문자열로 돌려주므로 `"chaos CRs left over: 9"`가 됐다.
→ `UPSTREAM.md` A7. 프레임워크 자신의 injector도 이걸 밟고 있다.

### (3) ★ 누출 규칙이 정답이 아니라 "ConfigMap을 읽었다"에 반응했다

flagd ConfigMap은 **어느 플래그가 armed든 12개 전부를 설명과 함께 나열한다.**
그래서 "플래그 이름이 나왔다"와 `"defaultVariant"`라는 문자열은 정답에 대해
아무 정보도 없다. 옛 규칙은 `"defaultVariant": "off"`에도 매칭됐다.

→ **flagd를 읽기만 하면 무조건 누출.** noop 팔도 똑같이 찍힌다.
**실험이 기대는 대조 자체가 무너진다.**

기존 관측 픽스처가 전부 armed 플래그만 담은 이상적인 모양이라
**그때 테스트는 214개였고 전부 통과했다.** 같은 이유로 가짜 kubectl이 실패 시 예외를 던져서
(2)도 못 잡았다. **테스트 더블이 현실과 다르면 초록불은 아무것도 보장하지 않는다.**

데이터를 보고 규칙을 바꾼 것이므로 **§3-PREREG 7.5를 발동**했고,
처방대로 전량 재코딩하고 §9.1에 기록했다.

## 4.6 위험과 대응

| 위험 | 징후 | 대응 |
|---|---|---|
| ~~sb1 접속 불가~~ | 해소됨 | — |
| ~~docker 없음~~ | 실제로 없었다 (containerd만) | **DinD 파드로 해결.** `env/dind-pod.yaml` |
| 차단이 우회됨 | block 팔에서도 누출 | 메우지 않는다. **효력을 측정값으로** 낸다 → §3-PREREG 3.2·8 |
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
