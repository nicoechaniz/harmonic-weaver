#!/usr/bin/env bash
#
# start-live-stack.sh - Boot the full live beacon ecosystem stack.
#
# Brings up, in dependency order, with readiness gates between stages:
#
#   1. beacon-spatial   SuperCollider spatializer (scsynth + sclang + webui)
#                       OSC :57120, webui http://localhost:5050
#   2. harmonic-shaper  Additive synth (OSC :9002, HTTP API :8080)
#   3. harmonic-weaver  Live runtime: engine + Stage WS :8765 + source
#                       drivers (harmocap UDP :9100, ecg :5001, midi)
#   4. scene push       Upserts and activates a scene over the Stage WS
#   5. HarMoCAP         Realtime pose pipeline (webcam -> OSC :9100)
#   6. [optional] ECG simulator (synthetic stream -> OSC :5001)
#
# Every process logs under rehearsal/artifacts/<run-id>/logs/. Ctrl-C (or
# SIGTERM) stops the whole tree in reverse order. A pidfile is written so a
# detached run can be stopped later with --stop.
#
# Usage:
#   scripts/start-live-stack.sh [options]
#   scripts/start-live-stack.sh --stop <run-id|latest>
#
# Options:
#   --beacon-source <file|live>  Beacon audio source: session WAV or the
#                                R24 live input (default: file)
#   --beacon-mute                Keep the beacon engine running (OSC,
#                                state dumps, crowd routes all live) but
#                                disconnect scsynth from the audio outputs,
#                                so only the shaper is audible
#   --camera <source>            HarMoCAP camera index or video path
#                                (default: 0)
#   --harmocap-device <auto|cpu|cuda>
#                                Inference device for the pose backend.
#                                'cpu' exports CUDA_VISIBLE_DEVICES="" for
#                                the HarMoCAP process only (use it when the
#                                GPU is unstable; auto resolves per
#                                configs/model.yaml). Default: auto
#   --scene <name>               Scene to activate; resolved against
#                                rehearsal/scenes/<name>.scene.json with or
#                                without the suffix (default: event-demo)
#   --no-scene                   Do not push/activate any scene
#   --lease-ms <ms>              Source presence lease. Live default 300000
#                                so a camera stall or a person stepping out
#                                of frame does not latch the source gate as
#                                permanently absent (engine lease recovery
#                                is still an open issue; see MEMORY.md)
#   --max-runtime-s <s>          Lifetime of the weaver runtime and source
#                                listeners (default: 14400 = 4 h)
#   --record <path>              HarMoCAP session recording (.jsonl);
#                                default: artifacts/<run-id>/harmocap.jsonl
#   --no-record                  Do not record the HarMoCAP session
#   --show                       Open the HarMoCAP cv2 skeleton window
#   --shaper-no-audio            Run the shaper headless (no sounddevice)
#   --shaper-no-midi             Disable shaper MIDI inputs (default: MIDI
#                                enabled, so USB keyboards like the reface
#                                CP drive the native harmonic source)
#   --with-ecg-sim               Also launch the deterministic ECG simulator
#   --ecg-bpm <bpm>              ECG simulator rate (default: 72)
#   --no-beacon                  Skip beacon-spatial (assume already running)
#   --no-shaper                  Skip harmonic-shaper (assume already running)
#   --no-harmocap                Skip the HarMoCAP pipeline
#   --run-id <id>                Run identifier (default: live-<timestamp>)
#   --stop <run-id|latest>       Stop a detached run via its pidfile
#   -h, --help                   This help
#
# Environment overrides:
#   BEACON_DIR, SHAPER_DIR, HARMOCAP_DIR  Sibling checkout locations
#                                         (default: ~/Projects/<name>)
#   WEAVER_VENV, SHAPER_VENV, HARMOCAP_VENV
#                                         Python environments (default:
#                                         <repo>/.venv)
#
# First run bootstraps missing virtualenvs automatically:
#   weaver: uv sync --extra rehearsal --extra test
#   shaper: python3 -m venv + pip install -e .
# HarMoCAP and beacon-spatial environments must already exist.
#
set -u

WEAVER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BEACON_DIR="${BEACON_DIR:-$HOME/Projects/beacon-spatial}"
SHAPER_DIR="${SHAPER_DIR:-$HOME/Projects/harmonic-shaper}"
HARMOCAP_DIR="${HARMOCAP_DIR:-$HOME/Projects/HarMoCAP}"
WEAVER_VENV="${WEAVER_VENV:-$WEAVER_DIR/.venv}"
SHAPER_VENV="${SHAPER_VENV:-$SHAPER_DIR/.venv}"
HARMOCAP_VENV="${HARMOCAP_VENV:-$HARMOCAP_DIR/.venv}"

