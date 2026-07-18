# T4.5 end-to-end integration rehearsal

Result: **PASS**  
Run ID: `t45-20260718T112715Z`  
Weaver report: `reports/t45-20260718T112715Z`  
Rehearsal evidence: `rehearsal/artifacts/t45-20260718T112715Z`

## Runtime declaration

The full runtime completed as declared below.

Beacon is configured through the canonical `start-beacon.sh --file --no-https`
launcher with the 659 MB file-mode source. Shaper is configured headless with
`--no-midi --no-audio --slave`; its state API, not an audio device, is the
evidence plane. Weaver is configured with its normal Stage WebSocket API and
the HarMoCAP, MIDI, and ECG drivers installed. HarMoCAP uses the real
`two_persons.jsonl` kit replay over OSC. ECG uses a deterministic synthetic
raw-ADC stream over `/ecg/raw` into the production ECG driver. MIDI has no
hardware and its invalid channels are an expected assertion.

Repository inspection found that `cymatic-control/test_ecg_stream.py` is a
receiver/terminal diagnostic, despite the supplied inventory calling it an ECG
simulator. It cannot generate `/ecg/raw`. The rehearsal therefore uses
`rehearsal/ecg_simulator.py`, whose deterministic waveform comes from the
production driver's synthetic-ECG helper, and records this inventory mismatch
instead of pretending the diagnostic sends data. `simulate_eeg.py` is not
started because EEG is outside the Weaver driver set.

The configured Beacon gate requires its real OSC hello and atomic
contract-gated state dump. Shaper's exact v1 manifest explicitly declares that
OSC hello is not currently implemented, so the configured Weaver adapter gates
the exact manifest contract ID after the manifest-declared HTTP state snapshot.
This limitation is not hidden.

## Timeline

| Elapsed (s) | Event | Detail |
|---:|---|---|
| 0.008 | `process_started` | process=beacon, mode=--file --no-https |
| 10.036 | `process_started` | process=shaper, mode=--no-midi --no-audio --slave |
| 10.557 | `process_started` | process=weaver, drivers=harmocap,midi,ecg |
| 11.562 | `sources_started` | harmocap_fixture=/home/nicolas/Projects/HarMoCAP/examples/fixtures/two_persons.jsonl, ecg_bpm=72.0 |
| 12.640 | `scene_switched` | scene=event-demo, segment=1 |
| 12.641 | `recording_started` | path=/home/nicolas/Projects/harmonic-weaver/rehearsal/artifacts/t45-20260718T112715Z/audio/beacon_master_rehearsal.wav |
| 13.709 | `state_captured` | label=t0 |
| 58.807 | `scene_switched` | scene=sparse, phase=hot_swap |
| 68.877 | `scene_switched` | scene=event-demo, segment=2 |
| 113.911 | `state_captured` | label=end |
| 114.744 | `panic_triggered` | panic_generation=1 |
| 118.200 | `panic_cleared` | ack={'command_type': 'panic.clear', 'status': 'recovered', 'panic_generation': 1, 'activation_generation': 4} |
| 121.957 | `recording_stopped` | path=/home/nicolas/Projects/harmonic-weaver/rehearsal/artifacts/t45-20260718T112715Z/audio/beacon_master_rehearsal.wav |
| 124.640 | `rehearsal_complete` | result=PASS |
| 124.649 | `process_stopped` | process=ecg-simulator, exit_code=-15, log=/home/nicolas/Projects/harmonic-weaver/rehearsal/artifacts/t45-20260718T112715Z/logs/ecg-simulator.log |
| 124.650 | `process_stopped` | process=harmocap-replay, exit_code=-15, log=/home/nicolas/Projects/harmonic-weaver/rehearsal/artifacts/t45-20260718T112715Z/logs/harmocap-replay.log |
| 124.814 | `process_stopped` | process=weaver, exit_code=-15, log=/home/nicolas/Projects/harmonic-weaver/rehearsal/artifacts/t45-20260718T112715Z/logs/weaver.log |
| 125.179 | `process_stopped` | process=shaper, exit_code=0, log=/home/nicolas/Projects/harmonic-weaver/rehearsal/artifacts/t45-20260718T112715Z/logs/shaper.log |
| 125.294 | `process_stopped` | process=beacon, exit_code=0, log=/home/nicolas/Projects/harmonic-weaver/rehearsal/artifacts/t45-20260718T112715Z/logs/beacon.log |

