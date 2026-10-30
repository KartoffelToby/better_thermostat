import { LitElement, html, css } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import { classMap } from 'lit/directives/class-map.js';
import gsap from 'gsap';

interface QuestionOption {
  value: string;
  label: string;
  desc: string;
  icon: string;
}

interface Question {
  id: string;
  title: string;
  options: QuestionOption[];
}

const questions: Question[] = [
  {
    id: 'trvChannel',
    title: 'How is your radiator valve (TRV) integrated?',
    options: [
      {
        value: 'direct_valve',
        label: 'Direct Valve Control (0–100 %)',
        desc: 'Sonoff TRVZB (Z2M), Shelly BLU TRV, Eurotronic Spirit, or TRV exposing valve position',
        icon: '🎛️'
      },
      {
        value: 'offset',
        label: 'Open Radiator + Local Offset',
        desc: 'Most Zigbee2MQTT TRVs standing freely in the room with local_temperature_calibration',
        icon: '🌡️'
      },
      {
        value: 'setpoint',
        label: 'Covered Radiator, Narrow Offset, or ZHA / tado°',
        desc: 'Behind curtains/furniture, narrow ±2.5–5 °C offset cap (e.g. Saswell), or ZHA / tado°',
        icon: '🪟'
      },
      {
        value: 'homematic',
        label: 'HomematicIP / OpenCCU (868 MHz)',
        desc: 'HomematicIP Access Point or OpenCCU with strict 1 % radio duty-cycle limit',
        icon: '📡'
      }
    ]
  },
  {
    id: 'heatingType',
    title: 'What heats this room?',
    options: [
      {
        value: 'radiator_single',
        label: 'Single Standard Radiator',
        desc: 'One radiator valve in the room',
        icon: '♨️'
      },
      {
        value: 'radiator_multi',
        label: 'Multiple Radiators (2+ TRVs)',
        desc: 'Two or more TRVs grouped in one Better Thermostat room',
        icon: '🔗'
      },
      {
        value: 'radiator_oversized',
        label: 'Oversized / Fast Radiator',
        desc: 'Warms the room rapidly and tends to overshoot the target',
        icon: '🚀'
      },
      {
        value: 'underfloor',
        label: 'Underfloor Heating / Cast-Iron',
        desc: 'High thermal mass with slow 1–2 hour response time',
        icon: '🦶'
      }
    ]
  },
  {
    id: 'roomType',
    title: 'What type of room is this?',
    options: [
      {
        value: 'living',
        label: 'Living Room / Home Office',
        desc: 'Everyday living space needing steady daytime comfort',
        icon: '🛋️'
      },
      {
        value: 'bedroom',
        label: 'Bedroom / Nursery',
        desc: 'Quiet motor movement and zero temperature overshoot',
        icon: '🛏️'
      },
      {
        value: 'disturbance',
        label: 'Kitchen / Sunny Room / Hallway',
        desc: 'Hit by sudden heat or cold from cooking, sunshine, or doors',
        icon: '☀️'
      },
      {
        value: 'bathroom',
        label: 'Bathroom',
        desc: 'Smaller room needing reliable warmth and humidity triggers',
        icon: '🛁'
      }
    ]
  },
  {
    id: 'roomSize',
    title: 'Roughly how large is the room?',
    options: [
      {
        value: 'small',
        label: 'Small (< 12 m²)',
        desc: 'Compact study, bathroom, or small bedroom',
        icon: '📏'
      },
      {
        value: 'medium',
        label: 'Medium (12–25 m²)',
        desc: 'Typical bedroom, kitchen, or living room',
        icon: '📐'
      },
      {
        value: 'large',
        label: 'Large (> 25 m²)',
        desc: 'Open-plan living / dining space',
        icon: '🏢'
      }
    ]
  },
  {
    id: 'insulation',
    title: 'How well does the room hold heat?',
    options: [
      {
        value: 'good',
        label: 'Well Insulated',
        desc: 'Modern building / triple glazing — holds heat long, overshoots easily',
        icon: '🛡️'
      },
      {
        value: 'average',
        label: 'Average Insulation',
        desc: 'Standard house or apartment',
        icon: '🏠'
      },
      {
        value: 'poor',
        label: 'Poorly Insulated / Drafty',
        desc: 'Older building — loses heat fast and warms up sluggishly',
        icon: '🥶'
      }
    ]
  },
  {
    id: 'sensors',
    title: 'Which optional sensors are configured?',
    options: [
      {
        value: 'both',
        label: 'Window/Door + Outdoor/Weather',
        desc: 'Pauses while airing and shuts off automatically in warm weather',
        icon: '🌤️'
      },
      {
        value: 'window',
        label: 'Window or Door Sensor Only',
        desc: 'Pauses heating automatically when airing the room',
        icon: '🚪'
      },
      {
        value: 'outdoor',
        label: 'Outdoor Sensor or Weather Only',
        desc: 'Automatic 24h-damped Summer Mode shutdown',
        icon: '🌡️'
      },
      {
        value: 'none',
        label: 'Room Temperature Sensor Only',
        desc: 'Only the mandatory external room thermometer',
        icon: '📍'
      }
    ]
  }
];

