#!/usr/bin/env bash
# Conformance tests for this repository's AI-pipeline shell tools.
# Version: 1.0.0
#
# These are interface and hygiene tests. They check that every pipeline script
# obeys the contract in dev_tools/SCRIPT_CONVENTIONS.md. They deliberately do
# NOT run the pipeline: that needs a GPU, a Python environment, real captures
# and hours of runtime.
#
# Every check runs a real invocation where it can, so a script that does work
# before parsing its arguments is caught here rather than by a user.
#
# Usage: tests/run_pipeline_shell_tests.sh [OPTIONS]
#   -v, --verbose   Print every passing check, not just failures.
#   --only PATTERN  Restrict to scripts whose path contains PATTERN.
#   -V, --version   Print version and exit.
#   -h, --help      Show this help and exit.

set -uo pipefail

VERSION="1.0.0"
VERBOSE=false
ONLY=""


ok()   { PASS=$((PASS+1)); $VERBOSE && printf '  [ok  ] %-46s %s\n' "$1" "${2-}"; return 0; }
bad()  { FAIL=$((FAIL+1)); FAILURES+=("$1 -- ${2-}"); printf '  [FAIL] %-46s %s\n' "$1" "${2-}"; }
# Always printed, verbose or not, and always with a reason. A skip is a check
# that did not run; counting them silently and reporting the total at the end
# lets a suite look green over work it declined to do.
skip() { SKIP=$((SKIP+1)); SKIPS+=("$1 -- ${2-}"); printf '  [skip] %-46s %s\n' "$1" "${2-}"; return 0; }

# Enumerate tracked and untracked shell tools while excluding ignored files.
# Libraries have no command-line interface and receive only library checks.
shell_files() { git ls-files --cached --others --exclude-standard '*.sh' | sort -u; }

# Check the fork's Python command-line tools. Upstream modules and inference
# entry points import torch and are covered separately by the Python tests.
python_files() {
    git ls-files --cached --others --exclude-standard '*.py' \
        | grep -vE '^(realesrgan|scripts|options|experiments|venv|weights)/' \
        | grep -vE '^(setup|cog_predict|inference_realesrgan|inference_realesrgan_video)\.py$' \
        | grep -vE '^tests/' \
        | sort -u
}
# A harness is still a tool: its work belongs in main() behind the standard
# guard, like every other script here. Only `set -euo pipefail` is genuinely
# out of reach -- -e aborts the run on the first failing check, which is the
# opposite of what a suite must do.
is_harness() { [[ $1 == tests/run_*.sh ]]; }

PASS=0; FAIL=0; SKIP=0
declare -a FAILURES=()
declare -a SKIPS=()

