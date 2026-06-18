# Coder on EKS — Recreate-from-Scratch Demo Guide

Everything below was tested end-to-end. Run the commands in order from
the root of this repo (`coder-demo/`).

## What you get
- A small EKS cluster (2x t3.medium)
- Coder control plane (Helm, OSS, in-cluster Postgres)
- A `demo-app` namespace where workspaces run
- A workspace ServiceAccount (`coder-workspace`) that can ONLY act inside
  `demo-app` — verified below to be blocked everywhere else in the cluster
- A `demo-k8s` workspace template (Kubernetes Deployment) with kubectl + helm
  preinstalled, ready for "containerize / fix / helm-ify" style demos

---

## 1. Create the EKS cluster

```bash
cd coder-demo   # repo root
eksctl create cluster -f eks-coder-cluster.yaml
```

Takes ~12-15 min. Confirm:
```bash
kubectl get nodes
```

## 2. Enable EBS storage (required for Postgres + workspace home dirs)

EKS 1.30 has no default storage provisioner — without this, every PVC stays
`Pending` forever.

```bash
eksctl utils associate-iam-oidc-provider --cluster coder-demo --region us-east-1 --approve

eksctl create iamserviceaccount \
  --name ebs-csi-controller-sa --namespace kube-system \
  --cluster coder-demo --region us-east-1 \
  --attach-policy-arn arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy \
  --approve --role-only --role-name AmazonEKS_EBS_CSI_DriverRole_coder-demo

ROLE_ARN=$(aws iam get-role --role-name AmazonEKS_EBS_CSI_DriverRole_coder-demo --query 'Role.Arn' --output text)

eksctl create addon --cluster coder-demo --region us-east-1 \
  --name aws-ebs-csi-driver --service-account-role-arn "$ROLE_ARN" --force

kubectl apply -f coder-demo/storageclass.yaml
kubectl patch storageclass gp2 -p '{"metadata": {"annotations":{"storageclass.kubernetes.io/is-default-class":"false"}}}'
```

## 3. Create the sandbox namespace + scoped RBAC

This is the **safety boundary**: the workspace's ServiceAccount can only
read/write Pods, Deployments, ReplicaSets, Services, ConfigMaps, Secrets,
PVCs, and Jobs — **inside `demo-app` only**. It cannot see `kube-system`,
other namespaces, or any cluster-scoped resource (no `get namespaces`, no
RBAC objects, nothing outside its box).

```bash
kubectl apply -f coder-demo/rbac.yaml
```

## 4. Install Postgres (Coder's database)

```bash
kubectl create namespace coder

helm repo add bitnami https://charts.bitnami.com/bitnami
helm repo add coder-v2 https://helm.coder.com/v2
helm repo update

helm install coder-db bitnami/postgresql \
  --namespace coder \
  --set auth.username=coder \
  --set auth.password=coderpass \
  --set auth.database=coder \
  --set primary.persistence.size=10Gi \
  --wait --timeout 5m

kubectl create secret generic coder-db-url -n coder \
  --from-literal=url="postgres://coder:coderpass@coder-db-postgresql.coder.svc.cluster.local:5432/coder?sslmode=disable"
```

## 5. Install Coder

`coder-demo/coder-values.yaml` already:
- Sets resource requests low enough to fit on t3.medium (default 2 CPU /
  4Gi request doesn't fit — we set 250m/512Mi request, 1 CPU/1Gi limit)
- Exposes Coder via a LoadBalancer
- Contains `CODER_ACCESS_URL`/`CODER_WILDCARD_ACCESS_URL` from the **current
  live deployment** (`35-174-85-96.nip.io`). On a fresh cluster these point
  at the wrong (old) IP — that's harmless for this first install (Coder will
  still start and become `Running`), because you'll overwrite them with the
  correct new IP in the `helm upgrade` step below.
- Sets `CODER_OAUTH2_GITHUB_DEFAULT_PROVIDER_ENABLE: "false"` to remove the
  broken default "Sign in with GitHub" button (see step 6).

