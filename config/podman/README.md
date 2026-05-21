# Podman / systemd based setup

* `/etc/containers/systemd/rancher-github-credential-renewal.container`
* `/etc/systemd/system/rancher-github-credential-renewal.timer`

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now rancher-github-credential-renewal.timer
```