## Scripted assertions

| Result | Assertion | Evidence |
|---|---|---|
| PASS | `preflight.inventory` | missing=[] |
| PASS | `preflight.file_source_size` | bytes=691200078 |
| PASS | `preflight.demo_runtime` | declared cumulative demo runtime=90.000s |
| PASS | `preflight.executable.pw-jack` | resolved=/usr/bin/pw-jack |
| PASS | `preflight.executable.scsynth` | resolved=/usr/bin/scsynth |
| PASS | `preflight.executable.sclang` | resolved=/usr/bin/sclang |
| PASS | `preflight.port.8765` | loopback socket created and port available before launch |
| PASS | `preflight.port.8080` | loopback socket created and port available before launch |
| PASS | `preflight.port.57120` | loopback socket created and port available before launch |
| PASS | `preflight.port.9002` | loopback socket created and port available before launch |
| PASS | `preflight.port.9001` | loopback socket created and port available before launch |
| PASS | `preflight.port.9100` | loopback socket created and port available before launch |
| PASS | `preflight.port.5001` | loopback socket created and port available before launch |
| PASS | `contract.beacon.golden` | contract_id=eaad56d9081d01c4a63646e0055b37b7 |
| PASS | `contract.shaper.golden` | contract_id=8ad5459b6e85989542c583cc9da8d7c2 |
| PASS | `contract.stage.golden` | contract_id=cc2f83205e0dccf6d0b5d488883d73ad |
| PASS | `process.beacon.ready` | real hello and atomic state dump completed |
| PASS | `process.shaper.ready` | HTTP state API ready with audio and MIDI disabled |
| PASS | `process.weaver.ready` | health={'status': 'ok', 'contract_id': 'cc2f83205e0dccf6d0b5d488883d73ad', 'server_stream_id': '897eba0939488a34', 'stage_revision': 12, 'panic_active': False} |
| PASS | `gate.instrument.beacon-spatial` | gate_state=ready contract_id=eaad56d9081d01c4a63646e0055b37b7 |
| PASS | `gate.instrument.shaper` | gate_state=ready contract_id=8ad5459b6e85989542c583cc9da8d7c2 |
| PASS | `beacon.nature.loaded` | path=/home/nicolas/Projects/beacon-spatial/assets/nature-samples/dominicalito_frogs_pond.wav |
| PASS | `beacon.nature.gain_bounded` | gain=0.11999999731779099 |
| PASS | `shaper.five_voices.primed` | active_voices=5 |
| PASS | `scene.demo.routes_active` | active_scene=event-demo |
| PASS | `source.midi.hardware_absent_invalid` | cc_1 and modwheel are invalid as expected without MIDI hardware |
| PASS | `source.harmocap.replay_flowing` | last_frame_seq=118 |
| PASS | `source.ecg.raw_flowing` | last_frame_seq=48 |
| PASS | `scene.hot_swap.to_sparse` | active scene changed atomically and activation generation incremented |
| PASS | `scene.hot_swap.return_demo` | demo scene restored after sparse interlude |
| PASS | `timeline.demo_runtime_ge_90s` | measured cumulative demo runtime=90.000140s |
| PASS | `route.focused_subject.five_harmonics` | observed_harmonics=[1, 2, 3, 4, 5] |
| PASS | `route.ecg.rhythmic_pulses` | full-gain beat pulses=114 |
| PASS | `panic.stage.latched_safe` | outcomes={'beacon-spatial': 'ok', 'shaper': 'ok'} |
| PASS | `panic.shaper.voices_released` | active_voices=0 |
| PASS | `panic.beacon.silence_profile` | master=0.0 nature_gain=0.0 |
| PASS | `panic.routes.gated` | route writes stayed at 38952 for 3 seconds while sources continued |
| PASS | `panic.clear.routes_recovered` | route writes before=38952 after=40370 |
| PASS | `panic.clear.shaper_rearmed` | active_voices=5 |
| PASS | `panic.clear.beacon_recovered` | master=0.3807147741317749 |
| PASS | `audio.wav.created` | exists=True bytes=41959512 |
| PASS | `audio.duration` | duration=109.269333s required>=100.000140s |
| PASS | `audio.finite` | nan=0 inf=0 |
| PASS | `audio.signal_flow` | peak=0.220144823 rms=0.008154641 non_silence_ratio=0.961431310 |
| PASS | `weaver.behavior_reports.present` | report_root=/home/nicolas/Projects/harmonic-weaver/reports/t45-20260718T112715Z |
| PASS | `process.shutdown.all_managed_processes` | all managed process groups stopped after SIGTERM |