```bash
helm install coder coder-v2/coder \
  --namespace coder \
  --values coder-demo/coder-values.yaml \
  --version 2.34.2 \
  --wait --timeout 5m
```

Get the LB hostname and resolve its IP (the AWS LB can take 1-2 min to
become resolvable, so retry):
```bash
LB=$(kubectl get svc -n coder coder -o jsonpath='{.status.loadBalancer.ingress[0].hostname}')
echo "LB hostname: $LB"

for i in $(seq 1 15); do
  IP=$(getent ahostsv4 "$LB" | awk '{print $1; exit}')
  [ -n "$IP" ] && break
  echo "waiting for LB DNS..."; sleep 10
done

echo "Access URL: http://$(echo $IP | tr '.' '-').nip.io"
```
> If `getent` isn't available, `dig +short $LB | head -1` works the same way.

**Update `coder-demo/coder-values.yaml`** — replace the
`CODER_ACCESS_URL` / `CODER_WILDCARD_ACCESS_URL` values with the nip.io
hostname from above (access URL has no `*.` prefix, wildcard URL does:
`*.<ip-with-dashes>.nip.io`), then:

```bash
helm upgrade coder coder-v2/coder \
  --namespace coder \
  --values coder-demo/coder-values.yaml \
  --version 2.34.2 \
  --wait --timeout 5m
```

## 6. Create your admin login + UI access

```bash
ACCESS_URL="http://<your-ip-with-dashes>.nip.io"

curl -s -X POST $ACCESS_URL/api/v2/users/first \
  -H "Content-Type: application/json" \
  -d '{"email":"ddd@gmail.com","username":"admin","name":"Admin","password":"CoderDemo123!"}'
```

**Open the UI**: visit `$ACCESS_URL` in your browser and log in using the
**username/password form**. The "Email" field on this form requires a
valid email format — enter `ddd@gmail.com` (NOT `admin`), and
password `CoderDemo123!`.

> Note: `coder-values.yaml` sets `CODER_OAUTH2_GITHUB_DEFAULT_PROVIDER_ENABLE:
> "false"`, which removes the default "Sign in with GitHub" button (it
> otherwise errors with "signup is disabled" since no GitHub OAuth app is
> configured). Only the password form is shown — that's expected.

> Current live deployment: **http://35-174-85-96.nip.io** —
> log in with email `ddd@gmail.com` / password `CoderDemo123!`
> (the dashboard will then show your username `admin`).

![Coder login screen](screenshots/01-coder-login.png)
*Login form — use the email field (not "admin") with the password set above.*

## 7. Install the Coder CLI (no sudo needed)

```bash
curl -fsSL https://coder.com/install.sh | sh -s -- --version 2.34.2
# if it can't sudo, extract manually:
dpkg -x ~/.cache/coder/coder_2.34.2_amd64.deb /tmp/coder-extract
mkdir -p ~/bin && cp /tmp/coder-extract/usr/bin/coder ~/bin/coder && chmod +x ~/bin/coder
export PATH=$HOME/bin:$PATH
```

Authenticate. We persist the session to `~/.config/coderv2` (not just env
vars) because `coder ssh`/`coder config-ssh` shell out via a ProxyCommand
that won't see your exported env vars:
```bash
TOKEN=$(curl -s -X POST $ACCESS_URL/api/v2/users/login \
  -H "Content-Type: application/json" \
  -d '{"email":"ddd@gmail.com","password":"CoderDemo123!"}' \
  | grep -o '"session_token":"[^"]*"' | cut -d'"' -f4)

export CODER_SESSION_TOKEN=$TOKEN
export CODER_URL=$ACCESS_URL

mkdir -p ~/.config/coderv2
echo "$ACCESS_URL" > ~/.config/coderv2/url
echo -n "$TOKEN" > ~/.config/coderv2/session

coder whoami
coder config-ssh --yes
```

## 8. Push the scoped workspace template