@customElement('bt-configurator')
export class BtConfigurator extends LitElement {
  static styles = css`
    :host {
      display: block;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
      --primary-color: #059669;
      --primary-bright: #10b981;
      --surface-bg: var(--sl-color-bg-nav, #111827);
      --card-bg: var(--sl-color-bg-sidebar, rgba(31, 41, 55, 0.7));
      --text-main: var(--sl-color-white, #f9fafb);
      --text-muted: var(--sl-color-gray-2, #9ca3af);
      --border-color: var(--sl-color-gray-5, rgba(148, 163, 184, 0.28));
      --accent-tint: rgba(16, 185, 129, 0.14);
    }

    .container {
      background: var(--surface-bg);
      border: 1px solid var(--border-color);
      border-radius: 1.25rem;
      padding: 2rem;
      margin: 1.75rem 0;
      box-shadow: 0 16px 36px -12px rgba(0, 0, 0, 0.22);
      position: relative;
      overflow: hidden;
      color: var(--text-main);
    }

    .content-wrapper {
      position: relative;
      z-index: 1;
    }

    .progress-container {
      display: flex;
      align-items: center;
      gap: 1rem;
      margin-bottom: 2rem;
    }

    .progress-bar-bg {
      flex-grow: 1;
      height: 0.5rem;
      background-color: var(--border-color);
      border-radius: 1rem;
      overflow: hidden;
      position: relative;
    }

    .progress-bar-fill {
      height: 100%;
      background: linear-gradient(90deg, var(--primary-bright), var(--primary-color));
      border-radius: 1rem;
      width: 0%;
    }

    .progress-text {
      font-size: 0.875rem;
      font-weight: 700;
      color: var(--primary-bright);
      min-width: 4.5rem;
      text-align: right;
    }

    .step-container {
      min-height: 320px;
      position: relative;
    }

    .step {
      width: 100%;
    }

    .step-title {
      font-size: 1.6rem;
      font-weight: 700;
      margin: 0 0 1.5rem 0;
      color: var(--text-main);
      letter-spacing: -0.02em;
    }

    .options-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 1rem;
    }

    .option-card {
      appearance: none;
      font: inherit;
      color: inherit;
      background-color: var(--card-bg);
      border: 2px solid var(--border-color);
      border-radius: 1rem;
      padding: 1.25rem;
      cursor: pointer;
      display: flex;
      flex-direction: column;
      align-items: flex-start;
      text-align: left;
      transition: transform 0.2s ease, border-color 0.2s ease, background-color 0.2s ease, box-shadow 0.2s ease;
      position: relative;
      width: 100%;
    }

    .option-card:hover {
      transform: translateY(-2px);
      border-color: var(--primary-bright);
      box-shadow: 0 8px 20px -6px rgba(16, 185, 129, 0.25);
    }

    .option-card:focus-visible,
    .btn:focus-visible {
      outline: 3px solid var(--primary-bright);
      outline-offset: 3px;
    }

    .option-card.selected {
      background-color: var(--accent-tint);
      border-color: var(--primary-bright);
    }

    .option-icon {
      font-size: 2rem;
      margin-bottom: 0.75rem;
      line-height: 1;
    }

    .option-title {
      font-weight: 700;
      font-size: 1.05rem;
      margin-bottom: 0.4rem;
      color: var(--text-main);
    }

    .option-desc {
      font-size: 0.875rem;
      color: var(--text-muted);
      line-height: 1.45;
    }

    .navigation {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-top: 2rem;
      padding-top: 1.25rem;
      border-top: 1px solid var(--border-color);
    }

    .btn {
      font: inherit;
      padding: 0.7rem 1.35rem;
      border-radius: 0.75rem;
      font-weight: 600;
      font-size: 0.95rem;
      cursor: pointer;
      border: 1px solid transparent;
      display: inline-flex;
      align-items: center;
      gap: 0.5rem;
      transition: transform 0.2s ease, background-color 0.2s ease, opacity 0.2s ease;
    }

    .btn:disabled {
      opacity: 0.45;
      cursor: not-allowed;
      transform: none !important;
    }

    .btn-primary {
      background: linear-gradient(135deg, var(--primary-bright), var(--primary-color));
      color: #ffffff;
    }

    .btn-primary:hover:not(:disabled) {
      transform: translateY(-1px);
    }

    .btn-secondary {
      background-color: var(--card-bg);
      color: var(--text-main);
      border-color: var(--border-color);
    }

    .btn-secondary:hover:not(:disabled) {
      border-color: var(--primary-bright);
    }

    .result-header {
      text-align: center;
      margin-bottom: 2rem;
    }

    .result-title {
      font-size: 2rem;
      font-weight: 800;
      color: var(--primary-bright);
      margin-bottom: 0.5rem;
    }

    .result-subtitle {
      color: var(--text-muted);
      font-size: 1.05rem;
      margin: 0;
    }

    .recommendation-card {
      background: var(--card-bg);
      border: 1px solid var(--border-color);
      border-radius: 1rem;
      padding: 1.5rem;
      margin-bottom: 1.25rem;
    }

    .recommendation-card h3 {
      margin-top: 0;
      margin-bottom: 1rem;
      color: var(--text-main);
      display: flex;
      align-items: center;
      gap: 0.6rem;
      font-size: 1.15rem;
    }

    .setting-item {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 1rem;
      flex-wrap: wrap;
      padding: 0.85rem 0;
      border-bottom: 1px solid var(--border-color);
    }

    .setting-item:last-child {
      border-bottom: none;
      padding-bottom: 0;
    }

    .setting-label {
      color: var(--text-muted);
      font-weight: 500;
      font-size: 0.95rem;
    }

    .setting-value {
      color: var(--text-main);
      font-weight: 700;
      background: var(--accent-tint);
      padding: 0.3rem 0.75rem;
      border-radius: 0.5rem;
      border: 1px solid rgba(16, 185, 129, 0.35);
      font-size: 0.92rem;
    }

    .note-box {
      background: rgba(59, 130, 246, 0.12);
      border-left: 4px solid #3b82f6;
      padding: 1.15rem 1.25rem;
      border-radius: 0 0.75rem 0.75rem 0;
      margin-top: 1.25rem;
    }

    .note-box p {
      margin: 0 0 0.65rem 0;
      color: var(--text-main);
      line-height: 1.5;
      font-size: 0.95rem;
    }

    .note-box p:last-child {
      margin-bottom: 0;
    }

    @media (max-width: 640px) {
      .container {
        padding: 1.25rem;
      }
      .step-title {
        font-size: 1.35rem;
      }
    }

    @media (prefers-reduced-motion: reduce) {
      .option-card,
      .btn {
        transition: none !important;
      }
      .option-card:hover,
      .btn-primary:hover:not(:disabled) {
        transform: none !important;
      }
    }
  `;

