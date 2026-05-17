# Simulation Requirements Spec

Author: Bill Billion
Date: 2026-05-17
Status: Draft

## 1. Purpose And Scope

Aura is a smart-home scenario simulation system for validating how agents perceive
home events, reason about intent, coordinate with other agents, and control
devices. The primary product surface is not a generic smart-home dashboard. The
primary product surface is the complete, observable chain from a real-world event
to agent reasoning, device action, and state feedback.

The system must answer these questions for every meaningful episode:

- What happened in the simulated home?
- Which devices and rooms were relevant?
- Which agents participated?
- What did each agent infer and propose?
- How were conflicts resolved?
- Which commands were executed?
- Did the resulting world state satisfy the scenario?

This spec defines the simulation requirements that should guide the next runtime
and product iterations.

### 1.1 Goals

- Generate realistic smart-home event streams from explicit scenario definitions.
- Validate end-to-end agent flow: perception, intent recognition, task planning,
  multi-agent coordination, execution, and feedback.
- Keep every state change explainable through `SimEvent` causality.
- Make simulation runs reproducible by scenario id, seed, and initial state.
- Provide enough structure for automated evaluation, not just visual inspection.

### 1.2 Non-Goals

- Aura is not a production IoT control plane.
- Aura is not responsible for controlling real home devices.
- Aura is not a high-fidelity physics simulator.
- Aura does not need multi-tenant SaaS infrastructure for the current MVP.
- Aura does not need perfect human behavior modeling in the first iteration.

## 2. Simulation World Model

The simulation world is the single canonical state used by the runtime, agents,
WebSocket snapshots, and the frontend scene. Static registration data and mutable
world state must remain separate.

### 2.1 Core Entities

`Home`

- `scene_id`: stable scene identifier, for example `apartment_v1`.
- `floors`: logical floors in the scene.
- `rooms`: room records keyed by room id.
- `devices`: device records keyed by device id.
- `users`: simulated occupants keyed by user id.
- `environment`: global weather and time fields.
- `agents`: runtime diagnostics for each agent.

`Room`

- `id`
- `floor_id`
- `temperature`
- `humidity`
- `light_level`
- `occupancy`
- `persons`

`User`

- `id`
- `name`
- `location`
- `activity`
- `comfort_preferences`
- `presence_state`: `home | away | sleeping | unknown`

`Device`

- `id`
- `type`
- `display_name`
- `room_id`
- `floor_id`
- `capabilities`
- `state`
- `scene_bindings`

`Environment`

- `time_of_day`
- `weather`
- `outdoor_temp`
- `outdoor_humidity`
- `ambient_light`

`Agent`

- `id`
- `role`
- `status`
- `active_correlation_id`
- `last_reasoning_step`
- `last_action`
- `provider`
- `provider_configured`
- `last_latency_ms`

### 2.2 State Invariants

The simulator must preserve these invariants after every accepted event and
command:

- A user can only occupy one room or `outside` at a time.
- `room.occupancy` must be derivable from `room.persons`.
- A person listed in `room.persons` must have a matching `user.location.room`.
- A device belongs to exactly one room and one floor for a given scenario.
- A device with `online=false` cannot execute writable commands.
- Read-only capabilities can only be changed by simulation engines or sensor
  event sources, not by agents or UI commands.
- All state mutations must be attributable to a `SimEvent`.
- A mutation from an old `run_id` must not be applied to the active run.
- Reset must invalidate or cancel in-flight episodes from the previous run.
- Scenario evaluation must read state after feedback events, not before command
  execution completes.

Violating an invariant should create a structured failure event and fail the
scenario run. Silent correction is allowed only if the correction is also
recorded as an event.

### 2.3 Observable State And Ground Truth

The simulator should distinguish ground truth from what an agent can observe.
Research users need this separation to evaluate perception limits and sensor
noise.

Ground truth state:

- Actual user location and activity.
- Actual room temperature, humidity, and light level.
- Actual device state and availability.
- Scenario labels such as intended user goal and expected outcome.

Observable state:

- Sensor readings, which may be delayed, rounded, noisy, or missing.
- Device-reported state, which may be stale or inconsistent if a device is
  offline.
- Event payloads exposed to agents.
- User commands or activity labels available to the runtime.

Agents must consume observable state by default. Evaluation may compare agent
behavior against ground truth.

## 3. Device Capability Contract

Agents and UI controls must reason from capabilities, not from device type alone.
Device type is useful for grouping. Capability is the executable contract.

### 3.1 Supported Device Types

- `light`
- `hvac`
- `curtain`
- `fan`
- `camera`
- `sensor`

### 3.2 Capability Matrix

| Type | Capability | Value Contract | Writable |
| --- | --- | --- | --- |
| `light` | `power` | boolean | yes |
| `light` | `brightness` | integer `0..100` | yes |
| `light` | `color_temp` | integer Kelvin, suggested `2200..6500` | yes |
| `hvac` | `power` | boolean | yes |
| `hvac` | `target_temp` | number Celsius, suggested `16..30` | yes |
| `hvac` | `mode` | `cool | heat | fan | dry | auto` | yes |
| `hvac` | `speed` | `low | medium | high | auto` | yes |
| `curtain` | `open_percent` | integer `0..100` | yes |
| `fan` | `power` | boolean | yes |
| `fan` | `speed` | `low | medium | high` | yes |
| `fan` | `shake` | boolean | yes |
| `fan` | `timeout` | integer minutes, `0..240` | yes |
| `camera` | `view` | read-only preview metadata | no |
| `camera` | `online` | boolean | no by default |
| `sensor` | `read` | read-only sensor value | no |

### 3.3 Command Validation Requirements

Every device command must pass the same validation path regardless of source.
This includes commands from the UI, an agent, a scenario script, or a fallback
rule. The validation layer must check:

- Device exists.
- Capability exists.
- Capability is writable.
- Value type is valid.
- Value range or enum is valid.
- Command is allowed under the current scenario policy.

Invalid commands must emit structured `ERROR` messages for WebSocket clients and
structured failure events for agent episodes.

### 3.4 Device Effect Model

Device state changes must have explicit effects on the simulated world. Without
an effect model, command success only proves that a field changed; it does not
prove that the home responded realistically.

Minimum MVP effects:

`light`

- `power=false` contributes zero artificial light.
- `brightness` contributes to `room.light_level`.
- `color_temp` affects ambience and may affect user comfort in activity-specific
  metrics.

`hvac`

- `power=false` removes active cooling or heating.
- `mode=cool` moves room temperature toward `target_temp` when current
  temperature is higher.
- `mode=heat` moves room temperature toward `target_temp` when current
  temperature is lower.
- `speed` controls the rate of temperature change.

`curtain`

- `open_percent` changes natural light contribution.
- `open_percent` may affect privacy/security metrics when users are home.
- In hot weather, lower `open_percent` may reduce solar heat gain.

`fan`

- `power=true` and higher `speed` reduce perceived temperature but should not
  directly change physical room temperature unless a future model requires it.
- `shake=true` improves room-level comfort coverage.

`camera`

- `online=false` reduces security coverage.
- Camera preview changes are observation events, not comfort actions.

`sensor`

- Sensor values are generated from ground truth with an optional delay/noise
  model.
- Sensor values should not be manually writable in normal scenarios.

Every effect model must declare whether it affects physical state, perceived
comfort, security coverage, or only UI observability.

## 4. Event Taxonomy

Simulation events should be separated by semantic layer. Runtime logs are not
enough; the simulator must generate meaningful real-world events with explicit
expected device implications.

### 4.1 World Root Events

Root events represent things that happen in the home or around it. They start a
new causality chain.

Recommended namespace:

- `user.arrives_home`
- `user.leaves_home`
- `user.enters_room`
- `user.exits_room`
- `user.starts_activity`
- `user.ends_activity`
- `environment.weather_change`
- `environment.temperature_threshold`
- `environment.light_level_threshold`
- `security.presence_detected`
- `security.door_opened`
- `safety.smoke_detected`
- `device.offline`
- `device.recovered`

Current compatibility namespace:

- `user.activity_change`
- `user.command`
- `environment.state_refresh`

The current event names may remain during migration, but scenario definitions
should use the richer root event taxonomy above.

### 4.2 Derived Events

Derived events are generated by the simulation engine after root events or timer
ticks change the world.

- `sensor.reading_changed`
- `room.occupancy_changed`
- `room.temperature_changed`
- `room.light_level_changed`
- `device.state_changed`
- `device.command_failed`

### 4.3 Agent Events

Agent events describe the reasoning and execution chain.

- `reasoning.perception_snapshot`
- `reasoning.intent_recognized`
- `reasoning.task_decomposition`
- `reasoning.coordination_decision`
- `reasoning.execution_plan`
- `reasoning.fallback_rule_based`
- `action.device_control`
- `feedback.state_delta`

### 4.4 Causality Rules

- Every root event creates a new `correlation_id`.
- Every reasoning, action, and feedback event inherits the root correlation id.
- `causal_parent` must always point to the direct parent event.
- The root event must be visible before its children in the event stream.
- State deltas must be attributable to a preceding action or system event.

### 4.5 Event Generation Model

The simulator should support three event generation modes.

`scripted`

- Events come from an explicit `ScenarioSpec.timeline`.
- Used for demos, regression tests, and benchmark runs.
- The same scenario and seed must produce the same root event order.

`rule_based`

- Events are generated from state thresholds.
- Examples: temperature crosses comfort range, light level drops below target,
  sensor reports abnormal reading, user schedule advances to next activity.
- Used for continuous simulation.

`stochastic`

- Events are generated from seeded distributions.
- Examples: random device offline events, sensor drift, user changes mind,
  unexpected presence near the door.
- Used for robustness and stress tests.

Every generated event must include:

- `run_id`
- `scenario_id`
- `event_generation_mode`
- `generation_rule_id` when produced by a rule.
- `seed` or deterministic random stream identifier when stochastic.

The simulation runtime should make generated events inspectable so researchers
can determine whether a behavior was caused by scenario script, rules, or noise.

## 5. Scenario Specification

Simulation should be scenario-driven. A scenario is a reproducible contract for
initial state, scheduled events, allowed randomness, expected device effects, and
evaluation rules.

### 5.1 ScenarioSpec Fields

Required fields:

- `id`: stable scenario id.
- `name`: human-readable name.
- `description`: what real-world situation this scenario represents.
- `seed`: random seed for deterministic replay.
- `initial_state`: world overrides applied before the run starts.
- `timeline`: ordered root events or scheduled changes.
- `expected_device_effects`: acceptable target states or ranges.
- `involved_agents`: agents expected to participate.
- `success_criteria`: measurable pass/fail criteria.

Optional fields:

- `duration_seconds`
- `mode`: `observe | demo | stress`
- `allowed_fallback`
- `noise_model`
- `failure_injection`
- `metrics`

### 5.2 Example ScenarioSpec

```yaml
id: user_arrives_home_evening
name: User arrives home in the evening
seed: 1001
duration_seconds: 180
initial_state:
  time_of_day: "18:30"
  weather: cloudy
  users:
    user_01:
      location: outside
      activity: commuting
      presence_state: away
  rooms:
    living_room:
      occupancy: false
      light_level: 80
      temperature: 27.5
  devices:
    light_living_01:
      state:
        power: false
        extra:
          brightness: 0
    ac_living_01:
      state:
        power: false
        extra:
          target_temp: 24
          mode: cool
timeline:
  - at: 0
    type: user.arrives_home
    user_id: user_01
    room_id: living_room
expected_device_effects:
  - device_id: light_living_01
    within_seconds: 5
    expected:
      power: true
      extra.brightness:
        min: 50
  - device_id: ac_living_01
    within_seconds: 10
    expected:
      power: true
      extra.mode: cool
involved_agents:
  - home_orchestrator
  - lighting_agent
  - hvac_agent
success_criteria:
  require_complete_episode: true
  max_first_action_latency_ms: 5000
  max_command_failures: 0
  allow_fallback: true
```

