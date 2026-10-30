import { LitElement, html, css, nothing, type TemplateResult } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import { classMap } from 'lit/directives/class-map.js';

type IconId =
  | 'valve'
  | 'bluetooth'
  | 'sliders'
  | 'radio'
  | 'thermometer'
  | 'cpu'
  | 'radiator'
  | 'curtain'
  | 'grid'
  | 'flame'
  | 'layers'
  | 'home'
  | 'moon'
  | 'shield'
  | 'sun'
  | 'wind'
  | 'droplet'
  | 'zap'
  | 'activity'
  | 'clock'
  | 'scale'
  | 'target'
  | 'battery'
  | 'trending'
  | 'window'
  | 'door'
  | 'cloud'
  | 'snowflake'
  | 'calendar'
  | 'pause'
  | 'lock'
  | 'plug'
  | 'check'
  | 'copy'
  | 'refresh'
  | 'arrowLeft'
  | 'arrowRight'
  | 'info';

interface StepOption {
  value: string;
  label: string;
  badge?: string;
  desc: string;
  icon: IconId;
}

interface WizardStep {
  id: string;
  shortLabel: string;
  title: string;
  subtitle: string;
  multiSelect?: boolean;
  options: StepOption[];
}

interface SettingRow {
  field: string;
  value: string;
  hint: string;
  highlight?: boolean;
}

interface BenchmarkScores {
  label: string;
  comfort: number;
  actuator: number;
  energy: number;
  summary: string;
}

interface RecommendationResult {
  calType: string;
  calMode: string;
  altMode: string | null;
  mpcPreset: string | null;
  whySummary: string;
  benchmark: BenchmarkScores;
  screen1: SettingRow[];
  screen2: SettingRow[];
  checklist: TemplateResult[];
  plainTextChecklist: string;
}

const WIZARD_STEPS: WizardStep[] = [
  {
    id: 'trvSetup',
    shortLabel: 'TRV & Integration',
    title: 'Which radiator valve (TRV) and integration do you use?',
    subtitle:
      'Determines which hardware channel (Calibration type) and radio or firmware safeguards Better Thermostat enables.',
    options: [
      {
        value: 'z2m_direct',
        label: 'Direct-Valve TRV (0–100 % opening)',
        badge: 'Best Precision',
        desc: 'Sonoff TRVZB (Z2M, FW 1.3+), Eurotronic Spirit (SPZB0001 / Z-Wave), Aeotec ZWA021, or Danfoss/Popp with writable valve position.',
        icon: 'valve'
      },
      {
        value: 'shelly_blu',
        label: 'Shelly BLU TRV (via BLU Gateway)',
        badge: 'Direct Valve',
        desc: 'Valve-only actuator controlled directly in 0–100 % steps after enabling external-sensor mode in the Shelly app.',
        icon: 'bluetooth'
      },
      {
        value: 'z2m_offset',
        label: 'Zigbee2MQTT — Standard Offset TRV',
        desc: 'Sonoff TRV-ZBT (Gen 2), Bosch BTH-RM / BTH-RA, Aqara E1, Avatto ME167, Moes BHT-002, or Tuya TS0601 exposing local temperature calibration.',
        icon: 'sliders'
      },
      {
        value: 'homematic',
        label: 'HomematicIP / OpenCCU (868 MHz)',
        desc: 'HmIP-eTRV or HM-CC-RT-DN via OpenCCU / HomematicIP Local or Cloud Access Point with strict 1 % radio duty-cycle limit.',
        icon: 'radio'
      },
      {
        value: 'setpoint_only',
        label: 'ZHA, Matter, Z-Wave Standard, or Narrow-Offset TRV',
        desc: 'Any TRV via ZHA or Matter, Shelly Wi-Fi TRV (SHTRV-01), or models with narrow ±2.5–5 °C firmware offset caps (e.g. Saswell SEA801/802).',
        icon: 'thermometer'
      },
      {
        value: 'smart_internal',
        label: 'Keep Native TRV Modulation (e.g. tado°)',
        desc: 'Keep the TRV’s own internal motor control loop and use Better Thermostat purely for external room-sensor correction, windows, and presets.',
        icon: 'cpu'
      }
    ]
  },
  {
    id: 'emitterLayout',
    shortLabel: 'Radiator Layout',
    title: 'How is this room heated and mounted?',
    subtitle:
      'Covered radiators trap heat around the TRV, while multi-radiator rooms and underfloor slabs require smooth, non-oscillating outputs.',
    options: [
      {
        value: 'single_open',
        label: 'Single Open Radiator',
        desc: 'One wall radiator with free room airflow around the valve head.',
        icon: 'radiator'
      },
      {
        value: 'single_covered',
        label: 'Covered or Recessed Radiator',
        desc: 'Behind heavy curtains, a sofa, a deep windowsill, or a decorative cover where a +5 to +10 °C heat pocket forms.',
        icon: 'curtain'
      },
      {
        value: 'multi_trv',
        label: 'Multiple Radiators (2+ TRVs Grouped)',
        desc: 'Two or more radiator valves grouped inside a single Better Thermostat room entity.',
        icon: 'grid'
      },
      {
        value: 'oversized',
        label: 'Oversized / Very Fast Radiator',
        desc: 'Heats the room rapidly and easily overshoots the target temperature by 1–2 °C.',
        icon: 'flame'
      },
      {
        value: 'underfloor',
        label: 'Underfloor Heating or Heavy Cast-Iron',
        desc: 'High thermal mass with slow 1–3 hour heat-up and cool-down response.',
        icon: 'layers'
      }
    ]
  },
  {
    id: 'roomProfile',
    shortLabel: 'Room Profile',
    title: 'What kind of room and building envelope is this?',
    subtitle:
      'Insulation quality and sudden heat gains (cooking, sunshine, open doors) determine how aggressively Better Thermostat should brake or boost.',
    options: [
      {
        value: 'living_standard',
        label: 'Living Room / Home Office (Average Insulation)',
        desc: 'Everyday 15–25 m² living or working space needing steady daytime comfort.',
        icon: 'home'
      },
      {
        value: 'bedroom_quiet',
        label: 'Bedroom / Nursery',
        desc: 'Minimal valve motor noise at night and zero temperature overshoot.',
        icon: 'moon'
      },
      {
        value: 'well_insulated',
        label: 'Modern / Well-Insulated Room',
        desc: 'Triple glazing or low-energy building — holds heat for hours and overshoots if the valve closes late.',
        icon: 'shield'
      },
      {
        value: 'disturbance',
        label: 'Kitchen, Sunny South Room, or Hallway',
        desc: 'Frequent sudden heat or cold disturbances from ovens, direct afternoon sun, or hallway drafts.',
        icon: 'sun'
      },
      {
        value: 'cold_drafty',
        label: 'Cold / Poorly Insulated Room',
        desc: 'Older building envelope that loses heat fast and struggles to reach target temperature.',
        icon: 'wind'
      },
      {
        value: 'compact_bath',
        label: 'Compact Room / Bathroom (< 12 m²)',
        desc: 'Small air volume with rapid temperature response and optional humidity triggers.',
        icon: 'droplet'
      }
    ]
  },
  {
    id: 'sensorQuality',
    shortLabel: 'Room Sensor',
    title: 'How does your external room thermometer report updates?',
    subtitle:
      'In Better Thermostat 2.0’s thermal benchmark, coarse 0.5 °C sensors degrade derivative controllers (PID) while MPC v2 and TPI stay accurate.',
    options: [
      {
        value: 'fine',
        label: 'Fast & Fine (0.1–0.2 °C steps, every 1–5 min)',
        badge: 'Ideal',
        desc: 'ESPHome / BLE thermometers, Shelly H&T, Sonoff SNZB-02D, or Zigbee2MQTT sensor configured for 0.1 °C reporting.',
        icon: 'zap'
      },
      {
        value: 'standard',
        label: 'Standard Battery Zigbee / Thread Sensor',
        desc: 'Default wireless room sensor reporting around 0.2–0.3 °C changes or every 10–30 minutes.',
        icon: 'activity'
      },
      {
        value: 'coarse',
        label: 'Coarse or Slow (0.5 °C steps or ~hourly updates)',
        desc: 'Stock Aqara Zigbee sensors without custom reporting intervals, 433 MHz sensors, or wall stats with 0.5 °C steps.',
        icon: 'clock'
      }
    ]
  },
  {
    id: 'priority',
    shortLabel: 'Priority',
    title: 'What matters most to you in this room?',
    subtitle:
      'Matches the user priority weights from Better Thermostat 2.0’s 38-scenario controller benchmark.',
    options: [
      {
        value: 'balanced',
        label: 'Balanced Comfort, Battery & Energy',
        badge: 'Recommended',
        desc: 'Steady room temperature with calm valve modulation (50 % comfort · 30 % valve longevity · 20 % energy).',
        icon: 'scale'
      },
      {
        value: 'comfort_first',
        label: 'Tightest Setpoint Precision (±0.2 °C)',
        desc: 'Closest temperature tracking and fastest recovery after disturbances, accepting more motor movement.',
        icon: 'target'
      },
      {
        value: 'longevity_first',
        label: 'Maximum Battery Life & Quietest Valve',
        desc: 'Minimizes valve travel and radio commands — best for bedrooms, coin/AA batteries, and 868 MHz radios.',
        icon: 'battery'
      },
      {
        value: 'fast_heatup',
        label: 'Fastest Warm-Up After Setbacks',
        desc: 'Prioritizes reaching comfort temperature briskly after night or away setbacks.',
        icon: 'trending'
      }
    ]
  },
  {
    id: 'extras',
    shortLabel: 'Extras & v2',
    title: 'Which optional sensors or v2.0 features do you use here?',
    subtitle:
      'Toggle everything that applies to this room (or leave empty), then open your tailored Screen 1 & Screen 2 configuration.',
    multiSelect: true,
    options: [
      {
        value: 'window',
        label: 'Window Contact Sensor(s)',
        desc: 'Pauses heating automatically while airing the room, with debounce delays.',
        icon: 'window'
      },
      {
        value: 'door',
        label: 'Door Contact Sensor(s)',
        badge: 'New in v2',
        desc: 'Dedicated door input with separate open/close delays so normal walk-throughs never cut heating.',
        icon: 'door'
      },
      {
        value: 'outdoor',
        label: 'Outdoor Sensor or Weather Entity',
        desc: 'Automatic 24h-smoothed Summer Mode shutoff with 1 °C hysteresis.',
        icon: 'cloud'
      },
      {
        value: 'cooler',
        label: 'Air Conditioner / Cooling Device',
        badge: 'New in v2',
        desc: 'Combines radiator heating and AC cooling in one entity with Min/Max preset temperature pairs.',
        icon: 'snowflake'
      },
      {
        value: 'schedules',
        label: 'Weekly Schedules or Presence Setbacks',
        desc: 'Automate Comfort, Eco, Sleep, and Away temperatures via Better Thermostat presets and blueprints.',
        icon: 'calendar'
      },
      {
        value: 'secondary_heat',
        label: 'Wood Stove / Fireplace, or Boiler DHW Pauses',
        desc: 'Prevents AI self-learning drift while a stove heats the room or the boiler pauses for hot tap water.',
        icon: 'pause'
      },
      {
        value: 'child_lock',
        label: 'Physical TRV Child Lock',
        desc: 'Locks or ignores manual button presses and knob turns on the radiator valve.',
        icon: 'lock'
      },
      {
        value: 'power_cut',
        label: 'Seasonal Power Cut (Availability Guard)',
        desc: 'Suppresses unavailable warnings and repairs while a heating circuit or relay is switched off.',
        icon: 'plug'
      }
    ]
  }
];

