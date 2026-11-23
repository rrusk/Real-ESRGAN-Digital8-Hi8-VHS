# Installation

The supported pipeline environment is Linux with GNU command-line tools, Bash,
Python **3.10 or 3.11**, and an NVIDIA GPU with a working CUDA driver. The pinned
NumPy 1.25.0 supports Python only through 3.11; the pipeline uses 3.10 syntax.
Commands below run from the repository root.

## Automated setup

```bash
dev_tools/setup_env.sh --system-deps
source venv/bin/activate
```

Setup checks prerequisites, creates or reuses `venv/`, installs dependencies,
patches BasicSR imports, installs this package, downloads weights, verifies the
environment, and runs an isolated GPU smoke test. It stops on failure.

Use `--python python3.11` if `python3` is outside the supported range. An
existing environment uses its own interpreter; `--force` recreates it with the
selected interpreter after checking that it is safe to remove. Unsupported
interpreters are rejected before the environment is created or removed.

```bash
dev_tools/setup_env.sh --python python3.11 --dry-run
```

Omit `--system-deps` when system packages are already installed. Missing commands
are reported with their package names. `--skip-models` skips weights and the
smoke test; `--skip-smoke` skips only the GPU test. Both are setup options, not
proof that inference works. See `--help` for all options.

## Hardware and driver

Check the driver before installing:

```bash
nvidia-smi
```

The reported CUDA version is the newest CUDA API supported by the driver,
not the runtime installed in Python. PyTorch wheels include their CUDA runtime;
the driver must support that runtime.

These configurations have been exercised by the project:

| GPU | Driver | Python stack |
|---|---|---|
| RTX 4060 Ti 8 GB | 595.99.02 | torch 2.11.0 + CUDA 12.8, torchvision 0.26.0, torchaudio 2.11.0 |
| GTX 1060 6 GB | Not recorded | torch 2.0.1 + CUDA 11.7, torchvision 0.15.2, torchaudio 2.0.2 |

`requirements.txt` installs the RTX configuration. `verify_env.py` recognizes
both, but setup does not install the GTX configuration. CUDA 12.8 is the
validated stack here, not an intrinsic requirement of the RTX 4060 Ti.
[NVIDIA's Ada compatibility guide](https://docs.nvidia.com/cuda/ada-compatibility-guide/)
describes compatibility with earlier CUDA builds.

Standard-definition 2× processing has been exercised without tiling on these
cards. Memory needs depend on the model, dimensions, scale and other GPU users.
Test a representative clip before committing to a long capture.

## Manual installation

### 1. System tools

The package names below are for Debian/Ubuntu systems; availability varies by
release. Ubuntu 22.04 is the CI platform. Install an equivalent GPAC build
providing `MP4Box` if your distribution does not package it.

```bash
sudo apt install ffmpeg python3-venv python3-pip curl jq mkvtoolnix gpac
sudo apt install mediainfo  # optional: faster CRF inspection
```

Required commands are `ffmpeg`, `ffprobe`, `python3`, `curl`, `jq`, `mkvmerge`
and `MP4Box`. `mediainfo` is optional because CRF checks have a byte-scan
fallback. Model-comparison grids also need FFmpeg's `drawtext` filter.

### 2. Python packages

Choose an installed Python 3.10 or 3.11 interpreter. For example:

```bash
python3.11 -m venv venv
source venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install -c requirements.txt numpy cython torch torchvision torchaudio
python -m pip install --no-build-isolation --no-deps -c requirements.txt basicsr
python -m pip install -r requirements.txt
python dev_tools/patch_basicsr.py
python -m pip install -e . --no-deps
```

The CUDA wheel index is in `requirements.txt`; other packages come from PyPI.
`-c requirements.txt` applies its pins and index while installing only the
named packages.

Install BasicSR's build requirements first, then build it without isolation
against that stack. This avoids a separate Torch download for its build
environment.

The final `-r requirements.txt` installs and checks all runtime dependencies,
including BasicSR's dependencies omitted by `--no-deps`.

The model and subprocess paths require running the pipeline from this checkout.

### 3. BasicSR import compatibility

`dev_tools/patch_basicsr.py` repairs two imports in BasicSR 1.4.2:

| File | Installed import | Compatible import |
|---|---|---|
| `data/degradations.py` | `torchvision.transforms.functional_tensor` | `torchvision.transforms.functional` |
| `metrics/niqe.py` | `scipy.ndimage.filters` | `scipy.ndimage` |

Torchvision 0.26 lacks `functional_tensor`, so an unpatched BasicSR cannot load
in the validated environment. BasicSR imports its dataset modules during
startup, including code that reaches this import. The SciPy patch uses the
public namespace and avoids its deprecated alias. `scipy<2` remains a dependency
constraint; the import patch alone does not establish SciPy 2 compatibility.

The patch is idempotent and refuses unfamiliar source. Run it again after
reinstalling BasicSR. `--check` reports status without writing files.

### 4. Weights and verification

```bash
dev_tools/fetch_models.sh
python verify_env.py
dev_tools/smoke_test.sh
```

Weights come from upstream releases and are ignored by Git. Downloads skip
nonempty existing files; use `--force` to replace them. Temporary downloads
are renamed into place only after successful transfer.

Verification checks imports, versions, CUDA and available weights. The smoke
test generates a tiny audio/video clip, runs the pipeline at 2×, and verifies
output dimensions and audio. All smoke-test files and its active-run pointer
use a private temporary root. `--keep` retains that root for diagnosis.

## Optional RIFE interpolation

RIFE needs its own Vulkan binary and a working Vulkan GPU driver. It is not
installed by setup and is not used by default. From the repository root:

```bash
curl -fLO https://github.com/nihui/rife-ncnn-vulkan/releases/download/20221029/rife-ncnn-vulkan-20221029-ubuntu.zip
unzip rife-ncnn-vulkan-20221029-ubuntu.zip
ln -s rife-ncnn-vulkan-20221029-ubuntu rife-ncnn-vulkan
chmod u+x rife-ncnn-vulkan/rife-ncnn-vulkan
```

Use a roughly 25/30 fps master prepared with `--mode0`, then add `--rife` to the
pipeline. The binary is a separate project; see its
[upstream repository](https://github.com/nihui/rife-ncnn-vulkan) for platform support.

## Troubleshooting

| Symptom | Action |
|---|---|
| Unsupported Python | Select Python 3.10 or 3.11 with setup's `--python` option. |
| Missing `torchvision.transforms.functional_tensor` | Run `python dev_tools/patch_basicsr.py` inside the environment. |
| SciPy deprecated-import warning from BasicSR | Rerun the same patch script for its SciPy import repair. |
| Cannot import `realesrgan` | Run the editable install from the repository root. |
| CUDA unavailable | Check `nvidia-smi`, driver/runtime compatibility, GPU access, and that a CUDA wheel was installed. |
| Missing weights | Run `dev_tools/fetch_models.sh`. |
| Torchvision `pretrained=` warning in model tests | BasicSR's training tests use the deprecated VGG argument; the test configuration filters those specific warnings. |
| Missing system command | Install its named package or use `--system-deps`. |
