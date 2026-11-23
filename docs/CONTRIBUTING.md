# Contributing

Contributions to the Digital8, Hi8 and VHS pipeline are welcome. Report bugs and
propose changes in this fork's
[issue tracker](https://github.com/rrusk/Real-ESRGAN-Digital8-Hi8-VHS/issues).
For a bug, include the command, relevant log, tool versions, source metadata
and expected result. Do not post private recordings or identifying metadata.

Use a branch and open a pull request against this repository. Explain the
concrete problem, resulting behavior and validation. Follow the
[script conventions](../dev_tools/SCRIPT_CONVENTIONS.md) and describe current
constraints in documentation; development history belongs in Git.

## Writing user documentation

Lead with a task a new user can complete using one input and explicit options.
Use neutral filenames such as `capture01.dv` and explain which paths are inputs
and which the command creates. Introduce optional batch, comparison and
maintenance workflows separately from the basic preparation and upscale steps.

Keep machine-specific notes out of public guides. The repository ignores
`*_wrapper.*` files for local instructions and settings; public documentation
must remain usable without them. Preserve reusable technical findings in the
public guides with personal identifiers removed.

Examples that infer settings from filenames should state their naming policy
and tell readers how to adapt it. Keep the documented output and resume
contracts consistent with the implementation.

For each helper or repair, explain the problem it solves, how a user recognizes
that problem, whether normal processing already handles it, and when manual
use is appropriate. Include an explicit usage example and describe which files
it changes. Explain validation limits as well as successful checks.

Keep each paragraph focused on one topic. Separate explanations from commands
and exceptions, and use short lists when readers need to compare conditions or
limits. Break up dense paragraphs without removing technical reasons or
validation requirements.

## Local checks

Install the environment using [INSTALL.md](../INSTALL.md), then:

```bash
source venv/bin/activate
python -m pip install -r requirements-dev.txt
flake8 .
bash tests/run_pipeline_shell_tests.sh
bash tests/run_pipeline_functional_tests.sh
python -m pytest -q
```

The Python suite includes inference utility tests that need downloaded weights.
Upstream training-model tests require CUDA and are explicitly skipped on CPU
hosts; the other Python tests run on a CPU.

Shell suites use generated media and require FFmpeg, GPAC, mkvtoolnix, jq, and
ShellCheck. GitHub Actions runs lint and these suites on both supported Python
versions.

GPU inference needs a separate run of `dev_tools/smoke_test.sh` on an NVIDIA
machine.

Use `pre-commit install` for the optional hooks in
[.pre-commit-config.yaml](../.pre-commit-config.yaml). The lint configuration is
in [setup.cfg](../setup.cfg).

## Upstream research and training

The retained [upstream README](../README_Original.md) and training guides
describe Real-ESRGAN research and upstream interfaces. General model or training
contributions can also be proposed to
[upstream Real-ESRGAN](https://github.com/xinntao/Real-ESRGAN). Pipeline issues
and pull requests belong in this fork.
