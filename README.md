# Github-Actions-Rancher-Credential-Renewal

This repository contains a small utility for automating Kubernetes cluster access renewal
 (e.g. Rancher‑managed kubeconfigs) on a scheduled basis, typically via cron or a similar scheduler.

The core of the repository is a Python script which:

- Logs in to Rancher (or another configured endpoint).
- Renews or regenerates Kubernetes access credentials.
- Writes an updated `kubeconfig` file.
- Should run periodically to keep GitHub Actions runners authenticated for CI/CD.

A container image is provided as a ready‑made runtime environment for this script.
It exists purely for convenience and is not required if you prefer to run the script directly on a host that already has Python
and the needed dependencies.

This script, README.md and most of the other files in this repository were generated using GPT 4o on acdemic ai May 2026.
The README.md was edited quite a lot.

## Features

- Automated renewal of Kubernetes access credentials.
- Configuration driven (no hard‑coded secrets or endpoints).
- Designed to be triggered by cron, systemd timers, or GitHub Actions schedules.
- Optional containerized runtime.

## Repository Structure

- `Github-Actions-Rancher-Credential-Renewal.py`  
  Main Python script that performs the credential renewal.

- `config.yaml` (copy `config.yaml.example` and fill in your credentials)  
  Configuration file describing how to connect to Rancher and where to write the renewed kubeconfig.

- `pyproject.toml`, `uv.lock`
  Packages managed using uv

## Configuration

See [config.yaml.example](config.yaml.example)

## Python Script Usage

Run the script directly with Python:

```bash
uv run Github-Actions-Rancher-Credential-Renewal.py
```
## Running via Container

The repository includes a `Dockerfile` that packages:

- A minimal base image
- Python runtime
- Required Python dependencies
- `Github-Actions-Rancher-Credential-Renewal.py`

This container is intended as a runtime wrapper only.
You still provide configuration and credentials at runtime.

You can substitute `podman` for `docker` if this is already installed.

### Build the image

```bash
docker build -t github-actions-rancher-credential-renewal .
```

### Run the container

Mount your config file and, if needed, the path where the kubeconfig should be written:

```bash
docker run --rm \
  -v /etc/rancher-renewal/config.yaml:/app/config.yaml:ro
```

In this example:

- `/etc/rancher-renewal/config.yaml` on the host is mounted read‑only into `/app/config.yaml` inside the container.

### Run using podman and systemd

E. g. `/etc/containers/systemd/rancher-github-credential-renewal.container`

```systemd
[Unit]
Description=Rancher GitHub credential renewal

[Container]
Image=ghcr.io/acdh-oeaw/github-actions-rancher-credential-renewal/main:main
# Environment="HTTPS_PROXY=http://proxy.example.org:3128"
# Mount host config file into the container as /app/config.yaml (read only)
Volume=/etc/rancher-github-credential-renewal/config.yaml:/app/config.yaml:ro
# Enable Podman auto-update (pull from registry when image changes)
AutoUpdate=registry

[Service]
Type=oneshot
```

## Scheduling using systemd

E. g. `/etc/systemd/system/rancher-github-credential-renewal.timer`
```systemd
[Unit]
Description=Run Rancher GitHub credential renewal daily

[Timer]
OnCalendar=*-*-* 04:00:00
Persistent=true
RandomizedDelaySec=10m
Unit=rancher-github-credential-renewal.service

[Install]
WantedBy=timers.target
``` 

## Scheduling with Cron

On a host where Python is installed or where you use the provided container, you can run the script periodically.

### Direct Python

Edit the crontab:

```bash
crontab -e
```

Add a job, for example to run every 6 hours:

```cron
0 */6 * * * cd /path/to/repo && /path/to/repo/.venv/bin/python /path/to/repo/Github-Actions-Rancher-Credential-Renewal.py >> /path/to/repo/cron.log 2>&1
```

Where:

- `/path/to/repo` is the directory containing `Github-Actions-Rancher-Credential-Renewal.py`
- `/path/to/repo/.venv` is the uv-created virtual environment directory (containing `bin/python`)
- `cron.log` is where stdout/stderr from the script will be appended

### Using the Container

```cron
0 */6 * * * docker run --rm \
  -v /etc/rancher-renewal/config.yaml:/app/config.yaml:ro
  >> /var/log/rancher-renewal.log 2>&1
```

## Security Considerations

- `config.yaml` contains sensitive token. Limit exposure as much as possible
- This is meant to run on a separate system within the organization trustesd intranet

## Reviewing all generated kubeconfigs

Start a kube shell on the cluster Rancher is running on and execute

```shell
kubectl get kubeconfig -o wide
# NAME               TTL   TOKENS   STATUS     AGE     USER           CLUSTERS       DESCRIPTION
# kubeconfig-6f747   2d    1/1      Complete   12m     m-q6724        c-m-6hwgqq2g   GitHub Actions kubeconfig for repo oener/repo
```

You can delete the kubeconfig there using

```shell
kubectl delete kubeconfig kubeconfig-6f747
```

## License

[MIT](LICENSE)