const REQUIRED_STEP_IDS = WIZARD_STEPS.filter(s => !s.multiSelect).map(s => s.id);

@customElement('bt-configurator')
export class BtConfigurator extends LitElement {
  static styles = css`
    :host {
      display: block;
      container-type: inline-size;
      font-family: var(--__sl-font, system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif);
      --_surface: var(--sl-color-gray-6, #1c2029);
      --_surface-elevated: var(--sl-color-black, #12151c);
      --_border: var(--sl-color-gray-5, #2f3545);
      --_border-strong: var(--sl-color-gray-4, #4b5368);
      --_text-strong: var(--sl-color-white, #f8fafc);
      --_text-body: var(--sl-color-text, #cbd5e1);
      --_text-muted: var(--sl-color-gray-3, #94a3b8);
      --_accent: var(--sl-color-accent, #3b82f6);
      --_accent-high: var(--sl-color-text-accent, #93c5fd);
      --_accent-low: var(--sl-color-accent-low, rgba(59, 130, 246, 0.16));
      --_green: var(--sl-color-green, #22c55e);
      --_green-high: var(--sl-color-green-high, #86efac);
      --_green-low: var(--sl-color-green-low, rgba(34, 197, 94, 0.16));
      --_orange: var(--sl-color-orange, #f59e0b);
      --_orange-high: var(--sl-color-orange-high, #fcd34d);
      --_orange-low: var(--sl-color-orange-low, rgba(245, 158, 11, 0.16));
      --_code-bg: var(--sl-color-bg-inline-code, rgba(148, 163, 184, 0.18));
    }

    *,
    *::before,
    *::after {
      box-sizing: border-box;
    }

    button {
      font: inherit;
      color: inherit;
    }

    code {
      font-family: var(--__sl-font-mono, ui-monospace, SFMono-Regular, Menlo, Consolas, monospace);
      font-size: 0.85em;
      background: var(--_code-bg);
      padding: 0.12rem 0.38rem;
      border-radius: 0.3rem;
    }

    a {
      color: var(--_accent-high);
      text-decoration: underline;
      text-underline-offset: 0.18em;
    }

    a:hover {
      color: var(--_text-strong);
    }

    :where(button, a):focus-visible {
      outline: 3px solid var(--_accent);
      outline-offset: 3px;
    }

    .shell {
      background: var(--_surface);
      border: 1px solid var(--_border);
      border-radius: 1rem;
      padding: 1.5rem;
      margin: 1.5rem 0;
      color: var(--_text-body);
    }

    @container (min-width: 36rem) {
      .shell {
        padding: 2rem;
      }
    }

    /* Header progress + clickable step pills */
    .wizard-top {
      margin-bottom: 1.5rem;
    }

    .progress-row {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 0.75rem;
      margin-bottom: 0.55rem;
      font-size: 0.82rem;
      font-weight: 600;
      color: var(--_text-muted);
    }

    .progress-status {
      color: var(--_accent-high);
    }

    progress.wizard-progress {
      appearance: none;
      border: 0 solid;
      overflow: clip;
      display: block;
      inline-size: 100%;
      block-size: 0.45rem;
      border-radius: 999px;
      background: var(--_surface-elevated);
      margin-bottom: 1rem;
    }

    progress.wizard-progress::-webkit-progress-bar {
      background: transparent;
    }

    progress.wizard-progress::-webkit-progress-value {
      background: var(--_accent);
      border-radius: 999px;
      @media (prefers-reduced-motion: no-preference) {
        transition: inline-size 0.25s ease-out;
      }
    }

    progress.wizard-progress::-moz-progress-bar {
      background: var(--_accent);
      border-radius: 999px;
    }

    .stepper {
      display: flex;
      flex-wrap: wrap;
      gap: 0.4rem;
    }

    .step-pill {
      appearance: none;
      background: var(--_surface-elevated);
      border: 1px solid var(--_border);
      border-radius: 999px;
      padding: 0.35rem 0.72rem;
      min-block-size: 2rem;
      font-size: 0.78rem;
      font-weight: 600;
      color: var(--_text-muted);
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 0.38rem;
    }

    .step-pill:hover:not(:disabled) {
      border-color: var(--_accent);
      color: var(--_text-strong);
    }

    .step-pill.answered {
      color: var(--_text-strong);
      border-color: var(--_border-strong);
    }

    .step-pill.current {
      background: var(--_accent-low);
      border-color: var(--_accent);
      color: var(--_text-strong);
    }

    .step-pill:disabled {
      opacity: 0.45;
      cursor: not-allowed;
    }

    .step-num {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      inline-size: 1.15rem;
      block-size: 1.15rem;
      border-radius: 999px;
      font-size: 0.7rem;
      background: var(--_surface);
      color: var(--_text-muted);
    }

    .step-pill.answered .step-num {
      background: var(--_green-low);
      color: var(--_green-high);
    }

    .step-pill.current .step-num {
      background: var(--_accent);
      color: #ffffff;
    }

    /* Step question & option grid */
    .step-panel {
      @media (prefers-reduced-motion: no-preference) {
        animation: panel-enter 0.2s ease-out;
      }
    }

    @keyframes panel-enter {
      from {
        opacity: 0;
        translate: 0 6px;
      }
      to {
        opacity: 1;
        translate: 0 0;
      }
    }

    .step-heading {
      font-size: 1.35rem;
      font-weight: 700;
      line-height: 1.25;
      color: var(--_text-strong);
      margin: 0 0 0.45rem 0;
    }

    .step-heading:focus {
      outline: none;
    }

    .step-subtitle {
      font-size: 0.92rem;
      line-height: 1.5;
      color: var(--_text-muted);
      margin: 0 0 1.25rem 0;
    }

    .options-grid {
      display: grid;
      grid-template-columns: 1fr;
      gap: 0.75rem;
    }

    @container (min-width: 34rem) {
      .options-grid {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }
    }

    .option-card {
      appearance: none;
      background: var(--_surface-elevated);
      border: 1.5px solid var(--_border);
      border-radius: 0.85rem;
      padding: 1rem 1.05rem;
      min-block-size: 3.5rem;
      cursor: pointer;
      display: flex;
      flex-direction: column;
      align-items: stretch;
      text-align: left;
      gap: 0.45rem;
      @media (prefers-reduced-motion: no-preference) {
        transition: border-color 0.16s ease, background-color 0.16s ease, translate 0.16s ease;
      }
    }

    .option-card:hover {
      border-color: var(--_accent);
      @media (prefers-reduced-motion: no-preference) {
        translate: 0 -1px;
      }
    }

    .option-card:active {
      @media (prefers-reduced-motion: no-preference) {
        translate: 0 0;
      }
    }

    .option-card.selected {
      background: var(--_accent-low);
      border-color: var(--_accent);
    }

    .option-top {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 0.6rem;
    }

    .option-title-wrap {
      display: flex;
      align-items: center;
      gap: 0.6rem;
      min-width: 0;
    }

    .option-icon {
      flex-shrink: 0;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      inline-size: 2rem;
      block-size: 2rem;
      border-radius: 0.55rem;
      background: var(--_surface);
      color: var(--_accent-high);
      border: 1px solid var(--_border);
    }

    .option-card.selected .option-icon {
      background: var(--_accent);
      color: #ffffff;
      border-color: var(--_accent);
    }

    .option-title {
      font-weight: 650;
      font-size: 0.96rem;
      line-height: 1.3;
      color: var(--_text-strong);
    }

    .option-meta {
      display: flex;
      align-items: center;
      gap: 0.4rem;
      flex-shrink: 0;
    }

    .badge {
      display: inline-flex;
      align-items: center;
      padding: 0.14rem 0.48rem;
      border-radius: 999px;
      font-size: 0.7rem;
      font-weight: 700;
      letter-spacing: 0.01em;
      background: var(--_green-low);
      color: var(--_green-high);
      white-space: nowrap;
    }

    .check-indicator {
      inline-size: 1.25rem;
      block-size: 1.25rem;
      border-radius: 0.35rem;
      border: 1.5px solid var(--_border-strong);
      display: inline-flex;
      align-items: center;
      justify-content: center;
      color: transparent;
      flex-shrink: 0;
    }

    .check-indicator.radio {
      border-radius: 999px;
    }

    .option-card.selected .check-indicator {
      background: var(--_accent);
      border-color: var(--_accent);
      color: #ffffff;
    }

    .option-desc {
      font-size: 0.84rem;
      line-height: 1.45;
      color: var(--_text-muted);
    }

    .option-card.selected .option-desc {
      color: var(--_text-body);
    }

    /* Footer nav */
    .nav-bar {
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 0.75rem;
      margin-top: 1.5rem;
      padding-top: 1.15rem;
      border-top: 1px solid var(--_border);
    }

    .nav-actions {
      display: flex;
      align-items: center;
      gap: 0.6rem;
      flex-wrap: wrap;
    }

    .btn {
      appearance: none;
      padding: 0.6rem 1.05rem;
      min-block-size: 2.6rem;
      border-radius: 0.65rem;
      font-weight: 600;
      font-size: 0.9rem;
      cursor: pointer;
      border: 1px solid var(--_border-strong);
      background: var(--_surface-elevated);
      color: var(--_text-strong);
      display: inline-flex;
      align-items: center;
      gap: 0.45rem;
      white-space: nowrap;
    }

    .btn:hover:not(:disabled) {
      border-color: var(--_accent);
    }

    .btn:disabled {
      opacity: 0.42;
      cursor: not-allowed;
    }

    .btn-primary {
      background: var(--_accent);
      border-color: var(--_accent);
      color: #ffffff;
    }

    .btn-primary:hover:not(:disabled) {
      filter: brightness(1.08);
    }

    /* Results view */
    .summary-chips {
      display: flex;
      flex-wrap: wrap;
      gap: 0.45rem;
      margin-bottom: 1.25rem;
    }

    .summary-chip {
      appearance: none;
      background: var(--_surface-elevated);
      border: 1px solid var(--_border);
      border-radius: 0.55rem;
      padding: 0.32rem 0.62rem;
      min-block-size: 1.95rem;
      font-size: 0.78rem;
      color: var(--_text-body);
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 0.35rem;
    }

    .summary-chip:hover {
      border-color: var(--_accent);
      color: var(--_text-strong);
    }

    .summary-chip strong {
      color: var(--_text-strong);
    }

    .verdict-card {
      background: var(--_surface-elevated);
      border: 1.5px solid var(--_accent);
      border-radius: 0.9rem;
      padding: 1.25rem;
      margin-bottom: 1.15rem;
    }

    .verdict-grid {
      display: grid;
      grid-template-columns: 1fr;
      gap: 0.85rem;
      margin-bottom: 1rem;
    }

    @container (min-width: 34rem) {
      .verdict-grid {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }
    }

    .verdict-box {
      background: var(--_surface);
      border: 1px solid var(--_border);
      border-radius: 0.7rem;
      padding: 0.85rem 1rem;
    }

    .verdict-eyebrow {
      font-size: 0.74rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      color: var(--_text-muted);
      margin-bottom: 0.25rem;
    }

    .verdict-value {
      font-size: 1.02rem;
      font-weight: 750;
      color: var(--_text-strong);
      line-height: 1.3;
    }

    .verdict-alt {
      margin-top: 0.35rem;
      font-size: 0.8rem;
      color: var(--_text-muted);
    }

    .why-text {
      margin: 0 0 1rem 0;
      font-size: 0.92rem;
      line-height: 1.55;
      color: var(--_text-body);
    }

    /* Benchmark bars */
    .bench-box {
      border-top: 1px solid var(--_border);
      padding-top: 0.9rem;
    }

    .bench-header {
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      flex-wrap: wrap;
      gap: 0.5rem;
      margin-bottom: 0.65rem;
    }

    .bench-title {
      font-size: 0.82rem;
      font-weight: 700;
      color: var(--_text-strong);
    }

    .bench-caption {
      font-size: 0.76rem;
      color: var(--_text-muted);
    }

    .bench-bars {
      display: grid;
      grid-template-columns: 1fr;
      gap: 0.55rem;
    }

    @container (min-width: 34rem) {
      .bench-bars {
        grid-template-columns: repeat(3, minmax(0, 1fr));
        gap: 0.85rem;
      }
    }

    .bench-metric {
      display: flex;
      flex-direction: column;
      gap: 0.28rem;
    }

    .bench-metric-label {
      display: flex;
      justify-content: space-between;
      font-size: 0.78rem;
      color: var(--_text-body);
    }

    .bench-metric-label strong {
      color: var(--_text-strong);
    }

    progress.metric-bar {
      appearance: none;
      border: 0 solid;
      overflow: clip;
      inline-size: 100%;
      block-size: 0.42rem;
      border-radius: 999px;
      background: var(--_surface);
    }

    progress.metric-bar::-webkit-progress-bar {
      background: transparent;
    }

    progress.metric-bar::-webkit-progress-value {
      background: var(--_green);
      border-radius: 999px;
    }

    progress.metric-bar::-moz-progress-bar {
      background: var(--_green);
      border-radius: 999px;
    }

    /* Screen 1 & Screen 2 setting cards */
    .settings-card {
      background: var(--_surface-elevated);
      border: 1px solid var(--_border);
      border-radius: 0.9rem;
      padding: 1.25rem;
      margin-bottom: 1.15rem;
    }

    .card-heading {
      margin: 0 0 0.25rem 0;
      font-size: 1.08rem;
      font-weight: 700;
      color: var(--_text-strong);
      display: flex;
      align-items: center;
      gap: 0.5rem;
    }

    .card-subheading {
      margin: 0 0 0.9rem 0;
      font-size: 0.83rem;
      color: var(--_text-muted);
    }

    .setting-list {
      display: flex;
      flex-direction: column;
    }

    .setting-row {
      display: grid;
      grid-template-columns: 1fr;
      gap: 0.35rem;
      padding: 0.78rem 0;
      border-top: 1px solid var(--_border);
    }

    @container (min-width: 34rem) {
      .setting-row {
        grid-template-columns: minmax(0, 1.35fr) auto;
        align-items: start;
        gap: 1rem;
      }
    }

    .setting-field {
      font-size: 0.9rem;
      font-weight: 600;
      color: var(--_text-strong);
      line-height: 1.35;
    }

    .setting-hint {
      font-size: 0.8rem;
      color: var(--_text-muted);
      line-height: 1.45;
      margin-top: 0.18rem;
    }

    .setting-pill {
      justify-self: start;
      display: inline-flex;
      align-items: center;
      padding: 0.26rem 0.65rem;
      border-radius: 0.45rem;
      font-size: 0.84rem;
      font-weight: 700;
      background: var(--_surface);
      border: 1px solid var(--_border-strong);
      color: var(--_text-strong);
    }

    @container (min-width: 34rem) {
      .setting-pill {
        justify-self: end;
        text-align: right;
      }
    }

    .setting-pill.highlight {
      background: var(--_accent-low);
      border-color: var(--_accent);
    }

    .checklist {
      margin: 0.5rem 0 0 0;
      padding-left: 1.2rem;
      display: flex;
      flex-direction: column;
      gap: 0.6rem;
    }

    .checklist li {
      font-size: 0.88rem;
      line-height: 1.5;
      color: var(--_text-body);
    }

    .checklist li strong {
      color: var(--_text-strong);
    }

    .copy-status {
      font-size: 0.84rem;
      font-weight: 600;
      color: var(--_green-high);
      min-inline-size: 1rem;
    }

    @media (pointer: coarse) {
      .step-pill {
        min-block-size: 2.4rem;
        padding: 0.45rem 0.85rem;
      }
      .btn {
        min-block-size: 2.85rem;
      }
    }
  `;

