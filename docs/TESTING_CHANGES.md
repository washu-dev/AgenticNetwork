# Testing Your Changes Without Touching Production

There is **one** production system everyone shares — the **prod** deployment. You never test on
it. Instead you spin up your **own private copy** — a **test** instance — that runs right beside
prod but is completely walled off: different ports, its own throwaway database, and no public web
address.

## One-time setup

- A Docker login to the shared image account — `docker login -u di2washu` — **only if** you'll
  rebuild an image (see Step 2).

## Step 1 — Put your change on a git branch

A *branch* keeps your work separate from everyone else's. From the repo:
```bash
git checkout -b my-change
#  ...edit files...
git add -A && git commit -m "describe what you changed"
git push -u origin my-change
```

## Step 2 — Do you need to rebuild a Docker image?

Each service (planner, email agent, …) runs inside a Docker *image* — a frozen snapshot of its
code taken at build time. There are two cases:

- **You changed only config or deploy scripts** (`deployment/.env`, the `*.sbatch` files,
  `ads_daemon.yaml`): **no Docker needed.** Just get your branch onto the cluster and skip to
  Step 3:
  ```bash
  cd /engrfs/project/di2/AgenticNetwork
  git fetch && git checkout my-change && git pull
  ```

- **You changed a service's code** (e.g. Python under `planner_agent/`, or an agent in the
  `agent-directory` repo): the running container uses the **baked-in** copy, so a `git pull` alone
  won't take effect — you must rebuild **that one service's** image, under **your own tag** so you
  don't overwrite the image prod uses (`:latest`). On your laptop:
  ```bash
  docker login -u di2washu
  IMAGE_TAG=mytag bash deployment/build_and_push.sh planner   # <-- the service you changed
  ```
  (The four infra images — `planner`, `ads`, `postgres`, `registration` — build from **this**
  repo; the six specialist agents build from the **`agent-directory`** repo.)

## Step 3 — Launch YOUR test instance

On the cluster:
```bash
# make sure the model server is running (once per session):
sbatch deployment/start_vllm.sbatch          # wait until it shows 'R' in:  squeue -u $USER

# launch your isolated copy:
deployment/deploy.sh test

# if you rebuilt an image in Step 2, tell test to use YOUR tag — e.g. the planner:
PLANNER_IMAGE_TAG=mytag deployment/deploy.sh test
```
This runs beside prod on shifted ports (everything +10000) with its own throwaway database.

## Step 4 — Open it in your browser (SSH tunnel)

Your test instance has no public URL — you reach it through the cluster:
```bash
# 1. find the node it landed on:
squeue -u $USER -n agenticnetwork-test -h -o '%N'      # e.g. c2-gpu-013  =>  <test-node>

# 2. from your LAPTOP, tunnel to it (fill in your key / login host / node):
ssh -J <wustlkey>@<login-host> <wustlkey>@<test-node> -L 18090:localhost:18090
```
Leave that terminal open, then browse **http://localhost:18090**. You're dropped straight in as a
demo user (test instances have no sign-in).

## Step 5 — Tear it down when done

```bash
scancel -u $USER -n agenticnetwork-test
```

## Why this can't hurt prod

- **Different ports** — test = prod + 10000, so nothing collides even on the same machine.
- **Separate database** — test uses a throwaway DB on scratch disk; it physically **cannot** read
  or write prod's real database (which holds real users' saved credentials).
- **No public URL** — `https://agenticweb.engr.wustl.edu` always points only at prod; your test is
  reachable *only* through your own SSH tunnel.
- **A safety lock** — if you accidentally try to start a second prod, the deploy refuses.

## Gotchas

- **The test node changes every deploy** — re-run the `squeue` in Step 4 each time.
- **"Connect Google" won't work in test** — that login flow only works on prod's real URL.
  Everything else does.
- **Rebuild only what you changed** — a personal `IMAGE_TAG` on one service; the rest of your
  test instance just uses prod's `:latest` images.

## When your change works

Commit and push your branch (Step 1), then ask for it to be merged into `main`. **Don't merge to
`main` just to test** — that's exactly what the test instance is for.

---

More detail on how any of this works: [`MULTI_CONTRIBUTOR.md`](MULTI_CONTRIBUTOR.md) (the design)
and [`../deployment/README.md`](deployment/engr/README.md) (the full runbook).
