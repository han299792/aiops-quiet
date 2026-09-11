# 단계 B 런북 — 연구실 첫 접속

목표: **반나절, LLM 비용 $0.** 끝나면 노트북에서 파서·채점기 개발을 계속할 수
있는 실제 원본 데이터가 손에 있어야 한다.

이 세션의 성공 조건은 "실험 결과가 나오는 것"이 아니라
**`tests/fixtures/real/`에 정상 창 1개와 장애 창 1개의 RawWindow가 커밋되는 것**이다.
그게 되면 이후 작업이 전부 노트북으로 옮겨간다.

---

## B0. 격리 판정 (15분)

기존 워크로드가 도는 클러스터를 **건드리지 않는다.** 그 옆에 따로 세운다.

```bash
# 호스트에 셸이 있고 도커가 되는가?
docker info >/dev/null 2>&1 && echo "kind 직접 설치 가능 (선호)" || echo "kind-in-pod 필요"
```

- **되면**: 그 호스트에서 `kind create cluster`. 기존 k8s API 서버와 완전히
  분리되고, kubectl을 기존 클러스터에 겨눌 일이 아예 없다.
- **안 되면**: kind-in-pod (privileged + cgroup v2 필요).

**왜 네임스페이스 격리로 부족한가** — 이 프레임워크는 회차마다
`kubectl patch storageclass openebs-hostpath ... is-default-class: true` 를 하고
`start_problem` 끝에 `kubectl delete sc openebs-hostpath openebs-device` 를 한다.
**클러스터의 기본 StorageClass를 지웠다 만든다.** StorageClass를 명시하지 않은
기존 PVC가 있으면 그때부터 다른 프로비저너로 붙는다.

여기에 Chaos Mesh 2.6.2(CRD + MutatingAdmissionWebhook + privileged DaemonSet)와
node-exporter DaemonSet이 더해진다. node-exporter가 남의 워크로드가 도는 노드를
긁으면 **정상 창이 오염되고**, 정상이 시끄러우면 "조용한 장애"라는 개념 자체가
성립하지 않는다. 격리는 예의가 아니라 측정 조건이다.

자원: 여유 램 **16~24GB**, 디스크 50GB. (astronomy-shop의 메모리 limit 합만 6.4GiB)

## B1. 환경 (20분)

```bash
git clone --recurse-submodules https://github.com/han299792/AIOpsLab.git
cd AIOpsLab && git checkout quiet-failures
git submodule status          # 8038be6 여야 한다

cp aiopslab/config.yml.example aiopslab/config.yml
# k8s_host: kind   (kind면) / localhost (클러스터 위에서 직접 돌리면)

poetry env use python3.11 && poetry install
python -m venv aiopslab-quiet/.venv && aiopslab-quiet/.venv/bin/pip install -e "aiopslab-quiet[dev]"
aiopslab-quiet/.venv/bin/python -m pytest aiopslab-quiet/tests -q   # 176 통과해야 함
```

## B2. 배포 경로만 먼저 검증 (30~60분)

**장애 주입을 빼고** 배포 경로만 확인한다. 실패했을 때 원인이 갈리지 않게.

```bash
python -c "
from aiopslab.orchestrator.problems.registry import ProblemRegistry
p = ProblemRegistry().get_problem_instance('noop_detection_astronomy_shop-1')
p.app.delete(); p.app.deploy()
"
kubectl get pods -n astronomy-shop
```

astronomy-shop은 파드가 많고 프론트엔드가 무겁다(1.5GB). 전부 Ready가 될 때까지
기다린다. 안 뜨면 자원 부족이 1순위 의심.

## B3. ★★ preflight — 캡처 전에 반드시. 2분

**이 한 줄이 B3와 B5를 대신한다.**

```bash
aiopslab-quiet/.venv/bin/python -m quiet.harness.preflight --namespace astronomy-shop
```

확인하는 것: kubectl 컨텍스트 · 네임스페이스 · 전 파드 Ready · restart 0 ·
**flagd 플래그가 전부 off**(이전 회차 누수 감지) · **용량 사다리 변이 7개 존재** ·
잔존 chaos CR 0 · Jaeger 도달 · Prometheus 도달.

그리고 **`POWER.md`가 요구한 세 값을 같이 재준다** — 서비스별 분당 span,
기저 에러율, payment span 중 charge 비율. 목표 span 수에 필요한 창 길이까지 계산해준다.

`FAIL`이 하나라도 뜨면 **거기서 멈춘다.** 그대로 캡처를 시작하면 70분 뒤에 같은 사실을 알게 된다.

흔한 FAIL과 원인:

| FAIL | 원인 |
|---|---|
| `dose ladder missing [...]` | 차트 핀이 안 먹었다. `helm list -n astronomy-shop`의 CHART가 `opentelemetry-demo-0.37.2`인지 본다 |
| `flag baseline not off: [...]` | 이전 회차의 플래그가 남았다. `recover_fault`가 실패했다는 뜻 |
| `chaos CRs N left over` | Chaos Mesh 잔존물. 지우고 다시 |
| `jaeger no endpoint` | 서비스 이름이 다르다. `kubectl get svc -n astronomy-shop \| grep -i jaeger` 로 확인하고 `collect.open_endpoints`에 추가 |
| `zero spans` | 로드제너레이터가 안 돈다. 트래픽 없이 뜬 창은 모든 장애를 명시적으로 보이게 만든다 |

### preflight 결과를 반영한다