### 5.3 Scenario Ground Truth Labels

Each scenario should include ground truth labels for evaluation. These labels are
not necessarily visible to agents.

Recommended labels:

- `user_goal`: what the simulated user wants.
- `primary_room_ids`: rooms that matter for the scenario.
- `relevant_device_ids`: devices that may reasonably be controlled.
- `forbidden_device_ids`: devices that should not be touched.
- `required_agent_roles`: agent roles that should participate.
- `acceptable_noop`: whether doing nothing is a valid outcome.
- `expected_intent`: normalized intent label for reasoning evaluation.
- `safety_constraints`: constraints that must never be violated.

Example:

```yaml
ground_truth:
  user_goal: "comfortable arrival lighting and cooling"
  primary_room_ids: ["living_room"]
  relevant_device_ids: ["light_living_01", "ac_living_01"]
  forbidden_device_ids: ["camera_bedroom_02"]
  required_agent_roles: ["orchestrator", "lighting", "hvac"]
  acceptable_noop: false
  expected_intent: "arrival_comfort"
  safety_constraints:
    - "do_not_disable_security_when_user_is_away"
```

## 6. Canonical Scenarios

The MVP should include a small set of canonical scenarios that exercise the
agent system, not just device controls.

### 6.1 User Arrives Home

Root event:

- `user.arrives_home`

Relevant state:

- User moves from `outside` to an entry or living room.
- Home changes from unoccupied to occupied.
- Time of day determines lighting strategy.
- Indoor temperature determines HVAC strategy.

Relevant devices:

- Entry/living-room lights.
- HVAC in occupied rooms.
- Curtains in public rooms.
- Cameras may switch from away/security posture to home posture.

Expected agent behavior:

- Orchestrator classifies this as comfort plus presence transition.
- LightingAgent proposes lighting changes.
- HVACAgent proposes comfort changes if temperature is out of range.
- SecurityAgent may reduce intrusion sensitivity if home mode is enabled.

### 6.2 User Leaves Home

Root event:

- `user.leaves_home`

Relevant devices:

- Whole-home lights.
- HVAC.
- Fans.
- Curtains.
- Cameras.
- Sensors.

Expected behavior:

- Turn off unnecessary lights and fans.
- Shift HVAC to energy-saving mode.
- Enable security monitoring.
- Leave sensors read-only.

### 6.3 Night Sleep

Root event:

- `user.starts_activity` with `activity=sleeping`

Relevant devices:

- Bedroom lights.
- Living-room lights.
- Curtains.
- HVAC.
- Cameras.

Expected behavior:

- Dim or turn off non-bedroom lights.
- Close bedroom curtains.
- Set comfortable sleep temperature.
- Keep security devices active without disturbing sleep.

### 6.4 Morning Wake Up

Root event:

- `user.starts_activity` with `activity=waking_up`

Relevant devices:

- Bedroom light.
- Curtains.
- HVAC.

Expected behavior:

- Gradually increase light.
- Open curtains depending on weather and daylight.
- Adjust HVAC out of sleep mode.

### 6.5 Cooking

Root event:

- `user.starts_activity` with `activity=cooking`

Relevant devices:

- Kitchen light.
- Air quality or temperature sensors.
- Fan or ventilation device if available.
- Camera is not normally relevant.

Expected behavior:

- Improve kitchen lighting.
- Monitor air quality and temperature.
- Avoid unnecessary whole-home changes.

