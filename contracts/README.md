# Harmonic Weaver contract templates

These two machine-readable templates are the authority for the ecosystem's OSC
boundary:

- `source_frame.template.json` defines Source Frame v1 for normalized sensor,
  performance, simulator and analysis signals.
- `instrument_contract.template.json` defines Instrument Control v1 for native
  instrument capabilities and bidirectional state.

The adjacent `*.contract_id.golden` file contains the expected SHA-256 hash,
truncated to 128 bits. The hash covers canonical JSON (sorted keys, no
whitespace, UTF-8, no NaN/Infinity) and excludes only top-level `contract_id`,
`golden_hash` and `expected_contract_id` fields.

## Publishing a source

Copy the source template into the source repository as its concrete contract.
Replace `example-source` everywhere with one stable lowercase ID and declare
every emitted channel. A channel is a normalized finite float with an explicit
range, source-domain polarity, nominal rate and smoothing hint. It must never
name a destination instrument.

At runtime, publish `/src/<source_id>/hello`, then atomic frame bundles. Each
bundle begins with `/src/<source_id>/frame` and contains exactly one
`/src/<source_id>/<channel>` message for every declared channel. Every channel
message carries `(value, state, confidence)`. `held` means hold-last with
monotonically decaying confidence; `invalid` means the receiver ignores the
0.0 sentinel. Send heartbeats when frames pause so the presence lease remains
live.

## Publishing an instrument

Copy the instrument template and replace `/instrument` with the instrument's
existing namespace, such as `/beacon` or `/shaper`. Declare each native address
pattern, including placeholder bounds, OSC argument types and ranges, lag,
smoothing, and read/write support. Do not rename native controls to fit a common
voice model.

Every instrument must publish its `/hello` equivalent and choose at least one
bidirectional synchronization mechanism: an on-connect state dump, periodic
state broadcast, or both. A client synchronizes state only after receiving a
matching `contract_id` and repeats synchronization whenever `stream_id`
changes.

`voice_model_alias` is optional adapter metadata for instruments that naturally
fit `{gain, pan, phase}` per voice, such as harmonic-shaper or surge-bridge. A
contract may remove the section entirely. It must stay disabled or absent for
instruments where the abstraction is false; beacon-spatial's gain/azimuth/
distance/Q/mix controls over 13 bands of a live analog signal are the canonical
counterexample.

<!-- OPEN-BCP:1 Confirm with Anii whether a future external BCP adapter needs a
standard wire namespace and handshake for the optional voice-model alias. This
does not change the decided native instrument contract or block the MVP. -->

## Weaver gating

For a source, the weaver accepts frames only after `(source_id, stream_id,
contract_id)` matches an installed contract. It discards non-monotonic frames
within a stream and invalidates the source when its lease expires. A new
`stream_id` resets counters and cached values.

For an instrument, the weaver sends no writes until `/hello` matches its
installed `contract_id` and the declared state synchronization completes. A
stale or unknown ID stops writes and invalidates incoming state. A process
restart clears cached state and triggers a fresh synchronization.

## IDs and versioning

Never hand-edit a golden ID. Validate the concrete manifest, compute its ID with
`contract_id_from_manifest()`, and write that single lowercase 32-character
value plus a newline to its sidecar. `check_golden_sidecar()` is the release
gate. The module `src/harmonic_weaver/contract_codec.py` is stdlib-only and may
be copied unchanged into another repository.

Any manifest change changes `contract_id` and requires regenerating the
sidecar—even descriptive text—because the whole canonical manifest is hashed.
Wire-incompatible changes also increment the contract's major version; additive
backward-compatible capabilities increment minor; clarifications and compatible
metadata corrections increment patch. Deployment-only host/port configuration
belongs outside the contract and does not change its ID.

## Relationship to the HarMoCAP mold

Kept unchanged in principle: one authoritative manifest, canonical
SHA-256/128 identity plus a golden sidecar, stdlib codec, `/hello` gating,
process-scoped `stream_id`, monotonic discard, lease semantics, atomic bundles,
and explicit receiver rules.

Source Frame v1 generalizes HarMoCAP's feature validity states to every source
channel and adopts BCP's unified source-plane discipline. Its flat channel
tuples replace HarMoCAP's pose-specific person/blob/calibration layout because
MIDI, ECG, EEG and audio analysis do not share pose geometry or calibration.
The generalized gate therefore uses source identity, stream identity and
contract identity; any source-specific calibration may be declared by that
source without becoming mandatory for all drivers.

Instrument Control v1 deliberately departs from a unified BCP voice namespace:
native namespaces remain authoritative, capability manifests make them
discoverable, and mandatory state synchronization fixes the previous
unidirectional flow. The voice-model alias remains an optional adapter only.
Separate, descriptively named golden files are used because this directory
contains two authoritative templates rather than HarMoCAP's single manifest.