출력 마지막 절의 세 값을 **`POWER.md`에 적고** `python -m quiet.analysis.power`를
다시 돌린다. 그다음 창 길이를 확정하고 **`PREREG.md` §11 개정 이력에 기록한다.**
PREREG가 창 길이 확정을 여기까지 미뤄둔 이유가 이것이다.

## B4. ★★ 원본 한 번 뜨기 — 이 세션의 본론

**전 프로젝트에서 단일 최고 레버리지 클러스터 작업.** 이게 끝나면 파서와
채점기 개발이 전부 노트북으로 옮겨간다.

창을 **길게** 뜬다. 오프라인에서 잘라 짧은 창을 만들 수 있지만 그 반대는 안 된다.

```bash
aiopslab-quiet/.venv/bin/python -m quiet.harness.run_probe \
    payment_dose_100-detection-1 \
    --window 1800 --warmup 300 --settle 90 \
    --root aiopslab-quiet/runs
```

30분 창 × 2(정상·장애) + 워밍업 = 약 70분. preflight를 통과했다면 이제 기다리기만 하면 된다.

출력 확인:
```bash
ls aiopslab-quiet/runs/*payment_dose_100*/
# raw_normal.json  normal.json  raw_fault.json  fault.json  spec.json  run.json
python -c "
import json,glob
for f in sorted(glob.glob('aiopslab-quiet/runs/*payment_dose_100*/raw_*.json')):
    d=json.load(open(f))
    print(f.split('/')[-1], 'spans:', len(d['spans']), 'logs:', len(d['logs']),
          'events:', len(d['events']), 'errors:', d['collection_errors'])
"
```

**`collection_errors`가 비어 있어야 한다.** 비어 있지 않으면 그 채널은
채점에서 빠지므로, 원인을 지금 고친다. 흔한 것:
- `jaeger: no endpoint configured` → 서비스 이름이 다르다. `kubectl get svc -n astronomy-shop | grep -i jaeger` 로 확인하고 `collect.open_endpoints`에 추가
- `prometheus: ...` → `kubectl get svc -n observe` 확인

그다음 **fixture로 커밋한다** (`runs/**/raw_*.json` 은 gitignore이므로 복사해야 한다):
```bash
mkdir -p aiopslab-quiet/tests/fixtures/real
cp aiopslab-quiet/runs/*payment_dose_100*/raw_normal.json aiopslab-quiet/tests/fixtures/real/
cp aiopslab-quiet/runs/*payment_dose_100*/raw_fault.json  aiopslab-quiet/tests/fixtures/real/
git add -f aiopslab-quiet/tests/fixtures/real && git commit && git push
```

## B5. 캡처한 원본으로 값을 재확인 (선택)

B3의 preflight가 3분 표본으로 이미 같은 값을 냈다. 30분 캡처가 끝난 뒤
같은 값을 더 긴 표본으로 다시 보고 싶으면:

```bash
aiopslab-quiet/.venv/bin/python -m quiet.harness.preflight \
    --namespace astronomy-shop --minutes 30
```

preflight의 3분 표본과 크게 다르면 **부하가 시간에 따라 흔들린다는 뜻**이고,
그러면 정상 창과 장애 창이 교환 가능하지 않아 홀드아웃 거짓양성률이 α를 넘는다.
그 경우 워밍업을 늘린다.

## B6. versions.lock.yml

```bash
{
  echo "framework_sha: $(git rev-parse HEAD)"
  echo "submodule_sha: $(git submodule status | awk '{print $1}')"
  echo "chart: $(helm list -n astronomy-shop -o json | python -c 'import json,sys; print(json.load(sys.stdin)[0]["chart"])')"
  echo "k8s: $(kubectl version -o json | python -c 'import json,sys; print(json.load(sys.stdin)["serverVersion"]["gitVersion"])')"
  echo "scrape_interval: $(kubectl get cm prometheus-server -n observe -o jsonpath='{.data.prometheus\.yml}' | grep -m1 scrape_interval)"
  echo "images:"
  kubectl get pods -n astronomy-shop -o jsonpath='{range .items[*]}{range .status.containerStatuses[*]}  - {.imageID}{"\n"}{end}{end}' | sort -u
} > aiopslab-quiet/env/versions.lock.yml
```

**이미지는 태그가 아니라 digest로 기록한다.** `latest` 태그가 섞여 있으면
3주 뒤 다른 바이너리가 뜨고, 조용한 장애는 앱이 조금만 달라져도 안 조용해진다.

## B7. 발굴 (남는 시간에)

`runs/_archaeology/FINDINGS.md`의 클러스터 쪽 항목이 아직 비어 있다.

```bash
helm list -A                                   # 예전 릴리스가 남아 있나
kubectl get ns                                 # 예전 네임스페이스
kubectl get podchaos,networkchaos -A           # chaos CR 잔존물
kubectl get pv                                 # hostPath PV 잔존물
ls ~/.bash_history && grep -i aiopslab ~/.bash_history | tail -50
find ~ -path "*data/results/*.json" 2>/dev/null | head   # ★ 세션 JSON이 남아 있을 수 있다
```

찾은 것은 `runs/_archaeology/`에 **그대로** 넣는다. 해석은 나중에.
아무것도 없으면 그것도 한 줄로 기록한다.

---

## 하지 말 것

- **에이전트를 돌리지 않는다.** 이 세션은 비용 $0이다.
- **창을 짧게 뜨지 않는다.** 길게 떠서 오프라인에서 자른다.
- **collection_errors를 무시하지 않는다.** 채널이 조용히 빠진다.
- **preflight가 FAIL인 채로 진행하지 않는다** (B3). 70분 뒤에 같은 사실을 알게 된다.