  @state()
  private currentStep = 0;

  @state()
  private answers: Record<string, string> = {};

  @state()
  private extras: string[] = [];

  @state()
  private copyFeedback = '';

  private get allRequiredAnswered(): boolean {
    return REQUIRED_STEP_IDS.every(id => Boolean(this.answers[id]));
  }

  private get answeredStepCount(): number {
    const singleCount = REQUIRED_STEP_IDS.filter(id => Boolean(this.answers[id])).length;
    const onOrPastExtras = this.currentStep >= WIZARD_STEPS.length || (this.allRequiredAnswered && this.currentStep === WIZARD_STEPS.length - 1);
    return onOrPastExtras ? WIZARD_STEPS.length : singleCount;
  }

  private get isResultsView(): boolean {
    return this.currentStep >= WIZARD_STEPS.length;
  }

  private focusHeadingOnNextFrame() {
    if (typeof window === 'undefined') return;
    requestAnimationFrame(() => {
      const heading = this.shadowRoot?.querySelector<HTMLElement>('.step-heading');
      heading?.focus({ preventScroll: true });
    });
  }

  private goToStep(stepIndex: number) {
    this.currentStep = stepIndex;
    this.copyFeedback = '';
    this.focusHeadingOnNextFrame();
  }

  private selectSingleOption(stepId: string, value: string) {
    this.answers = { ...this.answers, [stepId]: value };
    const nextIndex = this.currentStep + 1;
    this.goToStep(nextIndex);
  }

