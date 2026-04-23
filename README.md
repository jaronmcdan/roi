# ROI (Remote Operational Equipment)

ROI is a Raspberry Pi focused bridge between a CAN bus and lab/test instruments.
It receives CAN control frames, applies them to connected devices, and publishes
readback/status frames back onto CAN.

## Start Here

- Documentation index: [`docs/README.md`](docs/README.md)
- Recommended path: overview -> install -> config -> run

## Developer Quick Start

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -U pip
pip install -e ".[dev]"
roi
```

Run tests:

```bash
python -m pytest
```

Check installed ROI version (+git hash when available):

```bash
roi --version
```

## Raspberry Pi Install

```bash
git clone <your-repo-url> roi
cd roi
sudo bash scripts/pi_install.sh --easy
sudo /opt/roi/.venv/bin/roi
```

Install and enable service:

```bash
sudo bash /opt/roi/scripts/service_install.sh --prefix /opt/roi --enable --start
sudo journalctl -u roi -f
```

## Offline Pi Deploy (build on PC, install on Pi)

```bash
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\make_pi_dist.ps1 -Offline -Deploy -InstallOnPi -RebootAfterInstall -DeployHost 192.168.45.1 -DeployUser pete
```

Build a tarball that includes a Python wheelhouse:

```bash
./scripts/make_pi_dist.sh --offline
# produces dist/roi-<sha-or-timestamp>.tar.gz
```

Windows PowerShell:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\make_pi_dist.ps1 -Offline
```

Build and upload directly to a Pi at `192.168.45.1` (prompts for SSH user/password):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\make_pi_dist.ps1 -Offline -Deploy
```

Build, upload, install on Pi, then reboot Pi:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\make_pi_dist.ps1 -Offline -Deploy -InstallOnPi -RebootAfterInstall
```

If your Pi is 32-bit (armv7), add:

```powershell
-PiPlatform manylinux2014_armv7l -PiPythonVersion 3.11
```

If you know the Pi Python version, you can lock it (for smaller wheelhouse):

```powershell
-PiPythonVersion 3.13
```

Copy to the Pi, extract, then install with offline pip mode:

```bash
sudo bash scripts/pi_install.sh --offline
```

Notes:

- `--offline` installs Python packages from `deploy/wheelhouse` without PyPI access.
- If the Pi is fully air-gapped, avoid `--easy` unless your apt sources are reachable
  (or install OS packages another way first).

## Updating From GitHub

### Pi service install (`/opt/roi`)

From the checkout you originally cloned (for example `~/roi`):

```bash
cd ~/roi
git pull --ff-only
sudo bash scripts/pi_install.sh --prefix /opt/roi
sudo bash /opt/roi/scripts/service_install.sh --prefix /opt/roi --start
sudo journalctl -u roi -n 50 --no-pager
```

Notes:

- `pi_install.sh` re-syncs code into `/opt/roi` and reinstalls the package in `/opt/roi/.venv`.
- `/etc/roi/roi.env` is preserved (it is only created if missing).

### Developer checkout update

```bash
git pull --ff-only
pip install -e ".[dev]"
```

## Diagnostics

```bash
roi-visa-diag
roi-mmter-diag
roi-mmter-diag --roi-cmds --style func
roi-can-diag --duration 5
roi-mrsignal-diag --read-count 3
roi-autodetect-diag
roi-env-hardcode
```

### Running diagnostics when service is enabled

Most diagnostics should be run with the service stopped, because ROI already
holds the same serial/VISA devices.

```bash
sudo systemctl stop roi

sudo /opt/roi/.venv/bin/roi-visa-diag
sudo /opt/roi/.venv/bin/roi-mmter-diag
sudo /opt/roi/.venv/bin/roi-mmter-diag --roi-cmds --style func
sudo /opt/roi/.venv/bin/roi-mmter-diag --roi-cmds --roi-cmds-mode runtime --style func
sudo /opt/roi/.venv/bin/roi-mmter-diag --roi-cmds --roi-cmds-mode legacy --style func
sudo /opt/roi/.venv/bin/roi-mrsignal-diag --read-count 3
sudo /opt/roi/.venv/bin/roi-autodetect-diag
sudo /opt/roi/.venv/bin/roi-can-diag --duration 5

sudo systemctl start roi
sudo journalctl -u roi -f
```

If you must keep ROI running, only `roi-can-diag` may be safe in listen-only
style (SocketCAN, no `--send-id`, no `--setup`).

If `roi-mmter-diag --roi-cmds` shows persistent unsupported meter commands on
your firmware, you can gate those command paths in `/etc/roi/roi.env`:

```bash
MMETER_LEGACY_MODE0_ENABLE=0
MMETER_EXT_SET_RANGE_ENABLE=0
MMETER_EXT_SECONDARY_ENABLE=0
# or disable all extended meter controls:
# MMETER_EXT_CTRL_ENABLE=0
```

## Optional Web Dashboard

```bash
ROI_WEB_ENABLE=1 ROI_WEB_PORT=8080 roi
```

Browse to `http://<pi-hostname>:8080/`.