  @state()
  private currentStep = 0;

  @state()
  private answers: Record<string, string> = {};

  @state()
  private isAnimating = false;

  private get prefersReducedMotion(): boolean {
    return typeof window !== 'undefined' &&
      window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  }

  private get isComplete(): boolean {
    return this.currentStep >= questions.length;
  }

  private get progress(): number {
    if (this.isComplete) return 100;
    return (this.currentStep / questions.length) * 100;
  }

  updated(changedProperties: Map<string, unknown>) {
    if (changedProperties.has('currentStep')) {
      this.animateProgressBar();
      this.animateStepChange();
    }
  }

  firstUpdated() {
    this.animateProgressBar();
    if (!this.prefersReducedMotion) {
      const container = this.shadowRoot?.querySelector('.container');
      if (container) {
        gsap.from(container, {
          y: 16,
          opacity: 0,
          duration: 0.45,
          ease: 'power2.out'
        });
      }
    }
  }

  private animateProgressBar() {
    const bar = this.shadowRoot?.querySelector('.progress-bar-fill') as HTMLElement | null;
    if (!bar) return;
    if (this.prefersReducedMotion) {
      bar.style.width = `${this.progress}%`;
      return;
    }
    gsap.to(bar, {
      width: `${this.progress}%`,
      duration: 0.35,
      ease: 'power2.out'
    });
  }