  private toggleExtra(value: string) {
    if (this.extras.includes(value)) {
      this.extras = this.extras.filter(item => item !== value);
    } else {
      this.extras = [...this.extras, value];
    }
  }

  private restart() {
    this.answers = {};
    this.extras = [];
    this.goToStep(0);
  }

  private async copyChecklist(text: string) {
    if (typeof navigator === 'undefined' || !navigator.clipboard) return;
    try {
      await navigator.clipboard.writeText(text);
      this.copyFeedback = 'Checklist copied to clipboard.';
      setTimeout(() => {
        this.copyFeedback = '';
      }, 3500);
    } catch {
      this.copyFeedback = 'Could not access clipboard.';
    }
  }

  private shortChoiceLabel(stepId: string): string {
    const step = WIZARD_STEPS.find(s => s.id === stepId);
    if (!step) return '';
    if (step.multiSelect) {
      return this.extras.length === 0 ? 'None' : `${this.extras.length} selected`;
    }
    const chosen = step.options.find(o => o.value === this.answers[stepId]);
    return chosen ? chosen.label.split(' (')[0] : '';
  }

  private computeRecommendation(): RecommendationResult {
    const { trvSetup, emitterLayout, roomProfile, sensorQuality, priority } = this.answers;
    const hasExtra = (key: string) => this.extras.includes(key);

    const isDirectValve = trvSetup === 'z2m_direct' || trvSetup === 'shelly_blu';
    const isCovered = emitterLayout === 'single_covered';
    const isMultiTrv = emitterLayout === 'multi_trv';
    const isOversized = emitterLayout === 'oversized';
    const isUnderfloor = emitterLayout === 'underfloor';
    const isHomematic = trvSetup === 'homematic';
    const isSmartInternal = trvSetup === 'smart_internal';
    const isCoarseSensor = sensorQuality === 'coarse';

    // 1. Calibration type (Hardware channel — exact strings.json option)
    let calType = 'Offset Based';
    let calTypeHint =
      'Shifts the TRV’s internal thermometer bias while keeping your real room setpoint visible on the TRV display.';

    if (isDirectValve) {
      calType = 'Direct Valve Based';
      calTypeHint =
        'Positions the physical valve stem directly from 0–100 %, bypassing the TRV’s internal thermostat loop.';
    } else if (trvSetup === 'setpoint_only' || isSmartInternal) {
      calType = 'Target Temperature Based';
      calTypeHint =
        'Drives heating via adjusted target setpoints so missing or narrow firmware offset caps (±2.5–5 °C) never stall heating.';
    } else if (isCovered) {
      calType = 'Target Temperature Based';
      calTypeHint =
        'Switched from Offset Based because covered radiators trap +5 to +10 °C of heat and hit firmware offset limits.';
    } else if (isHomematic) {
      calType = 'Offset Based';
      calTypeHint =
        'Requires un-ignoring TEMPERATURE_OFFSET in OpenCCU / HomematicIP Local; otherwise pick Target Temperature Based.';
    }

    // 2. Calibration mode (Control brain — exact strings.json option)
    let calMode = '(AI) Time Based (Default)';
    let altMode: string | null = 'TPI Controller';
    let mpcPreset: string | null = null;
    let whySummary = '';
    let benchmark: BenchmarkScores = {
      label: '(AI) Time Based — Adaptive Heating-Power Learning',
      comfort: 74,
      actuator: 65,
      energy: 98,
      summary: 'Event-driven (no 5-minute timer polling), low radio traffic, learns room heat-up rate over 2–3 cycles.'
    };

    const pickMpcPreset = (): string => {
      if (roomProfile === 'compact_bath' || isOversized) {
        return 'Small room (~10 m², fast response)';
      }
      if (isUnderfloor || isMultiTrv) {
        return 'Large room (~40 m², slow envelope)';
      }
      if (priority === 'fast_heatup') {
        return 'Medium room (~20 m², standard)';
      }
      return 'Auto (use learned heat-loss rate)';
    };

    const setMpcV2 = (reason: string, alternative: string | null = 'TPI Controller') => {
      calMode = '(AI) MPC v2 (QP + Kalman, experimental)';
      altMode = alternative;
      mpcPreset = pickMpcPreset();
      whySummary = reason;
      benchmark = {
        label: isCoarseSensor
          ? 'MPC v2 on 0.5 °C Coarse Sensor (38-Scenario Benchmark: 0.90 Overall Learned)'
          : 'MPC v2 Direct-Valve Benchmark (38 Scenarios: 0.85 Overall vs Oracle 1.00)',
        comfort: 98,
        actuator: 56,
        energy: 99,
        summary:
          'Highest setpoint comfort (0.975) across 38 thermal scenarios; Kalman filter learns sensor step size automatically.'
      };
    };

    const setTpi = (reason: string, alternative: string | null = '(AI) Time Based (Default)') => {
      calMode = 'TPI Controller';
      altMode = alternative;
      mpcPreset = null;
      whySummary = reason;
      benchmark = {
        label: 'TPI Controller Benchmark (38 Scenarios: 0.77 Overall · #1 Valve Longevity)',
        comfort: isCoarseSensor ? 81 : 78,
        actuator: 60,
        energy: 100,
        summary:
          'Top actuator-longevity score (0.603) and zero overheating penalty (1.000), with no learning wait on Day 1.'
      };
    };

    const setPid = (reason: string, alternative: string | null = 'TPI Controller') => {
      calMode = 'PID Controller';
      altMode = alternative;
      mpcPreset = null;
      whySummary = reason;
      benchmark = {
        label: 'PID Controller Benchmark (38 Scenarios: 0.76 Overall · 0.87 Comfort)',
        comfort: 87,
        actuator: 43,
        energy: 99,
        summary:
          'Strong setpoint tracking (0.866) and derivative disturbance braking on fast 0.1 °C sensors; higher motor travel (0.429).'
      };
    };

    if (isSmartInternal) {
      calMode = 'External Sensor Offset Only';
      altMode = 'TPI Controller';
      whySummary =
        'Applies a pure 1:1 thermometer bias correction from your external room sensor while leaving your TRV’s own internal motor control algorithm in charge—plus Better Thermostat’s window/door, Summer Mode, and preset features.';
      benchmark = {
        label: 'External Sensor Offset Only — Native TRV Motor Loop',
        comfort: 75,
        actuator: 72,
        energy: 97,
        summary: 'Zero extra control loop on top; modulation smoothness depends on your TRV’s built-in firmware.'
      };
    } else if (isUnderfloor) {
      if (isDirectValve && priority === 'comfort_first') {
        setMpcV2(
          'Your actuator supports direct 0–100 % valve positioning on a high-inertia slab. MPC v2 with the Large room envelope predicts multi-hour thermal momentum so it shuts down early before the floor slab overshoots.',
          '(AI) Time Based (Default)'
        );
      } else {
        calMode = '(AI) Time Based (Default)';
        altMode = 'TPI Controller';
        whySummary =
          'Underfloor slabs and cast-iron radiators carry 1–3 hours of thermal inertia. Pairing (AI) Time Based with a wider 0.5 °C tolerance prevents short-cycling while adapting automatically to the slow warm-up slope.';
      }
    } else if (isMultiTrv) {
      if (isDirectValve && priority !== 'longevity_first' && roomProfile !== 'bedroom_quiet') {
        setMpcV2(
          'With 2+ direct-valve radiators sharing one room, MPC v2 produces a smooth model-predictive valve demand that distributes cleanly across grouped TRVs without two loops fighting each other.',
          'TPI Controller'
        );
      } else {
        setTpi(
          'In Better Thermostat 2.0’s multi-TRV benchmark, smooth proportional outputs (TPI) distribute cleanly across grouped radiators, whereas aggressive or derivative modes can amplify differences between valves.',
          isDirectValve ? '(AI) MPC v2 (QP + Kalman, experimental)' : '(AI) Time Based (Default)'
        );
      }
    } else if (isDirectValve) {
      if (priority === 'longevity_first' || roomProfile === 'bedroom_quiet') {
        setTpi(
          'Even on a Direct Valve TRV, you prioritized quiet motor behavior and maximum battery longevity. TPI Controller leads the benchmark in actuator longevity (0.603) with calm proportional steps and zero overshoot.',
          '(AI) MPC v2 (QP + Kalman, experimental)'
        );
      } else if (roomProfile === 'disturbance' && sensorQuality === 'fine' && priority === 'comfort_first') {
        setMpcV2(
          'Your TRV exposes direct 0–100 % valve control and your room sensor reports fine 0.1 °C steps. MPC v2’s Kalman disturbance observer absorbs cooking, solar, or hallway heat spikes while leading the benchmark in comfort (0.975).',
          'PID Controller'
        );
      } else {
        setMpcV2(
          isCoarseSensor
            ? 'Built for Direct Valve TRVs: MPC v2 automatically learns your coarse 0.5 °C room sensor’s step threshold and weighs valve moves accordingly—scoring 0.901 once learned without derivative spikes.'
            : 'Built specifically for Direct Valve TRVs: MPC v2 models radiator and room thermal momentum ahead of time so it brakes softly before overshooting. Prefer a non-experimental Day-1 controller? Choose TPI Controller.',
          'TPI Controller'
        );
      }
    } else if (isHomematic) {
      if (
        roomProfile === 'bedroom_quiet' ||
        roomProfile === 'well_insulated' ||
        roomProfile === 'disturbance' ||
        isOversized ||
        priority === 'longevity_first' ||
        isCoarseSensor
      ) {
        setTpi(
          'HomematicIP’s 868 MHz 1 % duty cycle demands calm, low-frequency commands. TPI Controller prevents overshoot without motor hunting while Better Thermostat’s HomematicIP rate-limiter spaces background updates to 10 minutes.',
          '(AI) Time Based (Default)'
        );
      } else {
        calMode = '(AI) Time Based (Default)';
        altMode = 'TPI Controller';
        whySummary =
          'Ideal for HomematicIP living spaces: (AI) Time Based only recalculates on real temperature changes (no 5-minute timer polling), keeping 868 MHz radio traffic well within the 1 % duty-cycle limit.';
      }
    } else if (
      (roomProfile === 'cold_drafty' || priority === 'fast_heatup') &&
      !isOversized &&
      roomProfile !== 'bedroom_quiet' &&
      roomProfile !== 'well_insulated' &&
      priority !== 'longevity_first'
    ) {
      calMode = 'Aggressive';
      altMode = '(AI) Time Based (Default)';
      whySummary =
        'Poorly insulated rooms or brisk morning warm-ups can stall near setpoint on gentle controllers. Aggressive skips the initial tolerance delay and adds a +2.5 °C heating push while active, paired with Overheating protection to trim residual heat.';
      benchmark = {
        label: 'Aggressive Mode — Immediate Start + 2.5 °C Heating Boost',
        comfort: 76,
        actuator: 52,
        energy: 95,
        summary: 'Eliminates sluggish warm-up below target; switch to (AI) Time Based or TPI if the room overshoots.'
      };
    } else if (
      roomProfile === 'disturbance' &&
      sensorQuality === 'fine' &&
      priority === 'comfort_first'
    ) {
      setPid(
        'Your room sees sudden external heat or cold spikes (cooking, sun, or door drafts) and has a fast 0.1–0.2 °C thermometer. PID’s derivative brake reacts rapidly to sudden slopes and auto-tunes Kp, Ki, and Kd during its first full heating cycle.',
        'TPI Controller'
      );
    } else if (
      roomProfile === 'bedroom_quiet' ||
      roomProfile === 'well_insulated' ||
      roomProfile === 'disturbance' ||
      isOversized ||
      priority === 'longevity_first' ||
      isCoarseSensor
    ) {
      setTpi(
        isCoarseSensor
          ? 'Because your room sensor reports in coarse 0.5 °C steps, PID’s derivative term would spike on step jumps. TPI Controller scores 0.807 on 0.5 °C sensors and throttles smoothly near target without valve chatter.'
          : 'TPI Controller throttles heating proportionally inside a ±1.0 °C window around your setpoint—preventing overshoot in well-insulated, bedroom, or disturbance-prone rooms while keeping battery wear minimal.',
        '(AI) Time Based (Default)'
      );
    } else {
      calMode = '(AI) Time Based (Default)';
      altMode = 'TPI Controller';
      whySummary =
        'Measures your room’s actual warming and cooling rate over 2–3 heating cycles and scales heating demand automatically—giving reliable, hands-off everyday comfort with low battery wear.';
    }

    // 3. Tolerance (Screen 1)
    let tolerance = '0.3';
    let toleranceHint =
      'Replaces the 0.0 default so heating waits until 0.3 °C below target before restarting—stopping valve chatter and battery drain.';
    if (isUnderfloor) {
      tolerance = '0.5';
      toleranceHint = 'Wider 0.5 °C band avoids fighting multi-hour slab or cast-iron thermal inertia.';
    } else if (isDirectValve && priority === 'comfort_first') {
      tolerance = '0.2';
      toleranceHint = 'Tighter 0.2 °C hysteresis band suited to fast sensors and direct 0–100 % valve modulation.';
    } else if (priority === 'longevity_first' || isHomematic) {
      tolerance = '0.3';
      toleranceHint =
        '0.3 °C (up to 0.4 °C in very noisy rooms) cuts unnecessary micro-cycles and preserves battery & radio budget.';
    } else if (isSmartInternal) {
      tolerance = '0.3';
      toleranceHint =
        'Inactive in External Sensor Offset Only, but safe to store at 0.3 °C in case you switch controllers later.';
    }

    // 4. Screen 1 settings (exact verbatim strings.json field labels)
    const screen1: SettingRow[] = [];

    if (hasExtra('cooler')) {
      screen1.push({
        field: 'The cooling device (optional)',
        value: 'Select your AC climate.* entity',
        hint: 'Enables Heat/Cool mode; each enabled preset splits automatically into Min (heating) and Max (cooling) sliders.',
        highlight: true
      });
    }

    screen1.push({
      field: 'Tolerance, to prevent the thermostat from turning on and off too often',
      value: `${tolerance} (${tolerance} °C)`,
      hint: toleranceHint,
      highlight: true
    });

    if (hasExtra('window')) {
      screen1.push(
        {
          field: 'Delay before the thermostat should turn off when the window is opened',
          value: '00:02:00 (2 min)',
          hint: 'Ignores brief openings (letting a pet out or grabbing mail) before closing the valve.'
        },
        {
          field: 'Delay before the thermostat should turn on when the window is closed',
          value: '00:03:00 (3 min)',
          hint: 'Lets cold air mix with warm walls first so self-learning modes do not misread a fake cold spike.'
        }
      );
    }

    if (hasExtra('door')) {
      screen1.push(
        {
          field: 'Delay before the thermostat should turn off when the door is opened',
          value: '00:02:30 (2 min 30 s)',
          hint: 'Prevents everyday room walk-throughs from pausing the radiator; triggers only when propped open.'
        },
        {
          field: 'Delay before the thermostat should turn on when the door is closed',
          value: '00:01:30 (1 min 30 s)',
          hint: 'Resumes heating smoothly once hallway or balcony air settles.'
        }
      );
    }

    if (hasExtra('outdoor')) {
      const threshold =
        roomProfile === 'well_insulated'
          ? '15 (15 °C)'
          : roomProfile === 'cold_drafty' || roomProfile === 'compact_bath'
            ? '17 (17 °C)'
            : '16 (16 °C)';
      const resumeTemp =
        roomProfile === 'well_insulated' ? '14 °C' : roomProfile === 'cold_drafty' || roomProfile === 'compact_bath' ? '16 °C' : '15 °C';
      screen1.push({
        field: 'The outdoor temperature when the thermostat should turn off',
        value: threshold,
        hint: `Lowered from the 20 °C default. Uses a ~24h outdoor average and resumes heating automatically at ${resumeTemp} (1 °C hysteresis).`,
        highlight: true
      });
    }

    if (hasExtra('power_cut')) {
      screen1.push(
        {
          field: 'Availability guard',
          value: 'Select circuit power switch / binary_sensor',
          hint: 'Suppresses unavailable warnings and repair issues while the heating circuit is intentionally powered off.'
        },
        {
          field: 'Grace period after the availability guard turns on',
          value: '00:02:00 (2 min)',
          hint: 'Gives TRVs time to boot and rejoin Zigbee/Wi-Fi after power returns (default is 00:01:00).'
        }
      );
    }

    const recommendedPresets = hasExtra('schedules')
      ? roomProfile === 'compact_bath'
        ? 'Eco, Comfort, Sleep, Away, Boost'
        : 'Eco, Comfort, Sleep, Away'
      : 'Eco, Comfort';

    screen1.push(
      {
        field: 'Enabled Presets',
        value: recommendedPresets,
        hint: hasExtra('cooler')
          ? 'Exposes <Preset> Min and <Preset> Max number entities on the device page (keep ≥ 2.0 °C deadband between Min & Max).'
          : 'Creates adjustable preset temperature sliders on the Better Thermostat device page for schedules & automations.'
      },
      {
        field: 'Target temperature step',
        value: isHomematic ? '0.5 °C (or Auto)' : 'Auto',
        hint: isHomematic
          ? '0.5 °C steps avoid accidental 0.1 °C UI nudges burning 868 MHz duty-cycle budget.'
          : 'Inherits the natural step size reported by your thermostats.'
      }
    );

    // 5. Screen 2 settings (exact verbatim strings.json field labels)
    const screen2: SettingRow[] = [
      {
        field: 'Calibration type',
        value: calType,
        hint: calTypeHint,
        highlight: true
      },
      {
        field: 'Calibration mode',
        value: calMode,
        hint: altMode ? `Alternative if you want to compare: ${altMode}.` : 'Selected control algorithm.',
        highlight: true
      }
    ];

    if (mpcPreset) {
      screen2.push({
        field: 'MPC v2 room size',
        value: mpcPreset,
        hint: 'Starting thermal room model for MPC v2 (only active when MPC v2 is selected).',
        highlight: true
      });
    }

    const overheatingActive = calMode === '(AI) Time Based (Default)' || calMode === 'Aggressive';
    screen2.push({
      field: 'Overheating protection?',
      value: 'On',
      hint: overheatingActive
        ? 'Actively trims setpoints when residual radiator heat threatens to push the room past target.'
        : 'Safe to keep On (MPC v2, TPI, and PID already brake valve demand natively before target).'
    });

    if (isHomematic) {
      screen2.push({
        field:
          'If you use HomematicIP, you should enable this to slow down the requests to prevent the duty cycle',
        value: 'On',
        hint: 'Mandatory on 868 MHz HomematicIP / OpenCCU: spaces non-urgent background writes to 10-minute intervals.',
        highlight: true
      });
    }

    if (hasExtra('child_lock')) {
      screen2.push({
        field: 'Ignore all inputs on the TRV like a child lock',
        value: 'On',
        hint: 'Locks the TRV’s physical buttons where supported and ignores manual knob changes.'
      });
    }

    const recommendMaintenance = hasExtra('outdoor') && !isHomematic && !isSmartInternal;
    screen2.push({
      field: 'If your thermostat has no own maintenance mode, you can use this one',
      value: recommendMaintenance ? 'On' : 'Off',
      hint: recommendMaintenance
        ? 'Exercises the valve pin periodically during long Summer Mode shutdowns so lime scale cannot seize it.'
        : isHomematic || isSmartInternal
          ? 'Keep Off: HomematicIP and tado° already run their own weekly hardware decalcification cycle.'
          : 'Enable only if your TRV firmware lacks a built-in weekly anti-seize valve exercise.'
    });

    // 6. Tailored post-setup checklist & links
    const checklist: TemplateResult[] = [];
    const textBullets: string[] = [];

    if (trvSetup === 'shelly_blu') {
      checklist.push(
        html`<strong>Shelly BLU TRV pre-flight (mandatory):</strong> In the Shelly app, enable <em>External temperature / Disable internal control</em> and run the physical <strong>Valve Calibration</strong> routine before adding it—otherwise the valve rejects commands with <code>invalid_valve_calibration</code>. (<a href="/working-devices/compatibility/">Compatibility guide</a>)`
      );
      textBullets.push(
        'Shelly BLU TRV: Enable External temperature / Disable internal control and run physical Valve Calibration in the Shelly app first.'
      );
    }

    if (trvSetup === 'z2m_direct') {
      checklist.push(
        html`<strong>Sonoff TRVZB firmware check:</strong> If using a Sonoff <code>TRVZB</code> (Gen 1), ensure firmware is updated to <strong>v1.3.0+</strong> in Zigbee2MQTT so direct <code>0–100 %</code> valve opening modulates linearly without wide deadbands. (<a href="/working-devices/compatibility/">Compatibility guide</a>)`
      );
      textBullets.push('Direct-Valve TRV: Ensure Sonoff TRVZB is on firmware v1.3.0+ in Zigbee2MQTT.');
    }

    if (isHomematic) {
      checklist.push(
        html`<strong>OpenCCU offset channel:</strong> To use <code>Offset Based</code>, un-ignore the hidden <code>select.&lt;trv&gt;_temperature_offset</code> entity in Home Assistant. If using the HomematicIP Cloud Access Point without an offset entity, pick <code>Target Temperature Based</code> instead.`
      );
      textBullets.push(
        'HomematicIP / OpenCCU: Un-ignore select.<trv>_temperature_offset for Offset Based, or use Target Temperature Based.'
      );
    }

    if (trvSetup === 'z2m_offset') {
      checklist.push(
        html`<strong>Tuya / Moes mode quirk check:</strong> If your TRV’s heating mode in Zigbee2MQTT is named <code>auto</code> instead of <code>heat</code> (common on <code>BHT-002</code> / some <code>TS0601</code> units), turn <strong>On</strong> <em>"If 'auto' means 'heat' for your TRV and you want to swap it"</em> on Screen 2.`
      );
      textBullets.push(
        "Zigbee2MQTT Offset TRV: Enable \"If 'auto' means 'heat'...\" on Screen 2 only if your TRV uses 'auto' as its normal heating mode."
      );
    }

    if (calType === 'Target Temperature Based') {
      checklist.push(
        html`<strong>Normal TRV display behavior:</strong> Seeing <code>26 °C</code> or <code>30 °C</code> temporarily on the radiator’s physical LCD while heating is completely expected in <code>Target Temperature Based</code>—Better Thermostat lowers it automatically as your room reaches target.`
      );
      textBullets.push(
        'Target Temperature Based: Temporary high numbers (26–30 °C) on the TRV LCD during active heating are normal.'
      );
    }

    if (isOversized) {
      checklist.push(
        html`<strong>Cap oversized radiator power:</strong> Open the Better Thermostat device page in Home Assistant and lower <code>Valve Max Opening</code> to <strong>60–75 %</strong> for virtual hydraulic balancing. (<a href="/deep-explanations/hydraulic-balance/">Hydraulic balancing guide</a>)`
      );
      textBullets.push('Device Page: Lower Valve Max Opening to 60–75 % to tame the oversized radiator.');
    }

    if (isMultiTrv) {
      checklist.push(
        html`<strong>Multi-TRV room tip:</strong> On Screen 1, select all radiators of this room in <code>The real thermostat</code>. Screen 2 will appear once per TRV—apply the same <code>Calibration mode</code> to each valve, and cap any oversized radiator via its per-TRV <code>Valve Max Opening</code> slider.`
      );
      textBullets.push(
        'Multi-TRV Room: Select all TRVs in "The real thermostat" on Screen 1; Screen 2 repeats once per TRV.'
      );
    }

    if (isUnderfloor) {
      checklist.push(
        html`<strong>Keep setbacks gentle on slabs:</strong> Limit night or Eco setbacks to <strong>1.0–1.5 °C</strong> below Comfort so reheating the high-mass slab does not overshoot in the morning.`
      );
      textBullets.push('Underfloor / Cast-Iron: Keep night setbacks within 1.0–1.5 °C of daytime Comfort.');
    }

    if (isCoarseSensor && isUnderfloor) {
      checklist.push(
        html`<strong>Coarse sensor + slab warning:</strong> A <code>0.5–1.0 °C</code> room sensor updates too slowly for small <code>1 °C</code> setpoint steps on underfloor heating—consider upgrading to a <code>0.1 °C</code> room thermometer.`
      );
      textBullets.push('Sensor upgrade recommended: Use a 0.1 °C sensor for small setpoint steps on underfloor heating.');
    }

    if (hasExtra('secondary_heat')) {
      checklist.push(
        html`<strong>Freeze AI learning during stove / DHW windows:</strong> Toggle <code>switch.&lt;room&gt;_pause_learning</code> (or import the ready-made <strong>Pause Learning</strong> blueprints) whenever a wood stove is lit or your boiler diverts to hot tap water. (<a href="/setup/pause-learning/">Pausing learning guide</a>)`
      );
      textBullets.push(
        'Pause Learning: Automate switch.<room>_pause_learning during wood-stove heat or boiler DHW pauses.'
      );
    }

    if (hasExtra('schedules')) {
      checklist.push(
        html`<strong>Automate your presets:</strong> Set your desired temperatures on the device page’s <code>Eco</code>, <code>Comfort</code>, <code>Sleep</code>, and <code>Away</code> number entities, then import the official <a href="/setup/automation-blueprints/">Weekly Schedule &amp; Presence Blueprints</a> (never let an external schedule overwrite calibration offsets).`
      );
      textBullets.push(
        'Schedules: Configure preset temperatures on the device page and drive them via Weekly Schedule / Presence blueprints.'
      );
    }

    if (hasExtra('window') || hasExtra('door')) {
      checklist.push(
        html`<strong>Multiple contacts on one input?</strong> Combine 2+ window contacts (or 2+ door contacts) in <strong>Settings → Devices &amp; Services → Helpers → + Create Helper → Group → Binary sensor group</strong> with <code>All entities</code> turned <strong>Off</strong>.`
      );
      textBullets.push(
        'Multiple window/door sensors: Combine via a Home Assistant Binary sensor group Helper (All entities = Off).'
      );
    }

    if (checklist.length === 0) {
      checklist.push(
        html`<strong>Give learning 2–3 cycles:</strong> Place your external room sensor at chest height away from direct radiator draft or sunlight, and avoid manual tweaks during the first 2–3 heating cycles.`
      );
      textBullets.push('Place room sensor away from radiator draft/sunlight and allow 2–3 heating cycles to settle.');
    }

    const plainLines = [
      'Better Thermostat 2.0 — Recommended Room Configuration',
      '======================================================',
      '',
      'Screen 1 (Room, Sensors & Presets):',
      ...screen1.map(r => `- ${r.field}: ${r.value}`),
      '',
      'Screen 2 (Per-TRV Hardware & Algorithm):',
      ...screen2.map(r => `- ${r.field}: ${r.value}`),
      '',
      'Post-Setup Checklist:',
      ...textBullets.map(b => `- ${b}`)
    ];

    return {
      calType,
      calMode,
      altMode,
      mpcPreset,
      whySummary,
      benchmark,
      screen1,
      screen2,
      checklist,
      plainTextChecklist: plainLines.join('\n')
    };
  }

