# 업스트림 이슈·PR 후보

microsoft/AIOpsLab 을 읽으면서 발견한 것들. 이 포크에서 이미 고친 것과
아직 안 고친 것을 나눠 적는다. 각 항목은 **재현 방법**과 **왜 문제인가**를
같이 적는다 — 그게 없으면 이슈가 안 받아들여진다.

---

## A. 이 포크에서 고쳤음 (PR 가능)

### A1. 원격 Helm 차트에 버전 핀이 없어 문제 2개가 깨져 있다 ★

`aiopslab/service/metadata/astronomy-shop.json`에 `version` 키가 없어서
`helm install open-telemetry/opentelemetry-demo` 가 **원격 저장소의 최신본**을
당긴다. 레포 전체에서 버전을 핀한 앱은 tidb 하나뿐이다.

실제 결과 (2025-09 기준):

| | 서브모듈에 vendor된 차트 | 핀 없을 때 실제 설치되는 것 |
|---|---|---|
| 버전 | 0.37.2 | **0.41.0** |
| `loadGeneratorFloodHomepage` | 있음 | **없음** (`loadGeneratorTraffic` / `loadGeneratorVUs`로 대체) |
| 새 플래그 | — | `emailMemoryLeak`, `failedReadinessProbe`, `intlShippingSlowdown` |

→ **`astronomy_shop_loadgenerator_flood_homepage-{detection,localization}-1` 이
현재 무조건 실패한다.** `OtelFaultInjector.inject_fault`가
`ValueError: Feature flag 'loadGeneratorFloodHomepage' not found` 를 던진다.

재현:
```bash
helm repo add open-telemetry https://open-telemetry.github.io/opentelemetry-helm-charts
helm show values open-telemetry/opentelemetry-demo | grep -c loadGeneratorFloodHomepage   # 0
```

부수적으로, 핀이 없으면 어떤 결과도 시간이 지나면 재현되지 않는다.
조용한 장애는 미묘해서 앱이 조금만 달라져도 "조용함"이 안 조용해질 수 있다.

**고침**: `"version": "0.37.2"` 추가 (서브모듈 vendor 버전과 일치).
`Helm.install`은 이미 `version`을 지원한다(`helm.py`).

### A2. LLM 캐시를 끌 수 없다 ★

`clients/utils/llm.py`의 `GPTClient.__init__`이 `use_cache: bool = True`를
**인자로 받고 무시한다** — `self.cache = Cache()`가 무조건 실행된다.

왜 문제인가: 같은 문제를 N회 반복하는 실험에서 1스텝의 payload는 매번
동일하므로 반드시 캐시 히트다. 텔레메트리가 안정적이면 궤적 전체가 재생될 수
있고, 그러면 **반복이 독립이 아니게 되어 분산이 인위적으로 0에 가까워진다.**

**고침**: `self.cache = Cache() if use_cache else None`. `inference`/`run`은
이미 `if self.cache is not None:`로 감싸져 있어 한 줄이면 된다.

### A3. 캐시 키에 모델이 없어 모델 간 오염이 일어난다 ★

`Cache.process_payload`가 `json.dumps(payload)`만 키로 쓴다. 모델명도,
temperature도, max_tokens도 키에 없다.

→ **gpt-4o로 채운 `./cache_dir/cache.json`이 다른 모델의 질문에 그대로 답한다.**
이미 여러 모델을 비교 실행한 사람이 있다면 그 결과는 오염돼 있고, 본인은 모른다.

재현: `GPTClient()`로 한 번 돌린 뒤 같은 프롬프트로 `OpenRouterClient()`를
돌리면 OpenRouter API 호출 없이 gpt-4o의 답이 나온다.

**고침**: `Cache(namespace=...)`를 추가하고 각 클라이언트가 모델(및 샘플링
파라미터) 식별자를 넘긴다. 네임스페이스가 비면 기존 키 형식을 그대로 쓰므로
하위호환된다.

### A4. `normal_metrics`의 콤마 누락으로 메트릭 2개가 사라진다

`aiopslab/observer/metric_api.py`:
```python
    "container_threads",
    "container_threads_max"      # ← 콤마 없음
    # network
    "container_network_receive_errors_total",
```
파이썬의 인접 문자열 리터럴 연결로 두 항목이
`"container_threads_maxcontainer_network_receive_errors_total"` 라는
**존재하지 않는 메트릭 하나**가 된다. 결과적으로 `container_threads_max`와
`container_network_receive_errors_total`이 목록에서 빠진다.

재현: `len(normal_metrics)`가 26이 아니라 25.

### A5. `export_all_metrics`가 첫 청크 후 port-forward를 끊는다

`self.cleanup()`이 `while start_time < end_time:` 루프 **안**에 있다.
2시간 청크 하나를 끝내면 port-forward가 죽고, 그 뒤의 모든 청크는 닫힌
포트에 질의한다. 2시간을 넘는 창을 뜨면 조용히 데이터가 잘린다.

**고침**: `cleanup()`을 루프 밖으로.

### A6. `OtelFaultInjector`가 severity 변이를 노출하지 않는다

`paymentFailure`는 10%/25%/50%/75%/90%/100%를, `imageSlowLoad`는 5sec/10sec를
제공하는데 주입기가 각각 `"100%"`, `"10sec"`로 하드코딩한다. 부분 장애를
만들 방법이 없다.

