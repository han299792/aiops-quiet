# 런북 — sb1에서 캠페인 돌리기

**이 문서는 계획이 아니라 기록이다.** 아래 절차는 2026-09-12에 실제로 돌아갔고,
파일럿 회차를 냈다. 실패했던 경로도 같이 적는다 — 그게 다시 밟지 않는 방법이다.

> **불변 조건: sb1의 다른 워크로드는 절대 건드리지 않는다.**
> 이 절차의 모든 것은 `aiopslab-lab` 네임스페이스의 파드 **하나** 안에서 끝나고,
> `kubectl delete -f env/dind-pod.yaml` 하나로 전부 사라진다.

---

## 0. 왜 파드 안에 클러스터를 또 세우나

AIOpsLab 오케스트레이터가 회차마다 하는 일:

```
kubectl apply -f https://openebs.github.io/charts/openebs-operator.yaml
kubectl patch storageclass openebs-hostpath -p '{... is-default-class: "true"}'
...
kubectl delete sc openebs-hostpath openebs-device      # 회차 끝
```

**실측:** 이 클러스터의 StorageClass는 `local-storage` 하나뿐이고 **기본값 표시가
없다.** 캠페인은 없던 기본값을 만들고 60번 지운다. StorageClass를 명시하지 않은
기존 PVC가 있으면 그때부터 다른 프로비저너로 붙는다.

여기에 Chaos Mesh(CRD + MutatingAdmissionWebhook + privileged DaemonSet)와
AIOpsLab 자체 node-exporter DaemonSet이 더해진다. 후자는 이미 네 노드에 도는
kube-prometheus-stack node-exporter와 **충돌한다.**

**이 중 어느 것도 네임스페이스 스코프가 아니다.** 그래서 파드 안의 kind다.

**호스트에 kind를 직접 못 세우는 이유:** sandbox-m1에 docker도 podman도 없고
(containerd/nerdctl/ctr/crictl뿐) sudo에 암호가 걸려 있다. 그래서 DinD다 —
그리고 결과적으로 호스트에 도커를 까는 것보다 **격리가 더 낫다.**

---

## 1. 환경 세우기 (약 15분)

### 1.1 API 키

**키를 대화나 셸 히스토리에 남기지 않는다.** Secret으로만 넣는다.

```bash
# sb1에서. 값을 타이핑하지 말고 파일에서 읽는다
kubectl create namespace aiopslab-lab
kubectl create secret generic anthropic -n aiopslab-lab --from-file=api-key=/path/to/key
shred -u /path/to/key
```

`env/dind-pod.yaml`은 이 Secret을 `optional: true`로 읽는다. **없어도 파드는 뜬다** —
비용 0인 사전 점검 작업은 키 없이 할 수 있다.

### 1.2 파드

```bash
kubectl apply -f env/dind-pod.yaml
kubectl wait --for=condition=ready pod/aiopslab-lab -n aiopslab-lab --timeout=300s
```

`sandbox-w2`에 스케줄된다. 가장 여유 있는 워커고, **일부러 sandbox-m1이 아니다** —
거기는 컨트롤 플레인이고 운영자가 ssh로 들어가는 노드다.

### 1.3 부트스트랩

```bash
kubectl exec -it -n aiopslab-lab aiopslab-lab -- sh -c '
  apk add --no-cache git bash &&
  git clone https://github.com/han299792/aiops-quiet.git /work/aiops-quiet &&
  cd /work/aiops-quiet && git submodule update --init --recursive &&
  cp env/bootstrap-dind.sh /work/bootstrap.sh &&
  bash /work/bootstrap.sh'
```

> ### ★ `cp` 를 빼먹지 마라
> 예전에 `kubectl cp`로 스크립트를 넣어두고, 나중에 `git pull`로 레포만 갱신한
> 채 **옛날 `/work/bootstrap.sh`를 실행했다.** 고쳐놓은 마운트가 안 들어간
> 클러스터가 다시 세워졌고 300초 타임아웃으로 죽었다.
> **레포에서 매번 복사한다.** 실행되는 것과 커밋된 것이 같아야 한다.

부트스트랩이 하는 일: apk 의존성 → kubectl/helm/kind 설치 → kind 클러스터 생성
(2노드) → `pip install -e ".[dev,cluster]"` → `config.yml` 작성.

---

## 2. 실패했던 것들

세 번 실패하고 네 번째에 됐다. 원인과 조치를 남긴다.

| 증상 | 원인 | 조치 |
|---|---|---|
| `openebs-ndm` 파드가 안 뜨고 300초 타임아웃 | 노드에 `/run/udev`가 없어 hostPath 마운트 실패. 업스트림 `kind-config-x86.yaml`엔 이 extraMount가 있다 | 부트스트랩에서 `mkdir -p /run/udev` + 두 노드에 extraMounts |
| 위를 고쳤는데 또 같은 실패 | **실행된 스크립트가 옛날 사본** (§1.3 경고) | 레포에서 `cp` |
| astronomy-shop 배포 300초 타임아웃 | kind 재생성으로 containerd 이미지 캐시가 비었다. 24개 이미지를 처음부터 당긴다 | 이미지 워밍을 먼저 돌린다. 한 번 받아두면 kind 노드에 남는다 |
| 회차가 전부 `DISCARDED` | 리셋 검사 결함 2개 (아래) | 고침. `UPSTREAM.md` A7 |