  private renderIcon(icon: IconId): TemplateResult {
    switch (icon) {
      case 'valve':
        return html`<svg aria-hidden="true" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M12 2v7"/><path d="M8 2h8"/><path d="M4 12h5"/><path d="M15 12h5"/><path d="M4 9v6"/><path d="M20 9v6"/></svg>`;
      case 'bluetooth':
        return html`<svg aria-hidden="true" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="m7 7 10 10-5 5V2l5 5L7 17"/></svg>`;
      case 'sliders':
        return html`<svg aria-hidden="true" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><line x1="4" x2="4" y1="21" y2="14"/><line x1="4" x2="4" y1="10" y2="3"/><line x1="12" x2="12" y1="21" y2="12"/><line x1="12" x2="12" y1="8" y2="3"/><line x1="20" x2="20" y1="21" y2="16"/><line x1="20" x2="20" y1="12" y2="3"/><line x1="2" x2="6" y1="14" y2="14"/><line x1="10" x2="14" y1="8" y2="8"/><line x1="18" x2="22" y1="16" y2="16"/></svg>`;
      case 'radio':
        return html`<svg aria-hidden="true" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4.9 19.1C1 15.2 1 8.8 4.9 4.9"/><path d="M7.8 16.2c-2.3-2.3-2.3-6.1 0-8.5"/><circle cx="12" cy="12" r="2"/><path d="M16.2 7.8c2.3 2.3 2.3 6.1 0 8.5"/><path d="M19.1 4.9C23 8.8 23 15.1 19.1 19"/></svg>`;
      case 'thermometer':
        return html`<svg aria-hidden="true" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M14 4v10.54a4 4 0 1 1-4 0V4a2 2 0 0 1 4 0Z"/></svg>`;
      case 'cpu':
        return html`<svg aria-hidden="true" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect width="16" height="16" x="4" y="4" rx="2"/><rect width="6" height="6" x="9" y="9" rx="1"/><path d="M15 2v2"/><path d="M15 20v2"/><path d="M2 15h2"/><path d="M2 9h2"/><path d="M20 15h2"/><path d="M20 9h2"/><path d="M9 2v2"/><path d="M9 20v2"/></svg>`;
      case 'radiator':
        return html`<svg aria-hidden="true" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect width="18" height="14" x="3" y="5" rx="2"/><line x1="7" x2="7" y1="5" y2="19"/><line x1="12" x2="12" y1="5" y2="19"/><line x1="17" x2="17" y1="5" y2="19"/></svg>`;
      case 'curtain':
        return html`<svg aria-hidden="true" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 4h18"/><path d="M5 4v16"/><path d="M19 4v16"/><path d="M5 12c3 0 5-3 5-8"/><path d="M19 12c-3 0-5-3-5-8"/><path d="M9 20h6"/></svg>`;
      case 'grid':
        return html`<svg aria-hidden="true" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect width="7" height="14" x="3" y="5" rx="1.5"/><rect width="7" height="14" x="14" y="5" rx="1.5"/><path d="M10 12h4"/></svg>`;
      case 'flame':
        return html`<svg aria-hidden="true" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M8.5 14.5A2.5 2.5 0 0 0 11 12c0-1.38-.5-2-1-3-1.072-2.143-.224-4.054 2-6 .5 2.5 2 4.9 4 6.5 2 1.6 3 3.5 3 5.5a7 7 0 1 1-14 0c0-1.153.433-2.294 1-3a2.5 2.5 0 0 0 2.5 2.5z"/></svg>`;
      case 'layers':
        return html`<svg aria-hidden="true" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="m12.83 2.18a2 2 0 0 0-1.66 0L2.6 6.08a1 1 0 0 0 0 1.83l8.58 3.91a2 2 0 0 0 1.66 0l8.58-3.9a1 1 0 0 0 0-1.83Z"/><path d="m22 12.5-8.97 4.08a2 2 0 0 1-1.66 0L2 12.5"/><path d="m22 17-8.97 4.08a2 2 0 0 1-1.66 0L2 17"/></svg>`;
      case 'home':
        return html`<svg aria-hidden="true" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="m3 9 9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><polyline points="9 22 9 12 15 12 15 22"/></svg>`;
      case 'moon':
        return html`<svg aria-hidden="true" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/></svg>`;
      case 'shield':
        return html`<svg aria-hidden="true" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/></svg>`;
      case 'sun':
        return html`<svg aria-hidden="true" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2"/><path d="M12 20v2"/><path d="m4.93 4.93 1.41 1.41"/><path d="m17.66 17.66 1.41 1.41"/><path d="M2 12h2"/><path d="M20 12h2"/><path d="m6.34 17.66-1.41 1.41"/><path d="m19.07 4.93-1.41 1.41"/></svg>`;
      case 'wind':
        return html`<svg aria-hidden="true" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M17.7 7.7a2.5 2.5 0 1 1 1.8 4.3H2"/><path d="M9.6 4.6A2 2 0 1 1 11 8H2"/><path d="M12.6 19.4A2 2 0 1 0 14 16H2"/></svg>`;
      case 'droplet':
        return html`<svg aria-hidden="true" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22a7 7 0 0 0 7-7c0-2-1-3.9-3-5.5s-3.5-4-4-6.5c-.5 2.5-2 4.9-4 6.5C6 11.1 5 13 5 15a7 7 0 0 0 7 7z"/></svg>`;
      case 'zap':
        return html`<svg aria-hidden="true" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 14a1 1 0 0 1-.78-1.63l9.9-10.2a.5.5 0 0 1 .86.46l-1.92 6.02A1 1 0 0 0 13 10h7a1 1 0 0 1 .78 1.63l-9.9 10.2a.5.5 0 0 1-.86-.46l1.92-6.02A1 1 0 0 0 11 14z"/></svg>`;
      case 'activity':
        return html`<svg aria-hidden="true" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M22 12h-2.48a2 2 0 0 0-1.93 1.46l-2.35 8.36a.25.25 0 0 1-.48 0L9.24 2.18a.25.25 0 0 0-.48 0l-2.35 8.36A2 2 0 0 1 4.49 12H2"/></svg>`;
      case 'clock':
        return html`<svg aria-hidden="true" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>`;
      case 'scale':
        return html`<svg aria-hidden="true" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="m16 16 3-8 3 8c-.87.65-1.92 1-3 1s-2.13-.35-3-1Z"/><path d="m2 16 3-8 3 8c-.87.65-1.92 1-3 1s-2.13-.35-3-1Z"/><path d="M7 21h10"/><path d="M12 3v18"/><path d="M3 7h2c2 0 5-1 7-2 2 1 5 2 7 2h2"/></svg>`;
      case 'target':
        return html`<svg aria-hidden="true" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="6"/><circle cx="12" cy="12" r="2"/></svg>`;
      case 'battery':
        return html`<svg aria-hidden="true" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect width="16" height="10" x="2" y="7" rx="2" ry="2"/><line x1="22" x2="22" y1="11" y2="13"/><line x1="6" x2="6" y1="11" y2="13"/><line x1="10" x2="10" y1="11" y2="13"/><line x1="14" x2="14" y1="11" y2="13"/></svg>`;
      case 'trending':
        return html`<svg aria-hidden="true" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><polyline points="22 7 13.5 15.5 8.5 10.5 2 17"/><polyline points="16 7 22 7 22 13"/></svg>`;
      case 'window':
        return html`<svg aria-hidden="true" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect width="18" height="18" x="3" y="3" rx="2"/><line x1="12" x2="12" y1="3" y2="21"/><line x1="3" x2="21" y1="12" y2="12"/></svg>`;
      case 'door':
        return html`<svg aria-hidden="true" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M13 4h3a2 2 0 0 1 2 2v14"/><path d="M2 20h3"/><path d="M13 20h9"/><path d="M10 12v.01"/><path d="M13 4.562v16.157a1 1 0 0 1-1.242.97L5 20V5.562a2 2 0 0 1 1.515-1.94l4-1A2 2 0 0 1 13 4.561Z"/></svg>`;
      case 'cloud':
        return html`<svg aria-hidden="true" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M17.5 19H9a7 7 0 1 1 6.71-9h1.79a4.5 4.5 0 1 1 0 9Z"/></svg>`;
      case 'snowflake':
        return html`<svg aria-hidden="true" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><line x1="2" x2="22" y1="12" y2="12"/><line x1="12" x2="12" y1="2" y2="22"/><path d="m20 16-4-4 4-4"/><path d="m4 8 4 4-4 4"/><path d="m16 4-4 4-4-4"/><path d="m8 20 4-4 4 4"/></svg>`;
      case 'calendar':
        return html`<svg aria-hidden="true" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect width="18" height="18" x="3" y="4" rx="2"/><line x1="16" x2="16" y1="2" y2="6"/><line x1="8" x2="8" y1="2" y2="6"/><line x1="3" x2="21" y1="10" y2="10"/></svg>`;
      case 'pause':
        return html`<svg aria-hidden="true" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect width="4" height="16" x="6" y="4" rx="1"/><rect width="4" height="16" x="14" y="4" rx="1"/></svg>`;
      case 'lock':
        return html`<svg aria-hidden="true" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect width="18" height="11" x="3" y="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>`;
      case 'plug':
        return html`<svg aria-hidden="true" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22v-5"/><path d="M9 8V2"/><path d="M15 8V2"/><path d="M18 8v5a4 4 0 0 1-4 4h-4a4 4 0 0 1-4-4V8Z"/></svg>`;
      case 'check':
        return html`<svg aria-hidden="true" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>`;
      case 'copy':
        return html`<svg aria-hidden="true" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect width="14" height="14" x="8" y="8" rx="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/></svg>`;
      case 'refresh':
        return html`<svg aria-hidden="true" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/></svg>`;
      case 'arrowLeft':
        return html`<svg aria-hidden="true" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m15 18-6-6 6-6"/></svg>`;
      case 'arrowRight':
        return html`<svg aria-hidden="true" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m9 18 6-6-6-6"/></svg>`;
      case 'info':
        return html`<svg aria-hidden="true" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/></svg>`;
    }
  }