**고침**: `inject_fault(feature_flag, variant=None)`. 기본값은 기존 동작과
같으므로 하위호환. 추가로 변이를 **라이브 ConfigMap의 `variants` 맵에 대해
검증**한다 — A1 때문에 차트가 바뀔 수 있고, 검증이 없으면 인식 못 하는 변이가
ConfigMap에 쓰이고 flagd는 기본값을 서빙해서 **아무 일도 안 일어나는데 장애로
채점된다.**

부수적으로 기존 로그 메시지가 항상 `set to 'on'`이라고 출력했는데
(100%로 설정해놓고도) 실제 변이를 출력하도록 고쳤다.

---

## B. 아직 안 고침 (이슈 후보)

### B1. `exec_shell`이 정답을 그대로 읽을 수 있다 ★★

`aiopslab/orchestrator/actions/base.py`의 `exec_shell`은 임의 셸이고,
차단 목록은 대화형 명령 5개의 부분문자열 매칭뿐이다. 네임스페이스·리소스
화이트리스트가 레포 어디에도 없다.

```
kubectl get cm flagd-config -n astronomy-shop -o yaml
  -> "paymentFailure": {"defaultVariant": "100%"}      # 답이 그대로
kubectl get podchaos,networkchaos -A                    # 장애가 이름표를 달고 있음
kubectl get events -n astronomy-shop                    # 주입 시각이 찍혀 있음
```

즉 **벤치마크 점수의 일부가 추론이 아니라 정답 조회일 수 있다.**
이건 추측이 아니라 액션 스페이스에서 바로 따라 나오는 사실이다.
얼마나 자주 일어나는지는 이 프로젝트가 측정해서 숫자로 붙일 예정
(`aiopslab-quiet/quiet/harness/leak.py`).

숫자가 나온 뒤에 이슈를 낸다. 숫자 없이 내면 "그럴 수도 있다"에 그친다.

### B2. `TraceAPI.get_traces`가 `end_time`을 받아서 버린다 ★

```python
def get_traces(self, service_name, start_time, end_time, limit=None):
    ...
    lookback = now - start_time     # end_time은 쓰이지 않는다
```
Jaeger API 자체는 절대 `start`/`end`(µs)를 지원하는데 쓰지 않는다.
→ **이미 끝난 시간창을 가져올 수 없다.** 정상 구간과 장애 구간을 비교하는
어떤 분석도 이 API로는 불가능하다.

덤으로 `extract_traces`와 `save_traces`가 각각 끝에서 `self.cleanup()`을
불러 port-forward를 죽이므로 사실상 1회용이다.

### B3. `PrometheusAPI`가 forward한 포트와 다른 포트에 질의한다 ★

생성자가 32000~32100에서 빈 포트를 골라 그 포트로 port-forward 해놓고,
쿼리는 **인자로 받은 URL**로 던진다. 그런데 모든 호출부가
`http://localhost:32000`을 하드코딩한다(`actions/base.py`, `monitor_config.yaml`).

→ 32000이 이미 점유돼 있으면 32001로 forward하고 32000에 질의한다.

부수 문제: 무데이터 시 list가 아니라 `{"error": ...}` dict를 반환(호출부는
list를 기대), 값 타임스탬프에 **Asia/Shanghai** 타임존을 찍는다.

### B4. 제출 실패 시 `start_problem`이 예외로 죽는다

`orchestrator.py`:
```python
key = next((k for k in ["TTD","TTL","TTA","TTM"] if k in results), None)
framework_overhead = total_execution_time - results[key]
```
`results`가 비면 `key`가 `None`이 되어 `results[None]` → `KeyError`.
평가가 실패한 회차에서 결과 전체를 잃는다.

### B5. Anthropic 클라이언트가 없다

`clients/`에 OpenAI/Azure/DeepSeek/Qwen/vLLM/OpenRouter/Groq는 있는데
Anthropic 네이티브 클라이언트가 없다. `anthropic` 의존성도 없고
`.env.example`에 `ANTHROPIC_API_KEY`도 없다. Claude는 OpenRouter 경유만 가능.

기여 난이도가 낮고 범용성이 높아서 PR 후보 1번.

### B6. 비용 추적이 전혀 없다

모든 클라이언트가 `response.usage`를 버린다. `in_tokens`/`out_tokens`는
사후에 트레이스를 **한 번** 세는 것이라, 매 스텝 히스토리를 재전송하는
멀티턴 에이전트의 실제 입력 토큰을 크게 과소계상한다. 달러 값은 아예 없다.

### B7. Filebeat의 대상 네임스페이스가 실제 배포 네임스페이스와 다르다

`aiopslab/observer/filebeat/values.yaml`의 autodiscover가
`kubernetes.namespace: "hotel-reservation"`을 보는데, 앱은
`test-hotel-reservation`에 배포된다(`metadata/hotel-reservation.json`).
README가 "values.yaml에서 네임스페이스를 바꾸라"고 적어두긴 했지만
기본값이 틀린 상태다.

덤으로 Logstash의 출력이 `http://10.0.0.4:9200`으로 하드코딩돼 있고
Elasticsearch를 클러스터 **밖**에 따로 세우도록 요구한다.

---

## 우선순위

1. **A1** (차트 핀) — 문제 2개가 실제로 깨져 있다. 재현이 명확하고 고침이 1줄
2. **A2 + A3** (캐시) — 조용히 결과를 오염시킨다. 고침이 작다
3. **B5** (Anthropic 클라이언트) — 난이도 낮고 범용
4. **A4 + A5** (metric_api) — 명백한 버그, 고침이 작다
5. **B1** (정답 누출) — 가장 임팩트가 크지만 **숫자가 나온 뒤에** 낸다
6. **B2 + B3** (observer API) — 재작성이 필요해서 이슈로 먼저