`coder-demo/template-kubernetes/main.tf` is the stock Coder Kubernetes
template with these changes:
- `namespace` default = `demo-app`
- `service_account_name = "coder-workspace"` (the scoped SA from step 3)
- startup script installs `kubectl` + `helm`
- the Claude Code module + `env_from` block that injects the
  `anthropic-api-key` secret (described in step 11b/c) — already baked into
  this file

> **Important:** because the Claude Code module is already in `main.tf`,
> the container spec references the `anthropic-api-key` Kubernetes secret
> from the moment you push this template. Create that secret **now**, before
> pushing the template / creating the workspace, by running step 11a below —
> otherwise the workspace pod will fail to start with
> `CreateContainerConfigError`.
>
> ```bash
> kubectl create secret generic anthropic-api-key -n demo-app \
>   --from-literal=ANTHROPIC_API_KEY='sk-ant-...' \
>   --dry-run=client -o yaml | kubectl apply -f -
> ```

```bash
cd coder-demo/template-kubernetes
coder templates push demo-k8s --directory . --yes \
  --variable use_kubeconfig=false --variable namespace=demo-app
cd ../..
```

## 9. Create a workspace

```bash
coder create demo-ws --template demo-k8s --yes \
  --parameter cpu=2 --parameter memory=2 --parameter home_disk_size=10 < /dev/null
```

![New workspace form](screenshots/02-new-workspace-form.png)
*The "New workspace" form in the Coder UI (same thing the `coder create` command above does) — name it, pick CPU/memory/disk, and click "Create workspace".*

Verify it landed in `demo-app` with the scoped SA:
```bash
kubectl get pods -n demo-app -o wide
kubectl get pod -n demo-app -l app.kubernetes.io/name=coder-workspace \
  -o jsonpath='{.items[0].spec.serviceAccountName}'; echo
```

![Workspace running](screenshots/03-workspace-running.png)
*Workspace `demo-ws` up and "Running", with code-server and a Terminal available.*

## 10. Prove the sandbox 

```bash
POD=$(kubectl get pods -n demo-app -l app.kubernetes.io/name=coder-workspace -o jsonpath='{.items[0].metadata.name}')

# Works — inside demo-app
kubectl exec -n demo-app "$POD" -- kubectl get pods -n demo-app

# Forbidden — other namespace
kubectl exec -n demo-app "$POD" -- kubectl get pods -n kube-system

# Forbidden — cluster-scoped
kubectl exec -n demo-app "$POD" -- kubectl get namespaces
```

The last two should return `Error from server (Forbidden)`. 