  private renderStepper(): TemplateResult {
    const progressLabel = this.isResultsView
      ? 'Configuration ready'
      : `Step ${this.currentStep + 1} of ${WIZARD_STEPS.length}`;

    return html`
      <div class="wizard-top">
        <div class="progress-row">
          <span id="bt-progress-label">Better Thermostat 2.0 Setup Assistant</span>
          <span class="progress-status">${progressLabel}</span>
        </div>
        <progress
          class="wizard-progress"
          max=${WIZARD_STEPS.length}
          .value=${this.answeredStepCount}
          value=${this.answeredStepCount}
          aria-labelledby="bt-progress-label"
        ></progress>
        <div class="stepper" role="group" aria-label="Configuration steps">
          ${WIZARD_STEPS.map((step, index) => {
            const isCurrent = this.currentStep === index;
            const isAnswered = step.multiSelect
              ? this.allRequiredAnswered
              : Boolean(this.answers[step.id]);
            const isReachable =
              index === 0 ||
              isAnswered ||
              Boolean(this.answers[WIZARD_STEPS[index - 1]?.id]);
            return html`
              <button
                type="button"
                class="step-pill ${classMap({ current: isCurrent, answered: isAnswered && !isCurrent })}"
                aria-current=${isCurrent ? 'step' : nothing}
                ?disabled=${!isReachable}
                @click=${() => this.goToStep(index)}
              >
                <span class="step-num" aria-hidden="true">
                  ${isAnswered && !isCurrent ? '✓' : index + 1}
                </span>
                ${step.shortLabel}
              </button>
            `;
          })}
          <button
            type="button"
            class="step-pill ${classMap({ current: this.isResultsView, answered: this.allRequiredAnswered && !this.isResultsView })}"
            aria-current=${this.isResultsView ? 'step' : nothing}
            ?disabled=${!this.allRequiredAnswered}
            @click=${() => this.goToStep(WIZARD_STEPS.length)}
          >
            <span class="step-num" aria-hidden="true">★</span>
            Results
          </button>
        </div>
      </div>
    `;
  }