main() {
    while (($#)); do
        case $1 in
            -v|--verbose) VERBOSE=true; shift ;;
            --only) [[ $# -ge 2 ]] || { printf '[ERROR] --only requires a pattern\n' >&2; exit 2; }
                    ONLY=$2; shift 2 ;;
            -V|--version) printf '%s %s\n' "${0##*/}" "$VERSION"; exit 0 ;;
            -h|--help) sed -n '3,22p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
            *) printf '[ERROR] Unknown option: %s\n' "$1" >&2
               printf "Try '%s --help' for more information.\n" "${0##*/}" >&2; exit 2 ;;
        esac
    done

    ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
    cd "$ROOT" || exit 1

    mapfile -t TOOLS < <(shell_files | grep -v '_lib\.sh$')
    mapfile -t LIBS  < <(shell_files | grep    '_lib\.sh$')

    if [[ -n $ONLY ]]; then
        mapfile -t TOOLS < <(printf '%s\n' "${TOOLS[@]}" | grep -F -- "$ONLY")
        mapfile -t LIBS  < <(printf '%s\n' "${LIBS[@]}"  | grep -F -- "$ONLY")
    fi

    printf 'Pipeline conformance: %d shell tool(s), %d librar(y/ies), %d Python tool(s)\n\n' \
        "${#TOOLS[@]}" "${#LIBS[@]}" "$(python_files | wc -l)"

    # Finding nothing is a failure, not a pass. The file lists come from git
    # ls-files, so outside a repository -- a copy staged with tar rather than git,
    # for instance -- they come back empty, every per-tool loop iterates zero times,
    # and the suite reports success having checked three things instead of 163. A
    # gate that cannot tell "everything passed" from "nothing ran" is worse than no
    # gate, because it is trusted.
    if [[ -z $ONLY ]] && (( ${#TOOLS[@]} == 0 || $(python_files | wc -l) == 0 )); then
        printf '[FATAL] No tools found to check. This suite enumerates files with\n'
        printf '        git ls-files, so it must run inside the repository work tree.\n' >&2
        exit 1
    fi

    # ------------------------------------------------------------------ hygiene
    printf -- '--- Static hygiene ---\n'
    have_shellcheck=false
    command -v shellcheck >/dev/null 2>&1 && have_shellcheck=true

    for f in "${TOOLS[@]}" "${LIBS[@]}"; do
        [[ -n $f ]] || continue
        if bash -n "$f" 2>/dev/null; then ok "parses" "$f"; else bad "parses" "$f"; fi

        if $have_shellcheck; then
            out=$(shellcheck -S error -x "$f" 2>&1)
            if [[ -z $out ]]; then ok "shellcheck (severity error)" "$f"
            else bad "shellcheck (severity error)" "$f: $(head -2 <<<"$out" | tr '\n' ' ')"; fi
        else
            skip "shellcheck (severity error)" "shellcheck not installed"
        fi
    done

    # Exactly one convention is out of a harness's reach, and it is exempted by
    # itself rather than by waiving the whole group. `set -e` aborts the shell
    # on the first command that fails, which for a suite means stopping at the
    # first failing check and never reporting the rest -- the opposite of its
    # job. Everything else applies: a harness is a script someone runs, and it
    # gets a guarded main() like any other.
    for f in "${TOOLS[@]}"; do
        [[ -n $f ]] || continue
        if is_harness "$f"; then
            skip "set -euo pipefail" \
                 "$f: a suite must not set -e; it would stop at the first failing check"
        elif grep -qE '^set -euo pipefail' "$f"; then ok "set -euo pipefail" "$f"
        else bad "set -euo pipefail" "$f"; fi

        # The guarded main() is what makes a partially-edited script safe to have
        # running, and what makes the file safe to source.
        if grep -qE '^main\(\) *\{' "$f"; then ok "defines main()" "$f"
        else bad "defines main()" "$f"; fi

        if grep -qF 'BASH_SOURCE[0]} == "$0"' "$f" && grep -qE '^\s*exit \$\?' "$f"; then
            ok "guarded main call with exit \$?" "$f"
        else
            bad "guarded main call with exit \$?" "$f"
        fi
    done

    # ------------------------------------------------------------------ interface
    printf -- '\n--- Command-line interface ---\n'
    for f in "${TOOLS[@]}"; do
        [[ -n $f ]] || continue
        for flag in -h --help; do
            help_out=$(timeout 30 bash "$f" "$flag" </dev/null 2>/dev/null); rc=$?
            if (( rc == 0 )) && [[ -n $help_out ]]; then
                ok "$flag prints to stdout and exits 0" "$f"
            else
                bad "$flag prints to stdout and exits 0" "$f (rc=$rc, $(wc -c <<<"$help_out") bytes)"
            fi
        done

        # Section order, for the sections a given tool actually uses.
        help_out=$(timeout 30 bash "$f" --help </dev/null 2>/dev/null)
        order=$(grep -oE '^(Usage|Description|Arguments|Options|Controls|Examples|Dependencies)' \
            <<<"$help_out" | tr '\n' ' ')
        canonical='Usage Description Arguments Options Controls Examples Dependencies'
        remaining=$canonical
        misordered=false
        for section in $order; do
            case " $remaining " in
                *" $section "*) remaining=${remaining#*"$section"} ;;
                *) misordered=true; break ;;
            esac
        done
        if $misordered; then bad "help sections in documented order" "$f: $order"
        else ok "help sections in documented order" "$f"; fi

        # Options come before positionals in the usage line, and every parameter is
        # a named option: the only positionals are a tool's primary inputs. A bare
        # setting mixed among options -- prepare_video.sh once took the bottom mask
        # as a positional integer -- is indistinguishable from a typo.
        # Two layouts are in use: "Usage: tool [OPTIONS] ARG" on one line, and a
        # bare "Usage:" followed by indented synopsis lines. Reading only the line
        # matching ^Usage: found nothing but the word "Usage:" in the second style
        # and skipped the check -- silently, on tools that do take options.
        # Every synopsis line is considered, and the one carrying [OPTIONS] wins.
        usage_block=$(sed -n '/^Usage:/,/^[^[:space:]]/p' <<<"$help_out" \
            | sed '1s/^Usage:[[:space:]]*//')
        usage_line=$(grep -m1 -E '\[(OPTIONS|options)\]' <<<"$usage_block")
        [[ -n $usage_line ]] || usage_line=$(grep -m1 -E '^Usage:' <<<"$help_out")
        if [[ $usage_line == *'[OPTIONS]'* || $usage_line == *'[options]'* ]]; then
            # Walk the tokens before [OPTIONS]. A word is a positional only if it
            # is not itself an option and not the VALUE of the option before it --
            # `--saved-at TYPE=DIR` is an option taking a value, not a positional.
            stray=$(awk -v line="$usage_line" 'BEGIN {
                sub(/^Usage:[[:space:]]*/, "", line)
                n = split(line, word, /[[:space:]]+/)
                prev_was_option = 0
                for (i = 2; i <= n; i++) {          # skip word 1: the program name
                    if (word[i] ~ /^\[?(OPTIONS|options)\]?$/) break
                    if (word[i] == "..." || word[i] == "|") continue
                    if (word[i] ~ /^-/) { prev_was_option = 1; continue }
                    if (prev_was_option) { prev_was_option = 0; continue }
                    if (word[i] ~ /^<.*>$/ || word[i] ~ /^\[?[A-Z][A-Z_0-9]*\]?$/) {
                        print word[i]; exit
                    }
                }
            }')
            if [[ -n $stray ]]; then
                bad "options precede positionals in usage" "$f: '$stray' before [OPTIONS] -- $usage_line"
            else
                ok "options precede positionals in usage" "$f"
            fi
        else
            if ! grep -qE '^Usage:' <<<"$help_out"; then
                skip "options precede positionals in usage" \
                     "$f: --help prints no Usage: line"
            else
                skip "options precede positionals in usage" \
                     "$f: usage names its options explicitly, no [OPTIONS] token"
            fi
        fi

        # A command-line error is exit 2, names the tool's help, and does not dump
        # the whole help text over the message the user needs to read.
        err_out=$(timeout 30 bash "$f" --definitely-not-a-real-option </dev/null 2>&1); rc=$?
        if (( rc == 2 )); then ok "unknown option exits 2" "$f"
        else bad "unknown option exits 2" "$f (rc=$rc)"; fi

        if grep -qE "Try .*--help" <<<"$err_out"; then
            ok "unknown option points at --help" "$f"
        else
            bad "unknown option points at --help" "$f"
        fi

        if grep -qE '^(Options|Arguments):' <<<"$err_out"; then
            bad "unknown option does not print full help" "$f"
        else
            ok "unknown option does not print full help" "$f"
        fi
    done

    # ------------------------------------------------------------------ sourcing
    printf -- '\n--- Sourcing ---\n'
    for f in "${TOOLS[@]}"; do
        [[ -n $f ]] || continue
        # Only attempted where the guard is present. Sourcing an unguarded tool
        # would RUN it, which is the very thing the guard exists to prevent, so a
        # missing guard is reported above rather than demonstrated here.
        if ! grep -qF 'BASH_SOURCE[0]} == "$0"' "$f"; then
            skip "sourcing defines main without running" "$f: no guard to rely on"
            continue
        fi
        out=$(cd "$(mktemp -d)" && timeout 30 bash -c \
            'set +e; source "$1" >/dev/null 2>&1; declare -F main >/dev/null && echo DEFINED' \
            _ "$ROOT/$f" 2>/dev/null)
        if [[ $out == DEFINED ]]; then ok "sourcing defines main without running" "$f"
        else bad "sourcing defines main without running" "$f"; fi
    done

    for f in "${LIBS[@]}"; do
        [[ -n $f ]] || continue
        if ( timeout 30 bash -c 'source "$1"' _ "$ROOT/$f" >/dev/null 2>&1 ); then
            ok "library sources cleanly" "$f"
        else
            bad "library sources cleanly" "$f"
        fi
    done

    # ------------------------------------------------------------------ python
    printf -- '\n--- Python tools ---\n'
    mapfile -t PYTOOLS < <(python_files)
    [[ -n $ONLY ]] && mapfile -t PYTOOLS < <(printf '%s\n' "${PYTOOLS[@]}" | grep -F -- "$ONLY")

    # Prefer the repository venv: a tool may import a third-party module, and the
    # system interpreter would report an ImportError as a defect in the tool.
    PYTHON=python3
    [[ -x venv/bin/python ]] && PYTHON=venv/bin/python

    for f in "${PYTOOLS[@]}"; do
        [[ -n $f ]] || continue

        if "$PYTHON" -m py_compile "$f" 2>/dev/null; then ok "compiles" "$f"
        else bad "compiles" "$f: $("$PYTHON" -m py_compile "$f" 2>&1 | tail -1)"; fi

        # A tool with a command line must answer --help without doing any work.
        # Anything that is imported rather than run is exempt: it has no argv.
        if grep -q 'argparse\|sys.argv' "$f"; then
            out=$(timeout 30 "$PYTHON" "$f" --help 2>&1); rc=$?
            if (( rc == 0 )) && grep -qi 'usage' <<<"$out"; then
                ok "--help exits 0 and prints usage" "$f"
            else
                bad "--help exits 0 and prints usage" "$f: exit $rc, $(head -1 <<<"$out")"
            fi
        else
            skip "--help exits 0 and prints usage" "$f: not a command-line tool"
        fi
    done
    # -exec ... + rather than xargs, and one grouped path test rather than two
    # -name clauses joined by -o, whose precedence made the first clause's
    # -name apply to only half the expression.
    find . -type d -name '__pycache__' \
        \( -path './pipeline_tools/*' -o -path './ai_model_tests/*' \) \
        -exec rm -rf {} + 2>/dev/null || true

    # --------------------------------------------------------------- staleness
    printf -- '\n--- Storage layout currency ---\n'
    # Every tool carried over from the previous repository predates the run-directory
    # layout, and a tool that still walks a tree which no longer exists fails
    # silently: it finds nothing and reports nothing, which reads exactly like a
    # clean result. pipeline_docs/storage-layout.md is the specification, so a
    # reference to a path it does not define is a defect regardless of whether the
    # tool still parses and still answers --help.
    retired='outputs/ai/|logs/ai/masters|work/ai/(batch|masters|model-tests|processing)|chunks_archive'
    stale=$( { shell_files; python_files; } | grep -vE '^tests/' \
        | xargs grep -nE "$retired" 2>/dev/null | head -10)
    if [[ -z $stale ]]; then
        ok "no tool references a retired storage path" "pre-run-directory layout"
    else
        bad "no tool references a retired storage path" "$(head -2 <<<"$stale" | tr '\n' ' ')"
    fi

    # Documentation is held to the same standard: a doc describing a layout that no
    # longer exists sends someone looking in the wrong place, which is worse than
    # saying nothing.
    mapfile -t DOCS < <(git ls-files --cached --others --exclude-standard '*.md' \
        | grep -vE '^(README_Original|README_CN)\.md$' | grep -vE '^(realesrgan|scripts)/')
    stale_docs=$(printf '%s\n' "${DOCS[@]}" | xargs grep -nE "$retired" 2>/dev/null | head -10)
    if [[ -z $stale_docs ]]; then
        ok "no document describes a retired storage path" "pipeline_docs and READMEs"
    else
        bad "no document describes a retired storage path" "$(head -2 <<<"$stale_docs" | tr '\n' ' ')"
    fi

    # ------------------------------------------------------------------- summary
    printf '\nSummary\n  passed  %d\n  failed  %d\n  skipped %d\n' "$PASS" "$FAIL" "$SKIP"
    if ((FAIL)); then
        printf '\n%d failure(s):\n' "$FAIL"
        printf '  - %s\n' "${FAILURES[@]}"
        exit 1
    fi
    printf '\nAll pipeline conformance checks passed.\n'
    exit 0

}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    main "$@"
    exit $?
fi