(via the Coder UI's terminal or `coder ssh demo-ws`)

```bash
whoami && pwd
kubectl get pods -n demo-app
kubectl get pods -n kube-system    # Forbidden
helm version
```

Same result — `demo-app` works, `kube-system` returns `Forbidden` — but
since this is the literal shell the AI agent operates in, it reads as
"the agent itself is sandboxed," not "we sandboxed it from the outside."

![Sandbox proof in the workspace terminal](screenshots/04-sandbox-proof.png)
*`kubectl get pods -n demo-app` succeeds, `kubectl get pods -n kube-system`
returns `Error from server (Forbidden)`, `helm version` works — all from the
workspace's own shell.*

## 11. Enable the Agents/Tasks chat tab (Claude Code)

This gives the Coder UI a built-in chat tab backed by Claude Code, so you can
drive the whole demo from the browser instead of an SSH terminal.

> **Already done in step 8:** `template-kubernetes/main.tf` already includes
> the `env_from` block (a/b below) and the `claude-code` module (c below), and
> you already created the `anthropic-api-key` secret and pushed the template
> in step 8. The (a)-(d) sub-steps below are shown for reference — describing
> what's already in the template/cluster — so skip straight to **(e) Verify**.

**a. Store the Anthropic API key as a Kubernetes Secret** (never put it in
the template in plaintext) — *done in step 8*:
```bash
kubectl create secret generic anthropic-api-key -n demo-app \
  --from-literal=ANTHROPIC_API_KEY='sk-ant-...' \
  --dry-run=client -o yaml | kubectl apply -f -
```

**b. Inject the secret into the workspace container** — *already present in
`main.tf`*. Inside the `container` block for `kubernetes_deployment_v1.main`,
next to the existing `CODER_AGENT_TOKEN` `env` block:
```hcl
env_from {
  secret_ref {
    name = "anthropic-api-key"
  }
}
```

**c. The Claude Code module** — *already present in `main.tf`*, right before
`resource "coder_app" "code-server"`:
```hcl
module "claude-code" {
  count               = data.coder_workspace.me.start_count
  source              = "registry.coder.com/coder/claude-code/coder"
  version             = "~> 2.0"
  agent_id            = coder_agent.main.id
  folder              = "/home/coder"
  install_claude_code = true
  claude_code_version = "latest"
}
```

**d. Push the template and rebuild the workspace** — *already done in step
8/9*. If you edit `main.tf` further and need to push changes to an existing
workspace:
```bash
cd template-kubernetes
coder templates push demo-k8s
coder update demo-ws
```
> `coder update` takes no `-y`/`--yes` flag — it's not interactive and will
> just proceed. (Don't bother trying `-y`/`--yes`, both are rejected.)

**e. Verify it worked:**
```bash
coder list                              # STATUS=Started, HEALTHY=true
POD=$(kubectl get pods -n demo-app -l app.kubernetes.io/name=coder-workspace -o jsonpath='{.items[0].metadata.name}')
kubectl exec -n demo-app "$POD" -- sh -c 'echo ANTHROPIC_API_KEY set: ${ANTHROPIC_API_KEY:+yes}'
```
The second command should print `ANTHROPIC_API_KEY set: yes`. In the Coder
UI, open `demo-ws` — you should now see a **Claude Code Web** button
alongside **code-server**.

![Claude Code Web app available on the workspace](screenshots/05-claude-code-web-app.png)
*The workspace page now shows a "Claude Code Web" button — this is the
per-workspace Claude Code module from step (c).*

**f. Configure a provider + model for the Coder Agents chat tab.** The
per-workspace "Claude Code Web" button opens a terminal-based Claude Code
session. For the full browser **Agents** chat tab (top nav → **Agents**),
Coder needs its own provider/model configuration — separate from the
per-workspace secret above:

1. Top nav → **Agents** → you'll see *"To chat with Coder Agents, set up a
   provider then add a model"* with **No Models Configured**:

   ![Agents tab with no models configured](screenshots/06-agents-no-models.png)

2. Click **provider** → **Providers** page shows **No providers configured**:

   ![Providers page, empty](screenshots/07-providers-empty.png)

3. **Add provider** → **Anthropic** → fill in:
   - Name: `anthropic`
   - Endpoint: `https://api.anthropic.com`
   - API key: your `ANTHROPIC_API_KEY`

   ![Add Anthropic provider form](screenshots/08-add-anthropic-provider.png)

4. Go to **Models** → **Add Model** → model identifier `claude-sonnet-4-6`
   (defaults fill in automatically), click **Add model**:

   ![Add model form](screenshots/08-add-model.png)

5. Back in the **Agents** chat tab, the model selector now shows
   `claude-sonnet-4-6` and you can chat:

   ![Coder Agents chat working](screenshots/09-chat-working.png)

## 12. Open the workspace

- In the Coder UI, open `demo-ws` → launch **code-server** (browser IDE) or
  the new **Agents/Tasks** chat tab, or from your terminal:
  `coder ssh demo-ws`.
- If you're using the chat tab, `ANTHROPIC_API_KEY` is already set in the
  workspace from step 11 — no extra setup needed.
- If you're driving from a terminal CLI instead, the same env var is already
  present, so `claude` (or whichever agent CLI) will pick it up automatically.

## 13. Copy the sample app into the workspace

A minimal Flask app with a **deliberate bug**: it reads `os.environ["APP_MODE"]`
on startup with no default, so if the first deployment manifest doesn't set
that env var, the pod will crash on boot → `CrashLoopBackOff`. This gives you
a real bug to fix on camera.

From your machine (not the workspace), `scp` the app in — **the SSH host is
`<workspace-name>.coder`**
(not `coder.<workspace-name>`):
```bash
scp -r $HOME/coder-demo/sample-app \
  demo-ws.coder:/home/coder/sample-app
```
> If `scp`/`ssh` doesn't work in your environment, run
> `coder ssh demo-ws` and recreate the two files (`app.py`,
> `requirements.txt`) directly — they're tiny (shown below).

`app.py`:
```python
import os
from flask import Flask

app = Flask(__name__)

# Deliberate bug for the demo: this env var is never set in the first
# deployment manifest, so the app crashes on startup -> CrashLoopBackOff.
APP_MODE = os.environ["APP_MODE"]


@app.route("/")
def index():
    return f"Hello from sample-app! mode={APP_MODE}\n"


@app.route("/healthz")
def healthz():
    return "ok\n"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
```

`requirements.txt`:
```
flask==3.0.3
```

## 14. The agent prompts — run these inside the workspace, in order

All of these run through the agent CLI (e.g. `claude`) inside `demo-ws`,
working directory `/home/coder/sample-app`. Because the workspace's
ServiceAccount is RBAC-scoped to `demo-app` only, every `kubectl`/`helm`
action the agent takes is confined to that namespace (proven in step 10).

> **Important — no Docker daemon in the workspace.** The workspace image
> includes the `docker` CLI but no daemon (`docker info` fails, no
> `/var/run/docker.sock`), and no container registry is configured. So
> "containerize" here means: write a `Dockerfile` (for the repo/CI, as
> real teams do), but **deploy** using a stock `python:3.11-slim` image
> that mounts the app source from a ConfigMap and runs
> `pip install -r requirements.txt && python app.py` as its command. This
> needs zero extra infra (ECR/IRSA) and was verified end-to-end. If you
> later want a real `docker build`/push flow, that requires setting up a
> scoped IRSA role for ECR push — out of scope for this guide.

> **If you're using the "Agents" chat tab (step 11f) instead of a terminal
> CLI**: submitting a prompt there spins up a **new, separate workspace**
> (e.g. `demo-k8s-25a5`) dedicated to that task — it shows up in
> **Workspaces** alongside `demo-ws`. This is expected; the agent gets its
> own sandboxed workspace per task, with the same `demo-app`-scoped RBAC.
> Mention this on camera — it's part of the "safely" story (each task is
> isolated in its own pod).
>
> ![A new workspace created by the agent for its task](screenshots/12-agent-new-workspace.png)

**Prompt 1 — containerize and deploy:**
```
Containerize this Flask app: write a Dockerfile for it (for future CI use).
Then, since there's no Docker daemon available here, deploy it to the
demo-app namespace using a ConfigMap containing app.py and requirements.txt,
and a Deployment using the python:3.11-slim image whose container command
installs the requirements and runs app.py. Add a Service, a readiness and
liveness probe on /healthz, and CPU/memory requests and limits. Deploy it
with kubectl.
```

![Agent running Prompt 1](screenshots/10-prompt1-running.png)
*The agent working through Prompt 1 — writing the Dockerfile, ConfigMap,
Deployment, and Service, then deploying with kubectl.*

**Expected result**: the agent reads `app.py`, sees it needs `APP_MODE`, sets
it correctly, and the pod comes up `Ready`. (Don't rely on the agent
forgetting this — a careful agent will get it right, which would silently
defeat the "crash" beat if you were counting on it.)

```bash
kubectl get pods -n demo-app
```

![CrashLoopBackOff confirmed](screenshots/11-crashloopbackoff.png)
*In this run the agent's own deploy hit `CrashLoopBackOff` (it didn't set
`APP_MODE`) — the agent then diagnosed `os.environ["APP_MODE"]` raising
`KeyError` and proposed the env var fix. If your run instead comes up
`Ready`, use the manual-break step below for a guaranteed crash.*