  private renderStep(): TemplateResult {
    const step = WIZARD_STEPS[this.currentStep];
    const isMulti = Boolean(step.multiSelect);
    const currentSingle = this.answers[step.id];

    return html`
      <div class="step-panel">
        <h2 class="step-heading" tabindex="-1">${step.title}</h2>
        <p class="step-subtitle">${step.subtitle}</p>

        <div class="options-grid" role="group" aria-label=${step.title}>
          ${step.options.map(opt => {
            const isSelected = isMulti
              ? this.extras.includes(opt.value)
              : currentSingle === opt.value;
            return html`
              <button
                type="button"
                class="option-card ${classMap({ selected: isSelected })}"
                aria-pressed=${isSelected ? 'true' : 'false'}
                @click=${() =>
                  isMulti ? this.toggleExtra(opt.value) : this.selectSingleOption(step.id, opt.value)}
              >
                <div class="option-top">
                  <div class="option-title-wrap">
                    <span class="option-icon">${this.renderIcon(opt.icon)}</span>
                    <span class="option-title">${opt.label}</span>
                  </div>
                  <div class="option-meta">
                    ${opt.badge ? html`<span class="badge">${opt.badge}</span>` : nothing}
                    <span class="check-indicator ${classMap({ radio: !isMulti })}" aria-hidden="true">
                      ${this.renderIcon('check')}
                    </span>
                  </div>
                </div>
                <span class="option-desc">${opt.desc}</span>
              </button>
            `;
          })}
        </div>

        <div class="nav-bar">
          <button
            type="button"
            class="btn"
            ?disabled=${this.currentStep === 0}
            @click=${() => this.goToStep(this.currentStep - 1)}
          >
            ${this.renderIcon('arrowLeft')}
            Previous
          </button>

          <div class="nav-actions">
            ${this.allRequiredAnswered && !isMulti
              ? html`
                  <button
                    type="button"
                    class="btn"
                    @click=${() => this.goToStep(WIZARD_STEPS.length)}
                  >
                    Jump to Results
                  </button>
                `
              : nothing}
            ${isMulti
              ? html`
                  <button
                    type="button"
                    class="btn btn-primary"
                    @click=${() => this.goToStep(WIZARD_STEPS.length)}
                  >
                    View My v2 Configuration
                    ${this.renderIcon('arrowRight')}
                  </button>
                `
              : html`
                  <button
                    type="button"
                    class="btn btn-primary"
                    ?disabled=${!currentSingle}
                    @click=${() => this.goToStep(this.currentStep + 1)}
                  >
                    Next
                    ${this.renderIcon('arrowRight')}
                  </button>
                `}
          </div>
        </div>
      </div>
    `;
  }