### 6.6 Hot Weather

Root event:

- `environment.temperature_threshold`

Relevant devices:

- HVAC.
- Fans.
- Curtains.
- Temperature sensors.

Expected behavior:

- Balance comfort and energy.
- Prefer curtain/fan strategies when sufficient.
- Use HVAC when occupied room temperature exceeds comfort threshold.

### 6.7 Rainy Or Cloudy Weather

Root event:

- `environment.weather_change`

Relevant devices:

- Lights.
- Curtains.
- HVAC if humidity model is active.

Expected behavior:

- Adjust lighting for lower natural light.
- Avoid opening curtains if it worsens comfort or privacy.

### 6.8 Security Presence Detected

Root event:

- `security.presence_detected`

Relevant devices:

- Cameras.
- Entry lights.
- Optional notification channel.

Expected behavior:

- Surface camera preview.
- Turn on relevant entry light if dark.
- Do not change unrelated comfort devices.

### 6.9 Device Offline

Root event:

- `device.offline`

Relevant devices:

- The offline device.
- Alternative devices with overlapping capabilities.

Expected behavior:

- Mark device unavailable.
- Avoid issuing commands to unavailable devices.
- Use alternatives when possible.
- Emit a clear failure or fallback explanation.

### 6.10 Multi-User Conflict

Root event:

- `user.starts_activity` for multiple users with incompatible preferences.

Relevant devices:

- Shared-room lights.
- HVAC.
- Media or ambience devices if added later.

Expected behavior:

- Orchestrator detects conflicting preferences.
- Arbiter chooses a policy and explains it.
- The episode includes a coordination decision.

## 7. Event-To-Device Mapping

Every root event type must declare its default device relevance. This mapping is
not the same as mandatory execution. It defines the search space for agents.

| Root Event | Primary Devices | Secondary Devices | Default Policy |
| --- | --- | --- | --- |
| `user.arrives_home` | lights, HVAC | curtains, cameras | comfort and presence transition |
| `user.leaves_home` | lights, HVAC, fans, cameras | curtains | energy saving and security |
| `user.starts_activity:sleeping` | bedroom lights, HVAC, curtains | cameras | sleep comfort and quiet security |
| `user.starts_activity:cooking` | kitchen lights, sensors | fan | task lighting and air monitoring |
| `environment.temperature_threshold` | HVAC, fans | curtains, sensors | comfort if occupied, energy if empty |
| `environment.light_level_threshold` | lights, curtains | sensors | preserve target light level |
| `security.presence_detected` | cameras, entry lights | sensors | security first |
| `device.offline` | affected device | alternatives | fail closed and explain |

The implementation should expose this mapping as data, not hard-coded branching
inside individual agents.

## 8. Agent Architecture Requirements

The agent system should have one orchestration layer and multiple domain agents.
The orchestrator owns coordination. Domain agents own specialized policy.

### 8.1 HomeOrchestratorAgent

Responsibilities:

- Receive root events.
- Classify event domain: comfort, safety, security, energy, ambience, maintenance.
- Determine relevant rooms and device groups.
- Dispatch tasks to domain agents.
- Collect command proposals.
- Invoke arbitration.
- Produce a final execution plan.
- Explain why commands were accepted or rejected.

The orchestrator should not directly mutate world state.

### 8.2 Domain Agents

`LightingAgent`

- Controls lights and light-related curtain strategy if delegated.
- Optimizes occupancy, daylight, activity, and user preference.

`HVACAgent`

- Controls HVAC comfort strategy.
- Considers occupancy, temperature, weather, and mode.

`SecurityAgent`

- Controls camera/security posture.
- Handles presence, away mode, and device availability.

`EnergyAgent`

- Reviews proposals for energy cost.
- May veto or downgrade comfort actions when home is empty.

`SceneAgent`