  private animateStepChange() {
    if (this.prefersReducedMotion) return;
    const activeStep = this.shadowRoot?.querySelector('.step');
    if (!activeStep) return;
    gsap.fromTo(
      activeStep,
      { opacity: 0, x: 18 },
      { opacity: 1, x: 0, duration: 0.3, ease: 'power2.out' }
    );
  }

  private handleOptionSelect(questionId: string, value: string) {
    if (this.isAnimating) return;
    this.answers = { ...this.answers, [questionId]: value };

    if (this.prefersReducedMotion) {
      this.currentStep++;
      return;
    }

    this.isAnimating = true;
    setTimeout(() => {
      this.currentStep++;
      this.isAnimating = false;
    }, 180);
  }

  private nextStep() {
    if (this.currentStep < questions.length) {
      this.currentStep++;
    }
  }

  private prevStep() {
    if (this.currentStep > 0) {
      this.currentStep--;
    }
  }

  private restart() {
    this.answers = {};
    this.currentStep = 0;
  }

  private renderQuestion() {
    const question = questions[this.currentStep];
    const currentAnswer = this.answers[question.id];

    return html`
      <div class="step">
        <h2 class="step-title">${question.title}</h2>
        <div class="options-grid" role="group" aria-label=${question.title}>
          ${question.options.map(opt => {
            const isSelected = currentAnswer === opt.value;
            return html`
              <button
                type="button"
                class="option-card ${classMap({ selected: isSelected })}"
                aria-pressed=${isSelected ? 'true' : 'false'}
                data-value=${opt.value}
                @click=${() => this.handleOptionSelect(question.id, opt.value)}
              >
                <span class="option-icon" aria-hidden="true">${opt.icon}</span>
                <span class="option-title">${opt.label}</span>
                <span class="option-desc">${opt.desc}</span>
              </button>
            `;
          })}
        </div>
      </div>
    `;
  }