## Audio statistics

SuperCollider recorded its master output. These numbers prove a finite, non-silent signal was written; they do not claim a person heard it.

- Duration: `109.26933333333334` seconds
- Non-silence ratio: `0.9614313104012104` at absolute threshold `0.0001`
- Peak absolute sample: `0.2201448231935501`
- RMS: `0.008154640551393151`
- NaN / Inf: `0` / `0`

## State-dump diffs and panic/recovery

Machine-readable pre/post swap, panic, and recovery diffs are in `rehearsal/artifacts/t45-20260718T112715Z/state_diffs.json`. The exact Stage, Beacon, and Shaper snapshots named in the artifact
tree are the primary evidence. Panic assertions require Shaper voices inactive,
Beacon master and nature gain at zero, no route transport writes during a
three-second gated window, and route writes resuming after `panic.clear`.

## Artifact tree

- `rehearsal/artifacts/t45-20260718T112715Z/audio/beacon_master_rehearsal.wav` (41959512 bytes)
- `rehearsal/artifacts/t45-20260718T112715Z/audio_stats.json` (400 bytes)
- `rehearsal/artifacts/t45-20260718T112715Z/beacon_runtime_sync.json` (1925 bytes)
- `rehearsal/artifacts/t45-20260718T112715Z/instrument_outputs.jsonl` (11476292 bytes)
- `rehearsal/artifacts/t45-20260718T112715Z/logs/beacon-sclang.log` (6814 bytes)
- `rehearsal/artifacts/t45-20260718T112715Z/logs/beacon-scsynth.log` (268 bytes)
- `rehearsal/artifacts/t45-20260718T112715Z/logs/beacon-webui.log` (611 bytes)
- `rehearsal/artifacts/t45-20260718T112715Z/logs/beacon.log` (1048 bytes)
- `rehearsal/artifacts/t45-20260718T112715Z/logs/ecg-simulator.log` (673 bytes)
- `rehearsal/artifacts/t45-20260718T112715Z/logs/harmocap-replay.log` (82 bytes)
- `rehearsal/artifacts/t45-20260718T112715Z/logs/shaper.log` (1062 bytes)
- `rehearsal/artifacts/t45-20260718T112715Z/logs/weaver.log` (432 bytes)
- `rehearsal/artifacts/t45-20260718T112715Z/results.json` (13445 bytes)
- `rehearsal/artifacts/t45-20260718T112715Z/run_manifest.json` (1541 bytes)
- `rehearsal/artifacts/t45-20260718T112715Z/runtime_ready.json` (445 bytes)
- `rehearsal/artifacts/t45-20260718T112715Z/runtime_status.final.json` (269077 bytes)
- `rehearsal/artifacts/t45-20260718T112715Z/runtime_status.json` (269077 bytes)
- `rehearsal/artifacts/t45-20260718T112715Z/shaper_runtime_sync.json` (429 bytes)
- `rehearsal/artifacts/t45-20260718T112715Z/state_diffs.json` (4147 bytes)
- `rehearsal/artifacts/t45-20260718T112715Z/states/end.beacon.json` (2036 bytes)
- `rehearsal/artifacts/t45-20260718T112715Z/states/end.shaper.json` (1085 bytes)
- `rehearsal/artifacts/t45-20260718T112715Z/states/end.stage.json` (268555 bytes)
- `rehearsal/artifacts/t45-20260718T112715Z/states/mid_post_swap_sparse.beacon.json` (2049 bytes)
- `rehearsal/artifacts/t45-20260718T112715Z/states/mid_post_swap_sparse.shaper.json` (1027 bytes)
- `rehearsal/artifacts/t45-20260718T112715Z/states/mid_post_swap_sparse.stage.json` (264939 bytes)
- `rehearsal/artifacts/t45-20260718T112715Z/states/mid_pre_swap.beacon.json` (2034 bytes)
- `rehearsal/artifacts/t45-20260718T112715Z/states/mid_pre_swap.shaper.json` (1085 bytes)
- `rehearsal/artifacts/t45-20260718T112715Z/states/mid_pre_swap.stage.json` (268576 bytes)
- `rehearsal/artifacts/t45-20260718T112715Z/states/mid_return_demo.beacon.json` (2037 bytes)
- `rehearsal/artifacts/t45-20260718T112715Z/states/mid_return_demo.shaper.json` (1091 bytes)
- `rehearsal/artifacts/t45-20260718T112715Z/states/mid_return_demo.stage.json` (268594 bytes)
- `rehearsal/artifacts/t45-20260718T112715Z/states/panic.beacon.json` (2005 bytes)
- `rehearsal/artifacts/t45-20260718T112715Z/states/panic.shaper.json` (1016 bytes)
- `rehearsal/artifacts/t45-20260718T112715Z/states/panic.stage.json` (268547 bytes)
- `rehearsal/artifacts/t45-20260718T112715Z/states/recovery.beacon.json` (2036 bytes)
- `rehearsal/artifacts/t45-20260718T112715Z/states/recovery.shaper.json` (1088 bytes)
- `rehearsal/artifacts/t45-20260718T112715Z/states/recovery.stage.json` (268591 bytes)
- `rehearsal/artifacts/t45-20260718T112715Z/states/t0.beacon.json` (2033 bytes)
- `rehearsal/artifacts/t45-20260718T112715Z/states/t0.shaper.json` (1085 bytes)
- `rehearsal/artifacts/t45-20260718T112715Z/states/t0.stage.json` (268522 bytes)
- `rehearsal/artifacts/t45-20260718T112715Z/timeline.json` (2932 bytes)
- `reports/t45-20260718T112715Z/accepted_contract_ids.json` (754 bytes)
- `reports/t45-20260718T112715Z/behavior_events.jsonl` (38333 bytes)
- `reports/t45-20260718T112715Z/latency_summary.json` (524 bytes)
- `reports/t45-20260718T112715Z/run_config.json` (365 bytes)
- `reports/t45-20260718T112715Z/scene_snapshot.json` (6645 bytes)
- `reports/t45-20260718T112715Z/state_timestamps.jsonl` (15207872 bytes)
- `reports/t45-20260718T112715Z/summary.json` (38 bytes)

## Explicitly unverified

- Audible monitoring or subjective audio quality by a human.
- R24 live input or any other audio-interface input.
- Physical MIDI hardware, camera input, live people, ESP32/AD8232 hardware,
  EEG hardware, or EEG routing.
- Shaper real-time audio output; Shaper is configured with `--no-audio` by design.
- Shaper OSC hello/state dump, because the installed real manifest marks that
  wire handshake as planned rather than implemented.
- The supplied characterization of `cymatic-control/test_ecg_stream.py` as a
  simulator; the file is verified to be a listener, so it is inventory-only.