- Applies named scenes such as home, away, sleep, wake, cooking.
- Must output explicit device proposals, not hidden direct mutations.

### 8.3 Orchestrator Input And Output Contract

The orchestrator converts root events into domain-agent tasks. It should have a
stable input and output contract so the runtime can test it independently from
LLM behavior.

Input: `RootEventContext`

```json
{
  "run_id": "run_001",
  "scenario_id": "user_arrives_home_evening",
  "root_event": {
    "event_type": "user.arrives_home",
    "source": "scenario_runner"
  },
  "observable_state": {
    "time_of_day": "18:30",
    "weather": "cloudy",
    "rooms": ["living_room"],
    "devices": ["light_living_01", "ac_living_01"]
  },
  "ground_truth_labels": {
    "expected_intent": "arrival_comfort"
  },
  "policy": {
    "allow_fallback": true,
    "require_human_confirmation": false
  }
}
```

Output: `TaskPlan`

```json
{
  "orchestrator_id": "home_orchestrator",
  "intent": "arrival_comfort",
  "confidence": 0.88,
  "domain_tasks": [
    {
      "agent_role": "lighting",
      "task": "prepare occupied living-room lighting",
      "relevant_device_ids": ["light_living_01"],
      "priority": "comfort"
    }
  ],
  "noop_reason": null,
  "requires_confirmation": false
}
```

The orchestrator may return `noop_reason` when no action is needed. A no-op can
be successful if the scenario marks `acceptable_noop=true`.

### 8.4 Agent Output Contract

Agents output proposals, not direct state writes.

```json
{
  "agent_id": "lighting_agent",
  "intent": "prepare living room lighting for arrival",
  "priority": "comfort",
  "confidence": 0.91,
  "commands": [
    {
      "device_id": "light_living_01",
      "property": "extra.brightness",
      "value": 70,
      "reason": "living room is occupied after sunset"
    }
  ],
  "risks": [],
  "requires_coordination": false
}
```

Agents must also be able to express:

- No action needed.
- Low confidence.
- Missing observations.
- Unsafe command rejected before proposal.
- Human confirmation required.

## 9. Coordination And Arbitration

When multiple agents propose commands, the runtime must resolve conflicts before
execution.

### 9.1 Priority Order

Default priority:

1. Safety.
2. Explicit user command.
3. Security.
4. Comfort.
5. Energy saving.
6. Ambience.
7. Maintenance.

### 9.2 Conflict Types

- Same device, same property, different values.
- Same room, competing environmental goals.
- Energy policy conflicts with comfort policy.
- Security policy conflicts with privacy/home mode.
- Device unavailable but selected by an agent.

### 9.3 Arbitration Output

Arbitration must produce:

- Approved commands.
- Rejected commands.
- Conflict explanation.
- Winning priority.
- A `reasoning.coordination_decision` event.

## 10. Execution Contract

All mutations must flow through a single execution path.

Required sequence:

1. Root event is published.
2. Orchestrator creates a task plan.
3. Domain agents create command proposals.
4. Arbiter creates an execution plan.
5. Executor validates commands.
6. Executor emits `action.device_control`.
7. StateManager applies validated changes.
8. StateManager emits `feedback.state_delta`.
9. Evaluator checks scenario criteria.

### 10.1 Command Lifecycle

Every command must move through an explicit lifecycle.

Allowed statuses:

- `proposed`: an agent or scenario requested the command.
- `approved`: arbitration accepted the command.
- `rejected`: arbitration rejected the command.
- `validated`: executor confirmed device, capability, and value contract.
- `executing`: command has been emitted to the state/device layer.
- `succeeded`: state feedback confirms the command effect.
- `failed`: validation or execution failed.
- `timed_out`: feedback did not arrive within the expected window.
- `cancelled`: run reset, episode cancelled, or user cancelled.
- `superseded`: a newer command replaced this command.

Each status transition should be represented by event data so a researcher can
reconstruct why a command did or did not affect the world.

