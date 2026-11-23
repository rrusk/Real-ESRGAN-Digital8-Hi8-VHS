#!/usr/bin/env bash
# EXAMPLE: this collection's capture names, and the bottom mask and pipeline
# profile each one implies. Sourced by prepare_tape_capture.sh and
# enhance_tape.sh, so the two read one table. Copy it and edit the case
# statement in capture_settings for your own names; the scripts that source it
# need no change.
#
# prepare_video.sh deliberately infers nothing from filenames, because what a
# capture needs depends on the signal path it came through and no probe of the
# file can reveal that. A tape played through a DV converter is dvvideo in
# every respect ffprobe can see, identical to a camcorder's own digital
# recording, and the two need opposite masks: the analog transfer carries
# head-switching noise along the bottom edge and the digital recording has
# real picture there. The pipeline's profile follows the same signal path.
#
# Only whoever captured the tape knows which it was, and a naming convention is
# how that knowledge is usually written down. Encoding it here keeps it where
# it belongs while leaving prepare_video.sh correct for captures named by
# anyone. See pipeline_docs/prepare-video.md.

# capture_settings PATH
# Prints "MASK<TAB>PROFILE<TAB>WHY" for a capture, judged by its file name
# alone, or returns 1 for a name these conventions don't cover. The mask follows
# the signal path, not the file format: anything that passed through an analog
# tape head needs one, anything recorded digitally end to end does not.
capture_settings() {
    local base=${1##*/}
    case ${base,,} in
        hi8*) printf '8\thi8dv\t%s\n' "hi8 prefix -- Hi8 analog via Digital8/FireWire" ;;
        vhs*) printf '8\tvhsdv\t%s\n' "vhs prefix -- VHS via camcorder S-Video passthrough" ;;
        dv.*) printf '0\tdv\t%s\n' "dv. prefix -- Digital8 native; no analog tape head in the path" ;;
        *)    if [[ $base =~ ^[0-9]{8}([_\&-][0-9]{8})? ]]; then
                  printf '8\thi8dv\t%s\n' \
                      "date prefix -- Hi8 analog-to-DVD transfer; the noise survived the DVD encode"
              else
                  return 1
              fi ;;
    esac
}

# capture_names_help: the conventions, as the --help of each script lists them.
capture_names_help() {
    cat <<'EOF'
Naming conventions this example knows (edit capture_names_lib.sh for your own):
  hi8*                   Hi8 analog via Digital8/FireWire      mask 8, profile hi8dv
  vhs*                   VHS via camcorder S-Video passthrough mask 8, profile vhsdv
  YYYYMMDD*              Hi8 analog-to-DVD transfer            mask 8, profile hi8dv
                         (including date ranges)
  dv.YYYYMMDD-HHMMSS.*   Digital8 native capture               mask 0, profile dv

  A name matching none of these is refused rather than guessed at: the wrong
  mask either leaves head-switching noise in the master or blacks out real
  picture, the wrong profile denoises for another source, and neither is
  visible until much later.
EOF
}

# capture_name_refused PATH: why a capture whose name capture_settings doesn't
# know is refused, on standard error.
capture_name_refused() {
    printf '[ERROR] Unrecognised capture name: %s\n' "${1##*/}" >&2
    printf '        This example only knows the naming conventions in its --help.\n' >&2
    printf '        Add a case for this name to examples/capture_names_lib.sh, or call\n' >&2
    printf '        prepare_video.sh and video_upscale_pipeline.py directly with an\n' >&2
    printf '        explicit --mask and --profile. A guessed mask either leaves\n' >&2
    printf '        head-switching noise in the master or blacks out real picture,\n' >&2
    printf '        and neither shows until much later.\n' >&2
}