**비용:** 세 번의 실패 모두 **$0.00**. 실패 격리와 회차 전 예산 검사가 설계대로 동작했다.

### 무시해도 되는 것 하나

```
Error: INSTALLATION FAILED: cannot re-use a name that is still in use
```

`pod_kill` 회차마다 로그에 뜬다. 오케스트레이터가 Chaos Mesh를 **조건 없이**
`helm install` 하기 때문이고, 이미 깔려 있으면 이 문구를 내고 그냥 진행한다.
**회차 실패가 아니다.** `helm list -A`에 `chaos-mesh`가 `deployed`면 정상이다.

로그를 훑을 때 진짜 실패와 헷갈리기 쉬워서 적어둔다 — 실제 실패는
`FAILED:` 또는 `DISCARDED` 로 찍힌다.

> ### 리셋 검사가 왜 틀렸었나 — 읽어둘 값어치가 있다
> 1. **주입 *뒤에* 검사했다.** `init_problem`이 배포와 주입을 같이 하는데
>    그 뒤에 "flagd 플래그가 켜져 있다 → 오염"이라고 판정했다. **방금 자기가
>    주입한 fault를 오염으로 신고한 것이다.**
> 2. **에러 문구를 데이터로 셌다.** Chaos Mesh 없는 클러스터에서
>    `kubectl get podchaos`는 `error: the server doesn't have a resource type
>    "podchaos"`를 낸다. `exec_command`가 stderr를 정상 문자열로 돌려주므로
>    `.split()`한 **9 단어**가 `"chaos CRs left over: 9"`가 됐다.
>
> 지금은 `verify_reset`(주입 전, 클러스터 스코프)과 `verify_fault`(주입 후,
> **fault가 실제로 걸렸는지**)로 나뉘어 있다.

---

## 3. 사전 점검 (LLM 비용 0)

돈 쓰기 전에 환경이 성립하는지 본다.

```bash
kubectl exec -it -n aiopslab-lab aiopslab-lab -- bash
cd /work/aiops-quiet

./.venv/bin/python -m pytest tests -q          # 247 통과해야 한다
./.venv/bin/python -m quiet.harness.preflight --namespace astronomy-shop
```

preflight가 확인하는 것: 파드 준비 · flagd 플래그 전부 off · Jaeger/Prometheus
도달 가능 · 차트 버전이 `0.37.2` 핀인지.

| 실패 | 뜻 |
|---|---|
| `zero spans` | Jaeger가 `limit`을 못 견뎌 끊었을 수 있다. **가장 나쁜 실패 모드다** — 빈 창은 모든 fault를 명시적으로 보이게 만든다. `DEFAULT_LIMIT=400`인지 확인 |
| `chart version` 불일치 | 핀이 안 먹었다. `helm list -n astronomy-shop`의 CHART가 `opentelemetry-demo-0.37.2`인가 |

---

## 4. 파일럿 — 캠페인 전에 반드시

**5문제 × 1회.** 60회를 걸기 전에 문제마다 배포되는지, 회차당 비용이 얼마인지 본다.

```bash
./.venv/bin/python -m quiet.harness.run_campaign --arm observe --repeats 1 \
    --max-steps 20 --budget 32 --root /work/aiops-quiet/runs
```

**확인할 것:**

```bash
cat runs/*/usage.json | grep cache_read_tokens   # 0이면 프롬프트 캐시가 안 맞은 것
cat runs/*/leak.json  | grep leaked
cat runs/discarded.jsonl                         # 있으면 왜인지 먼저 본다
```

`cache_read_tokens`가 0이면 멈춘다. 설정했다고 되는 게 아니다.
**실측(2026-09-12): 회차당 $0.022, 100초, `cache_read_tokens` 4,301.**

---

## 5. 캠페인

**observe를 먼저 전부 돌린다.** 막기 전에 자연율을 재야 한다.

```bash
# 30회
nohup ./.venv/bin/python -m quiet.harness.run_campaign \
    --arm observe --repeats 6 --max-steps 20 --budget 32 --resume \
    --root /work/aiops-quiet/runs > /work/observe.log 2>&1 &

# 끝나면
nohup ./.venv/bin/python -m quiet.harness.run_campaign \
    --arm block --repeats 6 --max-steps 20 --budget 32 --resume \
    --root /work/aiops-quiet/runs > /work/block.log 2>&1 &
```

`--resume`은 `run.json`이 있는 회차를 건너뛴다. 밤새 죽어도 이어서 돈다.

---

## 6. 결과 꺼내기

**`runs/`는 emptyDir다. 파드와 함께 죽는다.** 반드시 복사한다.

```bash
kubectl cp aiopslab-lab/aiopslab-lab:/work/aiops-quiet/runs ./runs
./.venv/bin/python -m quiet.analysis.leakage runs -o runs/REPORT.md
```

---

## 7. 정리

```bash
kubectl delete -f env/dind-pod.yaml
kubectl delete secret anthropic -n aiopslab-lab   # 네임스페이스와 같이 지워지지만 명시적으로
```

kind 클러스터, CRD, webhook, DaemonSet, StorageClass **전부 파드 안에 있었으므로
같이 사라진다.** sb1에는 아무것도 남지 않는다.

확인:

```bash
kubectl get sc                 # local-storage 하나, 기본값 표시 없음 (원래대로)
kubectl get crd | grep -i chaos    # 비어 있어야 한다
kubectl get ns | grep aiopslab     # 없어야 한다
```