### 10.2 Failure Semantics

Command failure must be explicit. The executor should distinguish:

- `unknown_device`
- `device_offline`
- `capability_not_supported`
- `read_only_capability`
- `invalid_value_type`
- `invalid_value_range`
- `policy_denied`
- `execution_timeout`
- `state_feedback_missing`
- `superseded_by_newer_command`

Failed commands should not mutate world state unless the failure mode itself is a
state change, such as marking a device offline.

The following components must not directly mutate device state:

- LLM providers.
- Domain agents.
- Frontend components.
- Scenario scripts.

They may only request commands through the execution contract.

## 11. Reproducibility And Run Model

Every simulation run should have a `run_id`.

Required run metadata:

- `run_id`
- `scenario_id`
- `seed`
- `started_at`
- `sim_version`
- `agent_versions`
- `llm_provider`
- `llm_model`
- `initial_state_hash`

The same scenario id, seed, code version, and provider mode should reproduce the
same non-LLM event schedule. LLM outputs may vary unless mocked or recorded.

### 11.1 LLM Determinism Modes

The runtime should support three LLM modes:

`mocked`

- Agent decisions are fixture-based.
- Required for deterministic unit and integration tests.

`recorded`

- The first live provider response is stored with the run artifact.
- Later replays reuse the recorded response.
- Required for reproducible demos and bug reports.

`live`

- The runtime calls the configured provider.
- Useful for product validation, but not sufficient for benchmark claims.

Every run artifact must record which mode was used. Benchmark results should not
mix modes without labeling them.

## 12. Evaluation Metrics

MVP metrics:

- `episode_complete`: root, reasoning, action, and feedback exist.
- `first_action_latency_ms`.
- `command_failure_count`.
- `fallback_count`.
- `conflict_count`.
- `user_intent_satisfied`.
- `device_state_match_rate`.

Product metrics:

- Comfort score.
- Estimated energy cost.
- Security coverage.
- Unnecessary command count.
- Device unavailable handling rate.
- Event chain completeness.

Each `ScenarioSpec` must define which metrics are required for pass/fail.

### 12.1 Metric Definitions

`episode_complete`

- True when the root event, required reasoning events, approved action events,
  and feedback events exist under the same `correlation_id`.

`first_action_latency_ms`

- `wall_time(first action.device_control) - wall_time(root event)`.

`command_failure_count`

- Number of commands ending in `failed`, `timed_out`, or `cancelled` when the
  scenario did not expect cancellation.

`fallback_count`

- Number of `reasoning.fallback_rule_based` events.

`conflict_count`

- Number of arbitration conflicts detected before execution.

`user_intent_satisfied`

- True when all required expected effects are met and no safety constraints are
  violated.

`device_state_match_rate`

- Matched expected device fields divided by total expected device fields.

`unnecessary_command_count`

- Number of successful commands targeting devices outside
  `relevant_device_ids`, unless explicitly allowed by the scenario.

### 12.2 Research Evaluation Requirements

Research-oriented runs should include:

- Baseline policy name, for example `rule_based`, `llm_live`, `llm_recorded`.
- Scenario suite id and version.
- Scenario split label, for example `dev`, `test`, or `benchmark`.
- Per-scenario pass/fail.
- Per-scenario event trace export.
- Aggregate metrics across the suite.
- Mean, standard deviation, and seed count for stochastic scenario suites.
- Ablation labels when disabling agents or capabilities.
- Run artifacts sufficient to reproduce or inspect failures.

Benchmark claims must report the scenario suite version, seed set, LLM mode, and
agent versions. Agents should not be tuned on the same split used for benchmark
claims.

## 13. Failure And Recovery Requirements

The simulator must model failure as part of normal operation.

Required failure scenarios:

- Device offline before a command.
- Device goes offline during execution.
- Sensor drift or stale readings.
- LLM timeout.
- LLM invalid output.
- WebSocket reconnect during an active run.
- Reset during an in-flight episode.
- User manually overrides an agent proposal.
- User changes activity shortly after an agent action.
- Safety event interrupts comfort or energy-saving behavior.

Recovery requirements:

- In-flight work from an old `run_id` cannot mutate current state.
- Failed commands must emit failure events.
- The frontend must be able to show incomplete or failed episodes.
- The evaluator must distinguish expected failures from regressions.
- The runtime should prefer fail-closed behavior for safety and security events.

## 14. Schema Versioning

Scenario specs, events, and command schemas need explicit versioning.

Required version fields:

- `scenario_schema_version`
- `event_schema_version`
- `command_schema_version`
- `device_registry_version`

Compatibility rules:

- A run must record all schema versions.
- Scenario loaders should reject unknown major versions.
- Minor versions may add optional fields.
- Event consumers must ignore unknown optional fields but fail on missing
  required fields.

## 15. MVP Acceptance Criteria

The next implementation milestone should satisfy these criteria:

- At least 8 canonical scenarios are represented as data.
- Each scenario can run with a deterministic seed.
- Every root event creates a visible episode.
- Every agent-controlled episode includes perception, intent, task
  decomposition, coordination, execution, and feedback events.
- Device commands are validated by capability schema.
- Direct UI commands and scenario commands use the same executor.
- Reset cancels or invalidates old in-flight agent episodes.
- Event ordering is stable: root before reasoning, reasoning before action,
  action before feedback.
- Command lifecycle events expose success, failure, cancellation, and superseded
  states.
- At least one scenario covers each required failure category for MVP.
- The frontend can show whether an episode is complete or missing events.
- Tests cover scenario replay, command validation, reset during in-flight agent
  work, and event ordering.

## 16. Implementation Implications

This spec implies several concrete code changes:

- Add a scenario data model and loader.
- Add a unified command executor.
- Move direct WebSocket device mutation behind the executor.
- Add value-level device command validation.
- Add run id and scenario id to event metadata or event data.
- Introduce an orchestrator role above domain agents.
- Make event ordering consistent across `main.py` and `SimulationEngine`.
- Add scenario evaluation tests.
- Add event history or replay support beyond frontend-only memory.
- Add command lifecycle events.
- Add LLM mocked and recorded modes.
- Add schema version fields for scenarios, commands, and events.

## 17. Open Questions

- Should direct manual UI commands trigger full agent reasoning, or remain a
  separate immediate-control path?
- Should `HomeOrchestratorAgent` be LLM-backed, rule-backed, or hybrid for MVP?
- Should scenarios be stored as YAML files, Python fixtures, or database records?
- How much randomness should be allowed in MVP scenarios?
- Should camera and sensor behavior remain simulated metadata, or should they
  produce first-class sensor events?
- What is the first target product mode: demo, research evaluation, or runtime
  architecture validation?
- Should benchmark suites require recorded LLM responses, or allow live provider
  variance with statistical reporting?
- Should privacy behavior for cameras be modeled as a first-class policy domain?

## 18. Researcher Review Checklist

A researcher should be able to inspect a run and answer:

- What scenario and seed produced this run?
- Which events were scripted, rule-generated, or stochastic?
- Which state fields were observable to agents and which were hidden ground
  truth?
- Which devices were relevant, forbidden, or optional?
- Which baseline or agent version was used?
- Which commands were proposed, rejected, approved, executed, or failed?
- Which metric caused a scenario to pass or fail?
- Can the run be replayed without live LLM variance?
- Can another policy be evaluated on the same scenario suite?

If the answer to any of these is unavailable, the simulator is useful for demos
but not yet sufficient for research-grade evaluation.

## 19. Recommended Next Step

The next engineering step should be to implement `ScenarioSpec` and
`CommandExecutor` before adding more UI. Those two pieces create the missing
contract between realistic event generation, agent reasoning, device control,
and evaluation.