  private renderResults() {
    const { trvChannel, heatingType, roomType, roomSize, insulation, sensors } = this.answers;

    // 1. Calibration type (Hardware channel)
    let calType = 'Offset Based';
    if (trvChannel === 'direct_valve') {
      calType = 'Direct Valve Based';
    } else if (trvChannel === 'setpoint') {
      calType = 'Target Temperature Based';
    } else if (trvChannel === 'homematic') {
      calType = 'Offset Based (if TEMPERATURE_OFFSET enabled) or Target Temperature Based';
    }

    // 2. Calibration mode (Control brain) + Tolerance
    let calMode = '(AI) Time Based (Default)';
    let whyText =
      'Measures your room’s actual warming and cooling rate over 2–3 heating cycles and scales heating demand automatically without manual tuning.';
    let tolerance = '0.3 °C';
    let mpcRoomSize: string | null = null;
    const notes: unknown[] = [];

    if (heatingType === 'underfloor') {
      calMode = '(AI) Time Based (Default)';
      tolerance = '0.5 °C';
      whyText =
        'Underfloor slabs carry hours of thermal inertia. A 0.5 °C tolerance band avoids short-cycling while (AI) Time Based adapts to the slow heat-up slope (TPI Controller is also a great alternative).';
      notes.push(
        html`<strong>Underfloor tip:</strong> Keep night setbacks small (max 1.0–1.5 °C below daytime Comfort) so reheating the concrete slab does not overshoot in the morning.`
      );
    } else if (heatingType === 'radiator_multi') {
      calMode = 'TPI Controller';
      tolerance = '0.3 °C';
      whyText =
        'With 2 or more radiators grouped in one room, TPI Controller holds a calm, synchronized proportional opening without two self-learning loops over-reacting against each other.';
    } else if (trvChannel === 'direct_valve') {
      tolerance = '0.2 °C – 0.3 °C';
      if (roomType === 'disturbance') {
        calMode = 'PID Controller';
        whyText =
          'Your TRV supports direct 0–100 % valve positioning and the room sees sudden heat/cold disturbances (cooking, sun, or hallway drafts). PID reacts quickly to rate-of-change spikes—make sure your room sensor reports 0.1 °C steps (otherwise pick (AI) MPC v2 or TPI Controller).';
      } else {
        calMode = '(AI) MPC v2 (QP + Kalman, experimental)';
        mpcRoomSize =
          roomSize === 'small'
            ? 'Small (or Auto)'
            : roomSize === 'large'
              ? 'Large (or Auto)'
              : 'Auto';
        whyText =
          'Built specifically for Direct Valve Based TRVs: MPC v2 predicts radiator and room thermal momentum ahead of time so it brakes softly before overshooting. Prefer a non-experimental zero-learning option? Pick TPI Controller.';
      }
    } else if (
      insulation === 'poor' &&
      heatingType !== 'radiator_oversized' &&
      roomType !== 'bedroom'
    ) {
      calMode = 'Aggressive';
      tolerance = '0.3 °C';
      whyText =
        'Poorly insulated rooms lose heat quickly and can stall near the target. Aggressive skips the start-up tolerance delay and adds a +2.5 °C boost while heating for fast warm-up.';
    } else if (
      roomType === 'bedroom' ||
      insulation === 'good' ||
      heatingType === 'radiator_oversized'
    ) {
      calMode = 'TPI Controller';
      tolerance = '0.3 °C';
      whyText =
        'TPI Controller throttles smoothly as the room approaches target, preventing overshoot in well-insulated or fast-heating rooms while keeping valve motor noise and battery wear very low.';
    } else if (roomType === 'disturbance') {
      calMode = 'TPI Controller';
      tolerance = '0.3 °C';
      whyText =
        'TPI Controller shuts down cleanly as soon as cooking or sunshine warms the room past target, without needing Direct Valve hardware or heavy battery movement (switch to PID Controller only if you want tighter tracking with a fast 0.1 °C room sensor).';
    }

    // 3. Extra notes & sensors
    const hasWindow = sensors === 'both' || sensors === 'window';
    const hasOutdoor = sensors === 'both' || sensors === 'outdoor';
    const windowDelay = hasWindow
      ? '120 s (2 min) open / 180 s (3 min) close'
      : 'Not used (no window/door sensor selected)';

    let outdoorThreshold = 'Not used (no outdoor/weather entity selected)';
    if (hasOutdoor) {
      outdoorThreshold =
        insulation === 'good'
          ? '15 °C – 16 °C (24h day/night smoothed)'
          : insulation === 'poor'
            ? '17 °C – 18 °C (24h day/night smoothed)'
            : '16 °C (24h day/night smoothed)';
    }

    const overheatingToggle =
      calMode === '(AI) Time Based (Default)' || calMode === 'Aggressive'
        ? 'On (active in this mode)'
        : 'On (handled natively by controller)';

    if (heatingType === 'radiator_oversized') {
      notes.push(
        html`<strong>Oversized radiator tip:</strong> On the Better Thermostat device page in Home Assistant, lower <code>Valve Max Opening</code> to <strong>60–75 %</strong> to tame heat surges.`
      );
    }

    if (trvChannel === 'setpoint') {
      notes.push(
        html`<strong>Target Temperature Based note:</strong> Seeing <code>26 °C</code> or <code>30 °C</code> temporarily on your radiator’s physical display while heating is completely normal—Better Thermostat lowers it again as your room reaches target.`
      );
    }

    if (trvChannel === 'homematic') {
      notes.push(
        html`<strong>HomematicIP duty cycle:</strong> Keep <em>"If you use HomematicIP, you should enable this to slow down the requests to prevent the duty cycle"</em> turned <strong>On</strong> so background writes are spaced to 10 minutes.`
      );
    }

    return html`
      <div class="step">
        <div class="result-header">
          <div class="result-title">Recommended Room Profile</div>
          <p class="result-subtitle">Apply these values on Screen 1 and Screen 2 of Better Thermostat’s configuration.</p>
        </div>

        <div class="recommendation-card">
          <h3>
            <svg aria-hidden="true" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"></circle><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"></path></svg>
            Screen 1 &amp; Screen 2 Settings
          </h3>
          <div class="setting-item">
            <span class="setting-label">Calibration type (Screen 2)</span>
            <span class="setting-value">${calType}</span>
          </div>
          <div class="setting-item">
            <span class="setting-label">Calibration mode (Screen 2)</span>
            <span class="setting-value">${calMode}</span>
          </div>
          ${mpcRoomSize
            ? html`
                <div class="setting-item">
                  <span class="setting-label">MPC v2 room size (Screen 2)</span>
                  <span class="setting-value">${mpcRoomSize}</span>
                </div>
              `
            : ''}
          <div class="setting-item">
            <span class="setting-label">Tolerance (Screen 1)</span>
            <span class="setting-value">${tolerance}</span>
          </div>
          <div class="setting-item">
            <span class="setting-label">Overheating protection? (Screen 2)</span>
            <span class="setting-value">${overheatingToggle}</span>
          </div>
          <div class="setting-item">
            <span class="setting-label">Window open / close delays (Screen 1)</span>
            <span class="setting-value">${windowDelay}</span>
          </div>
          <div class="setting-item">
            <span class="setting-label">Outdoor shutdown threshold (Screen 1)</span>
            <span class="setting-value">${outdoorThreshold}</span>
          </div>
        </div>

        <div class="recommendation-card">
          <h3>
            <svg aria-hidden="true" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z"></path></svg>
            Why this combination fits your room
          </h3>
          <p style="color: var(--text-muted); margin: 0; line-height: 1.55;">${whyText}</p>
        </div>

        ${notes.length > 0
          ? html`
              <div class="note-box">
                ${notes.map(note => html`<p>${note}</p>`)}
              </div>
            `
          : ''}

        <div style="margin-top: 2rem; text-align: center;">
          <button type="button" class="btn btn-secondary" @click=${this.restart}>
            <svg aria-hidden="true" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"></path><path d="M3 3v5h5"></path></svg>
            Configure Another Room
          </button>
        </div>
      </div>
    `;
  }

