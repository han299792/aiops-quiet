#!/usr/bin/env sh
# Bring the lab pod from an empty docker:dind image to a running kind cluster
# with AIOpsLab installed. Run inside the pod:
#
#   kubectl exec -it -n aiopslab-lab aiopslab-lab -- sh
#   wget -qO- https://raw.githubusercontent.com/han299792/aiops-quiet/main/env/bootstrap-dind.sh | sh
#
# or copy it in:
#   kubectl cp env/bootstrap-dind.sh aiopslab-lab/aiopslab-lab:/work/bootstrap.sh
#
# Idempotent: safe to re-run after a disconnect.
set -eu

WORK=/work
REPO_URL="${REPO_URL:-https://github.com/han299792/aiops-quiet.git}"
KIND_VERSION="${KIND_VERSION:-v0.30.0}"
K8S_IMAGE="${K8S_IMAGE:-kindest/node:v1.32.1}"
CLUSTER="${CLUSTER:-aiopslab}"

say() { printf '\n=== %s ===\n' "$1"; }

say "0. docker daemon"
# The pod's readiness probe already waits for this, but a re-run after a
# container restart may race it.
for i in $(seq 1 30); do
  docker info >/dev/null 2>&1 && break
  [ "$i" = 30 ] && { echo "docker daemon never came up"; exit 1; }
  sleep 2
done
docker info --format 'server={{.ServerVersion}} cgroup={{.CgroupVersion}}'

say "1. base packages"
# docker:dind is Alpine.
apk add --no-cache git curl bash python3 py3-pip python3-dev \
  build-base libffi-dev openssl-dev >/dev/null
python3 --version

say "2. kubectl / helm / kind"
ARCH=$(uname -m); case "$ARCH" in x86_64) ARCH=amd64 ;; aarch64) ARCH=arm64 ;; esac

if ! command -v kubectl >/dev/null 2>&1; then
  KV=$(curl -sL https://dl.k8s.io/release/stable.txt)
  curl -sLo /usr/local/bin/kubectl "https://dl.k8s.io/release/${KV}/bin/linux/${ARCH}/kubectl"
  chmod +x /usr/local/bin/kubectl
fi
if ! command -v helm >/dev/null 2>&1; then
  curl -sL https://get.helm.sh/helm-v3.16.3-linux-${ARCH}.tar.gz | tar xz -C /tmp
  mv "/tmp/linux-${ARCH}/helm" /usr/local/bin/helm && chmod +x /usr/local/bin/helm
fi
if ! command -v kind >/dev/null 2>&1; then
  curl -sLo /usr/local/bin/kind \
    "https://kind.sigs.k8s.io/dl/${KIND_VERSION}/kind-linux-${ARCH}"
  chmod +x /usr/local/bin/kind
fi
kubectl version --client=true -o yaml 2>/dev/null | head -3
kind version; helm version --short

say "3. kind cluster"
if kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  echo "cluster '$CLUSTER' already exists — reusing"
else
  # Pinned node image: an unpinned kind image means a different Kubernetes
  # three weeks later, and quiet failures are subtle enough that the app
  # version matters.
  cat >/tmp/kind.yaml <<YAML
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
  - role: control-plane
    image: ${K8S_IMAGE}
  - role: worker
    image: ${K8S_IMAGE}
YAML
  kind create cluster --name "$CLUSTER" --config /tmp/kind.yaml --wait 300s
fi
kubectl get nodes

say "4. repository"
cd "$WORK"
if [ -d aiops-quiet/.git ]; then
  cd aiops-quiet && git pull --ff-only && git submodule update --init --recursive
else
  git clone --recurse-submodules "$REPO_URL" aiops-quiet
  cd aiops-quiet
fi
git log --oneline -1
git submodule status

say "5. python environments"
# The measuring instrument: light dependencies, its own venv.
[ -d .venv ] || python3 -m venv .venv
./.venv/bin/pip -q install -e ".[dev]"
./.venv/bin/python -m pytest tests -q 2>&1 | tail -3

# The framework: heavy. vllm is a hard dependency upstream and does not build
# here, so install what is actually needed rather than the whole lock file.
[ -d vendor/AIOpsLab/.venv ] || python3 -m venv vendor/AIOpsLab/.venv
# The list is longer than it looks it should be because several of these are
# imported unconditionally at module scope regardless of whether the feature is
# used: wandb by aiopslab/session.py, openai by evaluators/qualitative.py (the
# LLM judge, even with qualitative_eval false), docker and paramiko by the
# service layer. Importing the problem registry pulls all of them.
vendor/AIOpsLab/.venv/bin/pip -q install \
  kubernetes "urllib3<2.6.0" rich colorama pyyaml python-dotenv \
  prometheus-api-client tiktoken anthropic pandas requests \
  wandb openai prompt-toolkit docker paramiko

say "6. AIOpsLab config"
cd vendor/AIOpsLab
[ -f aiopslab/config.yml ] || {
  cp aiopslab/config.yml.example aiopslab/config.yml
  sed -i 's|^k8s_host:.*|k8s_host: localhost|' aiopslab/config.yml
  sed -i 's|^k8s_user:.*|k8s_user: root|' aiopslab/config.yml
}
grep -E '^k8s_host|^k8s_user' aiopslab/config.yml
cd "$WORK/aiops-quiet"

cat <<'DONE'

=== bootstrap complete ===

Next:
  export ANTHROPIC_API_KEY=...
  .venv/bin/python -m quiet.harness.preflight --namespace astronomy-shop

The preflight will fail until an application is deployed — that is expected.
Deploy one first (RUNBOOK step B2), then run it again.

Results live on an emptyDir and die with the pod. Copy them out from the
host with:
  kubectl cp aiopslab-lab/aiopslab-lab:/work/aiops-quiet/runs ./runs
DONE