# ---- defaults -------------------------------------------------------------
BEACON_SOURCE="file"
BEACON_MUTE=0
CAMERA="0"
HARMOCAP_DEVICE="auto"
SCENE="event-demo"
PUSH_SCENE=1
LEASE_MS="300000"
MAX_RUNTIME_S="14400"
RECORD=""
RECORD_SET=0
SHOW=0
SHAPER_AUDIO=1
SHAPER_MIDI=1
ECG_SIM=0
ECG_BPM="72"
DO_BEACON=1
DO_SHAPER=1
DO_HARMOCAP=1
RUN_ID="live-$(date -u +%Y%m%dT%H%M%S)"
STOP_TARGET=""

usage() { sed -n '2,80p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

while [ "$#" -gt 0 ]; do
    case "$1" in
        --beacon-source) BEACON_SOURCE="${2:?--beacon-source needs file|live}"; shift ;;
        --beacon-mute)   BEACON_MUTE=1 ;;
        --camera)        CAMERA="${2:?--camera needs a source}"; shift ;;
        --harmocap-device) HARMOCAP_DEVICE="${2:?--harmocap-device needs auto|cpu|cuda}"; shift ;;
        --scene)         SCENE="${2:?--scene needs a name}"; shift ;;
        --no-scene)      PUSH_SCENE=0 ;;
        --lease-ms)      LEASE_MS="${2:?--lease-ms needs a value}"; shift ;;
        --max-runtime-s) MAX_RUNTIME_S="${2:?--max-runtime-s needs a value}"; shift ;;
        --record)        RECORD="${2:?--record needs a path}"; RECORD_SET=1; shift ;;
        --no-record)     RECORD=""; RECORD_SET=1 ;;
        --show)          SHOW=1 ;;
        --shaper-no-audio) SHAPER_AUDIO=0 ;;
        --shaper-no-midi) SHAPER_MIDI=0 ;;
        --with-ecg-sim)  ECG_SIM=1 ;;
        --ecg-bpm)       ECG_BPM="${2:?--ecg-bpm needs a value}"; shift ;;
        --no-beacon)     DO_BEACON=0 ;;
        --no-shaper)     DO_SHAPER=0 ;;
        --no-harmocap)   DO_HARMOCAP=0 ;;
        --run-id)        RUN_ID="${2:?--run-id needs a value}"; shift ;;
        --stop)          STOP_TARGET="${2:?--stop needs a run-id or 'latest'}"; shift ;;
        -h|--help)       usage; exit 0 ;;
        *) echo "[ERROR] unknown option: $1 (try --help)" >&2; exit 2 ;;
    esac
    shift
done

case "$BEACON_SOURCE" in file|live) ;; *) echo "[ERROR] --beacon-source must be file or live" >&2; exit 2 ;; esac
case "$HARMOCAP_DEVICE" in auto|cpu|cuda) ;; *) echo "[ERROR] --harmocap-device must be auto, cpu or cuda" >&2; exit 2 ;; esac

ARTIFACT_DIR="$WEAVER_DIR/rehearsal/artifacts/$RUN_ID"
LOG_DIR="$ARTIFACT_DIR/logs"
PIDFILE="$ARTIFACT_DIR/live.pids"

# ---- stop mode ------------------------------------------------------------
if [ -n "$STOP_TARGET" ]; then
    if [ "$STOP_TARGET" = "latest" ]; then
        STOP_TARGET="$(ls -1t "$WEAVER_DIR"/rehearsal/artifacts/ 2>/dev/null | grep '^live-' | head -1)"
        [ -n "$STOP_TARGET" ] || { echo "[ERROR] no live runs found" >&2; exit 1; }
    fi
    PF="$WEAVER_DIR/rehearsal/artifacts/$STOP_TARGET/live.pids"
    [ -f "$PF" ] || { echo "[ERROR] no pidfile for run $STOP_TARGET ($PF)" >&2; exit 1; }
    echo "[stop] terminating run $STOP_TARGET"
    # Pids are stored in reverse launch order already (last started first).
    while read -r pid name; do
        if kill -0 "$pid" 2>/dev/null; then
            echo "[stop] SIGTERM $name (pid $pid)"
            kill "$pid" 2>/dev/null
        fi
    done < "$PF"
    sleep 3
    while read -r pid name; do
        if kill -0 "$pid" 2>/dev/null; then
            echo "[stop] SIGKILL $name (pid $pid)"
            kill -9 "$pid" 2>/dev/null
        fi
    done < "$PF"
    exit 0