> **Manual step — break it on purpose.** After
> Prompt 1 succeeds, you (the presenter) remove the `APP_MODE` env var from
> the running Deployment to force `CrashLoopBackOff`:
> ```bash
> kubectl set env deployment/sample-app -n demo-app APP_MODE-
> kubectl get pods -n demo-app -w   # watch it go into CrashLoopBackOff
> ```

> **Start a new chat session before Prompt 2.** A fresh session has no memory
> of Prompt 1 or the manual break, so it has to genuinely `kubectl
> logs`/`describe` its way to the root cause — this reads as a real debugging
> session, not the agent recalling its own prior work. (Same applies
> optionally before Prompt 3.)

**Prompt 2 — debug and fix the crash:**
```
The sample-app pod in demo-app is in CrashLoopBackOff. Investigate using
kubectl logs and kubectl describe, find the root cause, fix it (manifest
and/or code), redeploy, and confirm the pod becomes Ready.
```

Confirm:
```bash
kubectl get pods -n demo-app -w
```

**Prompt 3 — convert to a Helm chart:**
```
Convert the Kubernetes manifests for sample-app into a Helm chart. Parameterize
the image tag, replica count, and APP_MODE value in values.yaml. Validate with
helm lint and helm template, then install it into demo-app as release
"sample-app".
```