  private renderResults(): TemplateResult {
    const rec = this.computeRecommendation();

    return html`
      <div class="step-panel">
        <h2 class="step-heading" tabindex="-1">Your Better Thermostat 2.0 Profile</h2>
        <p class="step-subtitle">
          Click any summary tag below to tweak an answer and recalculate live, or copy the checklist while configuring Home Assistant.
        </p>

        <div class="summary-chips" role="group" aria-label="Edit chosen room parameters">
          ${WIZARD_STEPS.map(
            (step, idx) => html`
              <button
                type="button"
                class="summary-chip"
                @click=${() => this.goToStep(idx)}
              >
                <span>${step.shortLabel}:</span>
                <strong>${this.shortChoiceLabel(step.id)}</strong>
              </button>
            `
          )}
        </div>

        <div class="verdict-card">
          <div class="verdict-grid">
            <div class="verdict-box">
              <div class="verdict-eyebrow">Screen 2 · Calibration type</div>
              <div class="verdict-value">${rec.calType}</div>
              <div class="verdict-alt">Hardware channel to your TRV</div>
            </div>
            <div class="verdict-box">
              <div class="verdict-eyebrow">Screen 2 · Calibration mode</div>
              <div class="verdict-value">${rec.calMode}</div>
              ${rec.mpcPreset
                ? html`<div class="verdict-alt">MPC v2 room size: <strong>${rec.mpcPreset}</strong></div>`
                : rec.altMode
                  ? html`<div class="verdict-alt">Runner-up alternative: <strong>${rec.altMode}</strong></div>`
                  : nothing}
            </div>
          </div>

          <p class="why-text">${rec.whySummary}</p>

          <div class="bench-box">
            <div class="bench-header">
              <span class="bench-title">${rec.benchmark.label}</span>
              <span class="bench-caption">${rec.benchmark.summary}</span>
            </div>
            <div class="bench-bars">
              <div class="bench-metric">
                <div class="bench-metric-label">
                  <span id="bm-comfort">Setpoint Comfort</span>
                  <strong>${rec.benchmark.comfort} %</strong>
                </div>
                <progress
                  class="metric-bar"
                  max="100"
                  .value=${rec.benchmark.comfort}
                  value=${rec.benchmark.comfort}
                  aria-labelledby="bm-comfort"
                ></progress>
              </div>
              <div class="bench-metric">
                <div class="bench-metric-label">
                  <span id="bm-actuator">Valve &amp; Battery Life</span>
                  <strong>${rec.benchmark.actuator} %</strong>
                </div>
                <progress
                  class="metric-bar"
                  max="100"
                  .value=${rec.benchmark.actuator}
                  value=${rec.benchmark.actuator}
                  aria-labelledby="bm-actuator"
                ></progress>
              </div>
              <div class="bench-metric">
                <div class="bench-metric-label">
                  <span id="bm-energy">Energy Discipline</span>
                  <strong>${rec.benchmark.energy} %</strong>
                </div>
                <progress
                  class="metric-bar"
                  max="100"
                  .value=${rec.benchmark.energy}
                  value=${rec.benchmark.energy}
                  aria-labelledby="bm-energy"
                ></progress>
              </div>
            </div>
          </div>
        </div>

        <div class="settings-card">
          <h3 class="card-heading">
            ${this.renderIcon('sliders')}
            Screen 1 — Room, Sensors &amp; Presets
          </h3>
          <p class="card-subheading">
            Exact field labels shown on the first dialog screen in Home Assistant.
          </p>
          <div class="setting-list">
            ${rec.screen1.map(
              row => html`
                <div class="setting-row">
                  <div>
                    <div class="setting-field">${row.field}</div>
                    <div class="setting-hint">${row.hint}</div>
                  </div>
                  <span class="setting-pill ${classMap({ highlight: Boolean(row.highlight) })}">
                    ${row.value}
                  </span>
                </div>
              `
            )}
          </div>
        </div>

        <div class="settings-card">
          <h3 class="card-heading">
            ${this.renderIcon('valve')}
            Screen 2 — Per-TRV Hardware &amp; Control Settings
          </h3>
          <p class="card-subheading">
            Exact dropdown values and toggles shown after clicking Submit on Screen 1.
          </p>
          <div class="setting-list">
            ${rec.screen2.map(
              row => html`
                <div class="setting-row">
                  <div>
                    <div class="setting-field">${row.field}</div>
                    <div class="setting-hint">${row.hint}</div>
                  </div>
                  <span class="setting-pill ${classMap({ highlight: Boolean(row.highlight) })}">
                    ${row.value}
                  </span>
                </div>
              `
            )}
          </div>
        </div>

        <div class="settings-card">
          <h3 class="card-heading">
            ${this.renderIcon('info')}
            Hardware Prep, Device Page Helpers &amp; Blueprints
          </h3>
          <ul class="checklist">
            ${rec.checklist.map(item => html`<li>${item}</li>`)}
          </ul>
        </div>

        <div class="nav-bar">
          <button
            type="button"
            class="btn"
            @click=${() => this.goToStep(WIZARD_STEPS.length - 1)}
          >
            ${this.renderIcon('arrowLeft')}
            Edit Extras
          </button>

          <div class="nav-actions">
            <span class="copy-status" role="status" aria-live="polite">
              ${this.copyFeedback}
            </span>
            <button
              type="button"
              class="btn"
              @click=${this.restart}
            >
              ${this.renderIcon('refresh')}
              Configure Another Room
            </button>
            <button
              type="button"
              class="btn btn-primary"
              @click=${() => this.copyChecklist(rec.plainTextChecklist)}
            >
              ${this.renderIcon('copy')}
              Copy Setup Checklist
            </button>
          </div>
        </div>
      </div>
    `;
  }

  render(): TemplateResult {
    return html`
      <div class="shell">
        ${this.renderStepper()}
        ${this.isResultsView ? this.renderResults() : this.renderStep()}
      </div>
    `;
  }
}