fi

# ---- helpers ----------------------------------------------------------------
log() { echo "[live-stack] $*"; }
fail() { echo "[live-stack] ERROR: $*" >&2; exit 1; }

PIDS=()
NAMES=()

register() { PIDS+=("$1"); NAMES+=("$2"); }

cleanup() {
    local i pid name
    log "shutting down (run $RUN_ID)"
    for (( i=${#PIDS[@]}-1; i>=0; i-- )); do
        pid="${PIDS[$i]}"; name="${NAMES[$i]}"
        if kill -0 "$pid" 2>/dev/null; then
            log "SIGTERM $name (pid $pid)"
            kill "$pid" 2>/dev/null
        fi
    done
    sleep 3
    for (( i=${#PIDS[@]}-1; i>=0; i-- )); do
        pid="${PIDS[$i]}"; name="${NAMES[$i]}"
        if kill -0 "$pid" 2>/dev/null; then
            log "SIGKILL $name (pid $pid)"
            kill -9 "$pid" 2>/dev/null
        fi
    done
}
trap cleanup INT TERM

wait_tcp() { # host port name timeout_s
    local deadline=$(( $(date +%s) + $4 ))
    while [ "$(date +%s)" -lt "$deadline" ]; do
        if (exec 3<>"/dev/tcp/$1/$2") 2>/dev/null; then exec 3>&- 3<&-; return 0; fi
        sleep 1
    done
    fail "$3 did not open $1:$2 within ${4}s (see $LOG_DIR)"
}

wait_http() { # url name timeout_s
    local deadline=$(( $(date +%s) + $3 ))
    while [ "$(date +%s)" -lt "$deadline" ]; do
        if curl -fsS -o /dev/null --max-time 2 "$1" 2>/dev/null; then return 0; fi
        sleep 1
    done
    fail "$2 did not answer $1 within ${3}s (see $LOG_DIR)"
}

wait_beacon_osc() { # timeout_s — real OSC hello against the live contract
    local deadline=$(( $(date +%s) + $1 ))
    while [ "$(date +%s)" -lt "$deadline" ]; do
        if PYTHONPATH="$WEAVER_DIR/src:$WEAVER_DIR" "$WEAVER_VENV/bin/python" - \
            "$BEACON_DIR/beacon_spatial.contract.json" <<'PY' >/dev/null 2>&1
import sys
from harmonic_weaver.contract_codec import contract_id_from_manifest
from rehearsal.support import beacon_snapshot, load_json
cid = contract_id_from_manifest(load_json(sys.argv[1]))
beacon_snapshot(host="127.0.0.1", port=57120, expected_contract_id=cid, timeout=3.0)
PY
        then return 0; fi
        sleep 2
    done
    fail "beacon OSC hello did not succeed within ${1}s (see $LOG_DIR/beacon.log)"
}

check_port_free() { # port proto name
    if [ "$2" = "tcp" ]; then
        (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null && { exec 3>&- 3<&-; fail "port $1/tcp already in use (needed by $3)"; }
    else
        "$WEAVER_VENV/bin/python" - "$1" <<'PY' || fail "port $1/udp already in use (needed by $3)"
import socket, sys
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    s.bind(("127.0.0.1", int(sys.argv[1])))
except OSError:
    sys.exit(1)
finally:
    s.close()
PY
    fi
    return 0
}

# ---- preflight --------------------------------------------------------------
log "run id: $RUN_ID"
mkdir -p "$LOG_DIR"

[ -d "$BEACON_DIR" ]  || fail "BEACON_DIR not found: $BEACON_DIR"
[ -d "$SHAPER_DIR" ]  || fail "SHAPER_DIR not found: $SHAPER_DIR"
[ -d "$HARMOCAP_DIR" ] || fail "HARMOCAP_DIR not found: $HARMOCAP_DIR"

# Weaver venv: bootstrap via uv if incomplete.
if ! "$WEAVER_VENV/bin/python" -c 'import fastapi, websockets, pythonosc, numpy, scipy' 2>/dev/null; then
    log "bootstrapping weaver venv (uv sync --extra rehearsal --extra test)"
    command -v uv >/dev/null || fail "uv not found; create $WEAVER_VENV manually"
    (cd "$WEAVER_DIR" && uv sync --extra rehearsal --extra test) || fail "uv sync failed"
fi

# Shaper venv: create + editable install if missing.
if [ "$DO_SHAPER" -eq 1 ]; then
    if ! "$SHAPER_VENV/bin/python" -c 'import sounddevice, fastapi, librosa' 2>/dev/null; then
        log "bootstrapping shaper venv ($SHAPER_VENV)"
        [ -x "$SHAPER_VENV/bin/python" ] || python3 -m venv "$SHAPER_VENV" || fail "venv creation failed"
        "$SHAPER_VENV/bin/pip" install -q -e "$SHAPER_DIR" || fail "shaper editable install failed"
    fi
fi

# HarMoCAP venv must pre-exist (torch build is deliberate, cu124 on this host).
if [ "$DO_HARMOCAP" -eq 1 ]; then
    [ -x "$HARMOCAP_VENV/bin/python" ] || fail "HarMoCAP venv missing: $HARMOCAP_VENV"
fi

# Port preflight.
[ "$DO_BEACON" -eq 1 ] && { check_port_free 57120 udp beacon; check_port_free 57110 udp beacon; check_port_free 5050 tcp beacon-webui; }
[ "$DO_SHAPER" -eq 1 ] && { check_port_free 9002 udp shaper; check_port_free 8080 tcp shaper-api; }
check_port_free 8765 tcp weaver-stage
check_port_free 9100 udp harmocap-driver
[ "$ECG_SIM" -eq 1 ] && check_port_free 5001 udp ecg-driver

# Scene file resolution.
SCENE_FILE=""
if [ "$PUSH_SCENE" -eq 1 ]; then
    for candidate in \
        "$WEAVER_DIR/rehearsal/scenes/$SCENE" \
        "$WEAVER_DIR/rehearsal/scenes/$SCENE.scene.json" \
        "$WEAVER_DIR/rehearsal/scenes/${SCENE//-/_}" \
        "$WEAVER_DIR/rehearsal/scenes/${SCENE//-/_}.scene.json"; do
        if [ -f "$candidate" ]; then SCENE_FILE="$candidate"; break; fi
    done
    [ -n "$SCENE_FILE" ] || fail "scene not found: $SCENE (looked in rehearsal/scenes/)"
fi

# HarMoCAP record path.
if [ "$RECORD_SET" -eq 0 ]; then
    RECORD="$ARTIFACT_DIR/harmocap-session.jsonl"
fi

# ---- 1. beacon-spatial --------------------------------------------------------
if [ "$DO_BEACON" -eq 1 ]; then
    log "starting beacon-spatial (--$BEACON_SOURCE)"
    (cd "$BEACON_DIR" && ./start-beacon.sh "--$BEACON_SOURCE" --no-https) \
        > "$LOG_DIR/beacon.log" 2>&1 &
    register $! beacon
    wait_beacon_osc 90
    log "beacon ready (OSC hello + state dump OK)"
    if [ "$BEACON_MUTE" -eq 1 ]; then
        # Keep the engine alive (OSC, state dumps, crowd routes) but silent:
        # drop every link from the scsynth JACK outputs. pw-link -d needs
        # explicit output+input pairs (a bare port fails with "No such file
        # or directory"), so enumerate the links first.
        for _ in $(seq 1 30); do
            pw-link -o 2>/dev/null | grep -q '^SuperCollider:out_1$' && break
            sleep 1
        done
        pw-link -l 2>/dev/null | awk '/^[^ |]/ {out=($1 ~ /^SuperCollider:out/) ? $1 : ""} /\|->/ && out {print out, $2}' \
            | while read -r o i; do pw-link -d "$o" "$i"; done
        sleep 2
        if pw-link -l 2>/dev/null | awk '/^SuperCollider:out/ {found=1} found && /\|->/ {print; found=0}' | grep -q .; then
            log "WARNING: beacon mute requested but scsynth is still linked"
        else
            log "beacon MUTED (scsynth outputs disconnected; engine and routes still live)"
        fi
    fi
fi

# ---- 2. harmonic-shaper -------------------------------------------------------
if [ "$DO_SHAPER" -eq 1 ]; then
    SHAPER_ARGS=()
    [ "$SHAPER_AUDIO" -eq 0 ] && SHAPER_ARGS+=(--no-audio)
    [ "$SHAPER_MIDI" -eq 0 ] && SHAPER_ARGS+=(--no-midi)
    log "starting harmonic-shaper ${SHAPER_ARGS[*]:-(audio+midi)}"
    (cd "$SHAPER_DIR" && "$SHAPER_VENV/bin/python" -m harmonic_shaper "${SHAPER_ARGS[@]}") \
        > "$LOG_DIR/shaper.log" 2>&1 &
    register $! shaper
    wait_http "http://127.0.0.1:8080/api/state" "shaper API" 60
    log "shaper ready (HTTP API on :8080)"
fi

# ---- 3. harmonic-weaver live runtime ------------------------------------------
log "starting weaver runtime (lease ${LEASE_MS}ms, max ${MAX_RUNTIME_S}s)"
(cd "$WEAVER_DIR" && \
    PYTHONPATH="$WEAVER_DIR/src:$WEAVER_DIR" "$WEAVER_VENV/bin/python" \
    rehearsal/weaver_runtime.py \
        --run-id "$RUN_ID" \
        --artifact-root "$ARTIFACT_DIR" \
        --beacon-manifest "$BEACON_DIR/beacon_spatial.contract.json" \
        --shaper-manifest "$SHAPER_DIR/contracts/shaper.contract.json" \
        --lease-ms "$LEASE_MS" \
        --max-runtime-s "$MAX_RUNTIME_S") \
    > "$LOG_DIR/weaver.log" 2>&1 &
register $! weaver
wait_tcp 127.0.0.1 8765 "weaver Stage WS" 60
log "weaver ready (Stage WS on :8765)"

# ---- 4. scene push --------------------------------------------------------------
if [ "$PUSH_SCENE" -eq 1 ]; then
    log "pushing scene: $SCENE_FILE"
    (cd "$WEAVER_DIR" && \
        PYTHONPATH="$WEAVER_DIR/src:$WEAVER_DIR" "$WEAVER_VENV/bin/python" \
        rehearsal/push_scene.py --scene "$SCENE_FILE" --switch) \
        || fail "scene push failed (see output above)"
fi

# ---- 5. HarMoCAP realtime --------------------------------------------------------
if [ "$DO_HARMOCAP" -eq 1 ]; then
    HARMOCAP_ARGS=(--source "$CAMERA" --host 127.0.0.1 --port 9100)
    [ -n "$RECORD" ] && HARMOCAP_ARGS+=(--record "$RECORD")
    [ "$SHOW" -eq 1 ] && HARMOCAP_ARGS+=(--show)
    log "starting HarMoCAP realtime (camera: $CAMERA, device: $HARMOCAP_DEVICE)"
    HARMOCAP_ENV=()
    [ "$HARMOCAP_DEVICE" = "cpu" ] && HARMOCAP_ENV=(CUDA_VISIBLE_DEVICES=)
    (cd "$HARMOCAP_DIR" && env "${HARMOCAP_ENV[@]}" "$HARMOCAP_VENV/bin/python" scripts/run_realtime.py "${HARMOCAP_ARGS[@]}") \
        > "$LOG_DIR/harmocap.log" 2>&1 &
    register $! harmocap
fi

# ---- 6. ECG simulator (optional) --------------------------------------------------
if [ "$ECG_SIM" -eq 1 ]; then
    log "starting ECG simulator (${ECG_BPM} bpm)"
    (cd "$WEAVER_DIR" && \
        PYTHONPATH="$WEAVER_DIR/src:$WEAVER_DIR" "$WEAVER_VENV/bin/python" \
        rehearsal/ecg_simulator.py --bpm "$ECG_BPM" --duration-s "$MAX_RUNTIME_S") \
        > "$LOG_DIR/ecg-sim.log" 2>&1 &
    register $! ecg-sim
fi

# ---- pidfile + status -------------------------------------------------------------
: > "$PIDFILE"
for (( i=${#PIDS[@]}-1; i>=0; i-- )); do
    echo "${PIDS[$i]} ${NAMES[$i]}" >> "$PIDFILE"
done

log "stack is UP — run $RUN_ID"
echo
echo "  components:"
for (( i=0; i<${#PIDS[@]}; i++ )); do
    printf "    %-10s pid %s\n" "${NAMES[$i]}" "${PIDS[$i]}"
done
echo
echo "  patchbay:   http://localhost:8765/"
echo "  beacon UI:  http://localhost:5050/"
echo "  shaper API: http://localhost:8080/api/state"
echo "  logs:       $LOG_DIR"
echo "  audit:      $ARTIFACT_DIR/instrument_outputs.jsonl"
[ -n "$RECORD" ] && echo "  recording:  $RECORD"
echo
echo "  Ctrl-C here stops everything; from another shell:"
echo "    $0 --stop $RUN_ID"
echo

# Wait for any child to exit; if one dies, bring the stack down.
wait -n "${PIDS[@]}" 2>/dev/null
EXIT_CODE=$?
log "a component exited (status $EXIT_CODE); stopping the stack"
cleanup
trap - INT TERM
exit "$EXIT_CODE"