  render() {
    const currentQuestion = questions[this.currentStep];
    const hasAnswer = currentQuestion && this.answers[currentQuestion.id] !== undefined;
    const progressPct = Math.round(this.progress);

    return html`
      <div class="container">
        <div class="content-wrapper">
          <div class="progress-container">
            <div
              class="progress-bar-bg"
              role="progressbar"
              aria-label="Configuration wizard progress"
              aria-valuenow=${progressPct}
              aria-valuemin="0"
              aria-valuemax="100"
            >
              <div class="progress-bar-fill"></div>
            </div>
            <div class="progress-text">
              ${this.isComplete ? 'Done' : `Step ${this.currentStep + 1}/${questions.length}`}
            </div>
          </div>

          <div class="step-container">
            ${!this.isComplete ? this.renderQuestion() : this.renderResults()}
          </div>

          ${!this.isComplete
            ? html`
                <div class="navigation">
                  <button
                    type="button"
                    class="btn btn-secondary"
                    @click=${this.prevStep}
                    ?disabled=${this.currentStep === 0}
                    style="visibility: ${this.currentStep > 0 ? 'visible' : 'hidden'}"
                  >
                    <svg aria-hidden="true" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="19" y1="12" x2="5" y2="12"></line><polyline points="12 19 5 12 12 5"></polyline></svg>
                    Back
                  </button>
                  <button
                    type="button"
                    class="btn btn-primary"
                    @click=${this.nextStep}
                    ?disabled=${!hasAnswer}
                  >
                    ${this.currentStep === questions.length - 1 ? 'Show Recommendation' : 'Next'}
                    <svg aria-hidden="true" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="5" y1="12" x2="19" y2="12"></line><polyline points="12 5 19 12 12 19"></polyline></svg>
                  </button>
                </div>
              `
            : ''}
        </div>
      </div>
    `;
  }
}
