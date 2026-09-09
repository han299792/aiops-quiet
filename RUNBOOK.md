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
aiopslab-quiet/.venv/bin/python -m pytest aiopslab-quiet/tests -q   # 175 통과해야 함
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

## B3. ★ 차트 버전 확인 — 여기서 갈린다

```bash
helm list -n astronomy-shop          # CHART 열이 opentelemetry-demo-0.37.2 여야 한다
kubectl get cm flagd-config -n astronomy-shop -o json \
  | python -c "import json,sys; d=json.load(sys.stdin); \
    print(json.loads(d['data']['demo.flagd.json'])['flags']['paymentFailure']['variants'])"
```

**10%/25%/50%/75%/90%/100%/off 가 전부 보여야 한다.** 안 보이면 핀이 안 먹은 것이고
용량 사다리가 성립하지 않으므로 여기서 멈추고 원인을 찾는다.

(이 핀이 없으면 0.41.0이 설치되는데, 거기서는 `loadGeneratorFloodHomepage`가
제거돼 업스트림 문제 2개가 아예 실행되지 않는다 — `UPSTREAM.md` A1 참조.)

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

30분 창 × 2(정상·장애) + 워밍업 = 약 70분. 그동안 B5를 읽어둔다.

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

## B5. ★ POWER.md가 요구한 세 값을 측정

`POWER.md`의 결론: 곡선의 모양이 아직 모르는 두 파라미터에 달려 있고,
**측정 전에 창 길이와 사다리를 확정하지 않는다.** 원본에서 바로 읽는다.

```bash
aiopslab-quiet/.venv/bin/python - <<'EOF'
import json, collections
raw = json.load(open("aiopslab-quiet/tests/fixtures/real/raw_normal.json"))
spans = raw["spans"]
minutes = 30.0
by_svc = collections.Counter(s["service"] for s in spans)
print("서비스별 분당 span:")
for svc, n in by_svc.most_common(15):
    print(f"  {svc:<28} {n/minutes:8.1f}/min   (창 전체 {n})")
err = sum(s["has_error"] for s in spans)
print(f"\n기저 에러율: {err/max(len(spans),1):.4%}   (POWER.md: 5% 넘으면 10% 용량이 묻힌다)")
pay = [s for s in spans if s["service"] == "payment"]
charge = [s for s in pay if "charge" in s["operation"].lower()]
print(f"payment span {len(pay)} 중 charge {len(charge)} "
      f"= charge_fraction {len(charge)/max(len(pay),1):.1%}")
print("   (POWER.md: 무릎이 10~25% 사이. 25%면 10% 용량도 명시적, 5%면 100%도 조용)")
EOF
```

이 세 값을 `POWER.md`에 적고 `python -m quiet.analysis.power`를 다시 돌린 뒤
**창 길이와 사다리를 확정하고 `PREREG.md` §11에 기록한다.**

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
- **차트 버전을 확인하기 전에 진행하지 않는다** (B3).