![Agent scaffolding the Helm chart](screenshots/13-helm-chart-creation.png)
*The agent checks live cluster resources with `kubectl get all`, writes
`Chart.yaml`/`values.yaml`/templates, and validates with `helm lint`.*

Confirm:
```bash
helm list -n demo-app
helm get values sample-app -n demo-app
```

![Helm release installed successfully](screenshots/14-helm-install-success.png)
*`helm list -n demo-app` shows the `sample-app` release deployed, pod
`Running` and passing its `/healthz` probe.*

After the run, open **code-server** (or VS Code Desktop) to inspect what the
agent produced:

![Workspace file tree with the Helm chart](screenshots/15-vscode-chart-files.png)
*`sample-app/` now contains `Dockerfile`, `k8s.yaml`, `requirements.txt`,
`app.py`, and the new `chart/` directory (`Chart.yaml`, `values.yaml`,
`templates/`).*

![app.py with the deliberate APP_MODE bug highlighted](screenshots/16-apppy-bug.png)
*The deliberate bug the agent had to reckon with — `os.environ["APP_MODE"]`
on line 8 with no default.*

![Final verification in the terminal](screenshots/17-final-verification.png)
*Final state: the crashed pod's logs show the `KeyError: 'APP_MODE'`
traceback, the fixed pod is `Running`, and `helm list -n demo-app` confirms
the `sample-app` release at revision 1.*

**Prompt 4 — prove the sandbox (optional, great closing beat):**
```
Show me that you're sandboxed: run `whoami && pwd`, then
`kubectl get pods -n demo-app`, then `kubectl get pods -n kube-system`,
then `helm version`. Explain what the results mean.
```
The agent's own shell hits `Forbidden` on `kube-system` — this is the
"AI agent operating safely inside its boundary" 

![Agent running the sandbox-proof commands](screenshots/18-prompt4-sandbox-commands.png)
*The agent runs `whoami && pwd`, `kubectl get pods -n demo-app` (success),
and `kubectl get pods -n kube-system` → `Error from server (Forbidden)`.*

![Agent explaining the sandbox boundary](screenshots/19-prompt4-agent-explanation.png)
*The agent's own explanation: "The service account has no RBAC permissions
outside `demo-app`... the sandbox model in one sentence: the workspace is an
unprivileged Linux user inside a container, with a Kubernetes service account
scoped by RBAC to a single namespace — it can do everything needed for the
demo, and nothing outside it."* 

---

## Teardown (avoid ongoing AWS cost)

```bash
coder delete demo-ws --yes < /dev/null
eksctl delete cluster --name coder-demo --region us-east-1
```
