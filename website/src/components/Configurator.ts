import { LitElement, html, css } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import { classMap } from 'lit/directives/class-map.js';
import gsap from 'gsap';

const questions = [
  {
    id: 'roomType',
    title: 'What type of room is this?',
    options: [
      { value: 'living', label: 'Living Room / Bedroom', desc: 'Standard living spaces', icon: '🛋️' },
      { value: 'bathroom', label: 'Bathroom', desc: 'Needs quick heat, high humidity', icon: '🛁' },
      { value: 'kitchen', label: 'Kitchen', desc: 'Has other heat sources (oven, stove)', icon: '🍳' },
      { value: 'hallway', label: 'Hallway', desc: 'Drafts, doors opening frequently', icon: '🚪' }
    ]
  },
  {
    id: 'roomSize',
    title: 'What is the size of the room?',
    options: [
      { value: 'small', label: 'Small', desc: '< 10m² (e.g., small bathroom)', icon: '📏' },
      { value: 'medium', label: 'Medium', desc: '10-20m² (e.g., standard bedroom)', icon: '📐' },
      { value: 'large', label: 'Large', desc: '> 20m² (e.g., open living room)', icon: '🏢' }
    ]
  },
  {
    id: 'insulation',
    title: 'How well is the room insulated?',
    options: [
      { value: 'poor', label: 'Poor', desc: 'Old building, heats slowly, loses heat fast', icon: '🥶' },
      { value: 'average', label: 'Average', desc: 'Standard insulation', icon: '🏠' },
      { value: 'good', label: 'Good', desc: 'New building, holds heat well', icon: '🔥' }
    ]
  },
  {
    id: 'heatingType',
    title: 'What type of heating do you have?',
    options: [
      { value: 'radiator', label: 'Radiator', desc: 'Fast response time', icon: '♨️' },
      { value: 'underfloor', label: 'Underfloor Heating', desc: 'Slow response, prone to overshoot', icon: '🦶' }
    ]
  },
  {
    id: 'windowSensor',
    title: 'Do you have window/door sensors?',
    options: [
      { value: 'yes', label: 'Yes', desc: 'Immediate reaction to open windows', icon: '🪟' },
      { value: 'no', label: 'No', desc: 'Relying on temperature drop detection', icon: '❌' }
    ]
  }
];

@customElement('bt-configurator')
export class BtConfigurator extends LitElement {
  static styles = css`
    :host {
      display: block;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, 'Open Sans', 'Helvetica Neue', sans-serif;
      --primary-color: #10b981; /* Emerald 500 */
      --primary-hover: #059669; /* Emerald 600 */
      --bg-color: #111827; /* Gray 900 */
      --card-bg: #1f2937; /* Gray 800 */
      --card-hover: #374151; /* Gray 700 */
      --text-main: #f9fafb; /* Gray 50 */
      --text-muted: #9ca3af; /* Gray 400 */
      --border-color: #374151; /* Gray 700 */
      --accent-glow: rgba(16, 185, 129, 0.15);
    }

    .container {
      background: linear-gradient(145deg, var(--bg-color), #0f172a);
      border: 1px solid var(--border-color);
      border-radius: 1.5rem;
      padding: 2.5rem;
      margin: 2rem 0;
      box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.5), 0 0 0 1px rgba(255, 255, 255, 0.05) inset;
      position: relative;
      overflow: hidden;
      color: var(--text-main);
    }

    /* Futuristic background glow */
    .container::before {
      content: '';
      position: absolute;
      top: -50%;
      left: -50%;
      width: 200%;
      height: 200%;
      background: radial-gradient(circle at 50% 0%, var(--accent-glow), transparent 50%);
      pointer-events: none;
      z-index: 0;
    }

    .content-wrapper {
      position: relative;
      z-index: 1;
    }

    .progress-container {
      display: flex;
      align-items: center;
      gap: 1rem;
      margin-bottom: 2.5rem;
    }

    .progress-bar-bg {
      flex-grow: 1;
      height: 0.5rem;
      background-color: rgba(255, 255, 255, 0.1);
      border-radius: 1rem;
      overflow: hidden;
      position: relative;
    }

    .progress-bar-fill {
      height: 100%;
      background: linear-gradient(90deg, #34d399, var(--primary-color));
      border-radius: 1rem;
      width: 0%;
      box-shadow: 0 0 10px var(--primary-color);
    }

    .progress-text {
      font-size: 0.875rem;
      font-weight: 600;
      color: var(--primary-color);
      min-width: 3rem;
      text-align: right;
    }

    .step-container {
      min-height: 350px;
      position: relative;
    }

    .step {
      position: absolute;
      top: 0;
      left: 0;
      width: 100%;
      opacity: 0;
      visibility: hidden;
    }

    .step.active {
      position: relative;
      opacity: 1;
      visibility: visible;
    }

    .step-title {
      font-size: 2rem;
      font-weight: 700;
      margin-bottom: 2rem;
      background: linear-gradient(to right, #fff, #a7f3d0);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
      letter-spacing: -0.025em;
    }

    .options-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 1.25rem;
    }

    .option-card {
      background-color: rgba(31, 41, 55, 0.6);
      backdrop-filter: blur(10px);
      border: 1px solid rgba(255, 255, 255, 0.1);
      border-radius: 1rem;
      padding: 1.5rem;
      cursor: pointer;
      display: flex;
      flex-direction: column;
      align-items: flex-start;
      text-align: left;
      transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
      position: relative;
      overflow: hidden;
    }

    .option-card::after {
      content: '';
      position: absolute;
      inset: 0;
      border-radius: 1rem;
      padding: 2px;
      background: linear-gradient(135deg, var(--primary-color), transparent);
      -webkit-mask: linear-gradient(#fff 0 0) content-box, linear-gradient(#fff 0 0);
      -webkit-mask-composite: xor;
      mask-composite: exclude;
      opacity: 0;
      transition: opacity 0.3s ease;
    }

    .option-card:hover {
      transform: translateY(-4px);
      background-color: rgba(55, 65, 81, 0.8);
      box-shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.3);
    }

    .option-card.selected {
      background-color: rgba(16, 185, 129, 0.1);
      border-color: transparent;
    }

    .option-card.selected::after {
      opacity: 1;
    }

    .option-icon {
      font-size: 2.5rem;
      margin-bottom: 1rem;
      filter: drop-shadow(0 4px 6px rgba(0,0,0,0.2));
    }

    .option-title {
      font-weight: 600;
      font-size: 1.125rem;
      margin-bottom: 0.5rem;
      color: #fff;
    }

    .option-desc {
      font-size: 0.875rem;
      color: var(--text-muted);
      line-height: 1.4;
    }

    .navigation {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-top: 3rem;
      padding-top: 1.5rem;
      border-top: 1px solid rgba(255, 255, 255, 0.1);
    }

    .btn {
      padding: 0.75rem 1.5rem;
      border-radius: 0.75rem;
      font-weight: 600;
      font-size: 1rem;
      cursor: pointer;
      border: none;
      display: inline-flex;
      align-items: center;
      gap: 0.5rem;
      transition: all 0.2s ease;
    }

    .btn:disabled {
      opacity: 0.5;
      cursor: not-allowed;
      transform: none !important;
    }

    .btn-primary {
      background: linear-gradient(135deg, var(--primary-color), #047857);
      color: white;
      box-shadow: 0 4px 14px 0 rgba(16, 185, 129, 0.39);
    }

    .btn-primary:hover:not(:disabled) {
      box-shadow: 0 6px 20px rgba(16, 185, 129, 0.23);
      transform: translateY(-2px);
    }

    .btn-secondary {
      background-color: rgba(255, 255, 255, 0.1);
      color: white;
      backdrop-filter: blur(10px);
    }

    .btn-secondary:hover:not(:disabled) {
      background-color: rgba(255, 255, 255, 0.15);
    }

    /* Results styling */
    .result-header {
      text-align: center;
      margin-bottom: 3rem;
    }

    .result-title {
      font-size: 2.5rem;
      font-weight: 800;
      background: linear-gradient(135deg, #34d399, #059669);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
      margin-bottom: 1rem;
    }

    .result-subtitle {
      color: var(--text-muted);
      font-size: 1.125rem;
    }

    .recommendation-card {
      background: rgba(31, 41, 55, 0.5);
      border: 1px solid rgba(255, 255, 255, 0.1);
      border-radius: 1rem;
      padding: 2rem;
      margin-bottom: 1.5rem;
      backdrop-filter: blur(10px);
    }

    .recommendation-card h3 {
      margin-top: 0;
      margin-bottom: 1.5rem;
      color: #fff;
      display: flex;
      align-items: center;
      gap: 0.75rem;
      font-size: 1.25rem;
    }

    .setting-item {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 1rem 0;
      border-bottom: 1px solid rgba(255, 255, 255, 0.05);
    }

    .setting-item:last-child {
      border-bottom: none;
      padding-bottom: 0;
    }

    .setting-label {
      color: var(--text-muted);
      font-weight: 500;
    }

    .setting-value {
      color: #fff;
      font-weight: 600;
      background: rgba(16, 185, 129, 0.1);
      padding: 0.25rem 0.75rem;
      border-radius: 0.5rem;
      border: 1px solid rgba(16, 185, 129, 0.2);
    }

    .note-box {
      background: rgba(59, 130, 246, 0.1);
      border-left: 4px solid #3b82f6;
      padding: 1.25rem;
      border-radius: 0 0.75rem 0.75rem 0;
      margin-top: 1.5rem;
    }

    .note-box p {
      margin: 0 0 0.75rem 0;
      color: #e2e8f0;
      line-height: 1.5;
    }
    .note-box p:last-child {
      margin-bottom: 0;
    }
  `;

  @state()
  private currentStep = 0;

  @state()
  private answers: Record<string, string> = {};

  @state()
  private isAnimating = false;

  private get isComplete() {
    return this.currentStep >= questions.length;
  }

  private get progress() {
    if (this.isComplete) return 100;
    return (this.currentStep / questions.length) * 100;
  }

  updated(changedProperties: Map<string, any>) {
    if (changedProperties.has('currentStep')) {
      this.animateStepChange();
      this.animateProgressBar();
    }
  }

  firstUpdated() {
    // Initial animation
    gsap.from(this.shadowRoot!.querySelector('.container'), {
      y: 30,
      opacity: 0,
      duration: 0.8,
      ease: 'power3.out'
    });
    this.animateStepChange();
    this.animateProgressBar();
  }

  private animateProgressBar() {
    const bar = this.shadowRoot!.querySelector('.progress-bar-fill');
    if (bar) {
      gsap.to(bar, {
        width: `${this.progress}%`,
        duration: 0.6,
        ease: 'power2.out'
      });
    }
  }

  private animateStepChange() {
    const steps = this.shadowRoot!.querySelectorAll('.step');
    const activeStep = steps[this.isComplete ? 1 : 0]; // 0 is question, 1 is result

    if (activeStep) {
      gsap.fromTo(activeStep, 
        { opacity: 0, x: 30 },
        { opacity: 1, x: 0, duration: 0.5, ease: 'power3.out', delay: 0.1 }
      );

      // Animate options stagger
      const options = activeStep.querySelectorAll('.option-card');
      if (options.length) {
        gsap.fromTo(options,
          { opacity: 0, y: 20 },
          { opacity: 1, y: 0, duration: 0.4, stagger: 0.1, ease: 'back.out(1.7)', delay: 0.2 }
        );
      }
      
      // Animate result cards stagger
      const resultCards = activeStep.querySelectorAll('.recommendation-card, .note-box');
      if (resultCards.length) {
        gsap.fromTo(resultCards,
          { opacity: 0, y: 20 },
          { opacity: 1, y: 0, duration: 0.5, stagger: 0.15, ease: 'power3.out', delay: 0.2 }
        );
      }
    }
  }

  private handleOptionSelect(questionId: string, value: string) {
    if (this.isAnimating) return;
    
    this.answers = { ...this.answers, [questionId]: value };
    
    // Auto advance
    this.isAnimating = true;
    
    // Add a little pop animation to the selected card
    const selectedCard = this.shadowRoot!.querySelector(`.option-card[data-value="${value}"]`);
    if (selectedCard) {
      gsap.to(selectedCard, {
        scale: 0.95,
        duration: 0.1,
        yoyo: true,
        repeat: 1,
        onComplete: () => {
          setTimeout(() => {
            this.nextStep();
            this.isAnimating = false;
          }, 300);
        }
      });
    } else {
      setTimeout(() => {
        this.nextStep();
        this.isAnimating = false;
      }, 400);
    }
  }

  private nextStep() {
    if (this.currentStep < questions.length) {
      const currentEl = this.shadowRoot!.querySelector('.step.active');
      if (currentEl) {
        gsap.to(currentEl, {
          opacity: 0,
          x: -30,
          duration: 0.3,
          ease: 'power2.in',
          onComplete: () => {
            this.currentStep++;
          }
        });
      } else {
        this.currentStep++;
      }
    }
  }

  private prevStep() {
    if (this.currentStep > 0) {
      const currentEl = this.shadowRoot!.querySelector('.step.active');
      if (currentEl) {
        gsap.to(currentEl, {
          opacity: 0,
          x: 30,
          duration: 0.3,
          ease: 'power2.in',
          onComplete: () => {
            this.currentStep--;
          }
        });
      } else {
        this.currentStep--;
      }
    }
  }

  private restart() {
    const currentEl = this.shadowRoot!.querySelector('.step.active');
    if (currentEl) {
      gsap.to(currentEl, {
        opacity: 0,
        scale: 0.95,
        duration: 0.4,
        ease: 'power2.in',
        onComplete: () => {
          this.answers = {};
          this.currentStep = 0;
        }
      });
    }
  }

  private renderQuestion() {
    const question = questions[this.currentStep];
    const currentAnswer = this.answers[question.id];

    return html`
      <div class="step active">
        <h2 class="step-title">${question.title}</h2>
        <div class="options-grid">
          ${question.options.map(opt => html`
            <div 
              class="option-card ${classMap({ selected: currentAnswer === opt.value })}" 
              data-value="${opt.value}"
              @click=${() => this.handleOptionSelect(question.id, opt.value)}
            >
              <div class="option-icon">${opt.icon}</div>
              <div class="option-title">${opt.label}</div>
              <div class="option-desc">${opt.desc}</div>
            </div>
          `)}
        </div>
      </div>
    `;
  }

  private renderResults() {
    let algo = 'AI Time Based (Heating Power Calibration)';
    let algoDesc = 'Best first choice for most homes. Learns how your room heats up.';
    let tolerance = '0.3°C';
    let windowDelay = '2-5 minutes';
    let notes = [];

    if (this.answers.heatingType === 'underfloor') {
      algo = 'MPC Predictive (Model Predictive Control)';
      algoDesc = 'Best for slow response heating to prevent massive overshoots.';
      tolerance = '0.5°C';
    } else if (this.answers.roomType === 'bathroom' || this.answers.insulation === 'poor' || this.answers.roomSize === 'large') {
      algo = 'Aggressive (Aggressive Target Temperature Calibration)';
      algoDesc = 'Faster warm-up, ideal for rooms that heat slowly or need quick heat bursts.';
    } else if (this.answers.roomType === 'kitchen' || this.answers.roomType === 'hallway') {
      algo = 'PID Controller';
      algoDesc = 'Advanced control with auto-tuning, great for handling strong disturbances like drafts or ovens.';
    } else if (this.answers.insulation === 'good' || this.answers.roomSize === 'small') {
      algo = 'MPC Predictive (Model Predictive Control)';
      algoDesc = 'Prevents overshoots in well-insulated or small rooms that heat up too quickly.';
    }

    if (this.answers.roomType === 'living' && this.answers.heatingType !== 'underfloor') {
      tolerance = '0.1°C - 0.2°C';
    }

    if (this.answers.windowSensor === 'yes') {
      windowDelay = '0 minutes (Immediate)';
    }

    if (this.answers.heatingType === 'underfloor') {
      notes.push(html`ℹ️ <strong>Underfloor Heating:</strong> Changes take a long time to reflect. Give the algorithm a few days to learn the thermal mass of your floor.`);
    }

    return html`
      <div class="step active">
        <div class="result-header">
          <div class="result-title">Optimization Complete</div>
          <div class="result-subtitle">Based on your room profile, here is the recommended configuration.</div>
        </div>

        <div class="recommendation-card">
          <h3>
            <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"></circle><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"></path></svg>
            Core Settings
          </h3>
          <div class="setting-item">
            <span class="setting-label">Calibration Mode</span>
            <span class="setting-value">${algo}</span>
          </div>
          <div class="setting-item">
            <span class="setting-label">Tolerance</span>
            <span class="setting-value">${tolerance}</span>
          </div>
          <div class="setting-item">
            <span class="setting-label">Window Delay</span>
            <span class="setting-value">${windowDelay}</span>
          </div>
        </div>

        <div class="recommendation-card">
          <h3>
            <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z"></path></svg>
            Why this algorithm?
          </h3>
          <p style="color: var(--text-muted); margin: 0; line-height: 1.5;">${algoDesc}</p>
        </div>

        ${notes.length > 0 ? html`
          <div class="note-box">
            ${notes.map(note => html`<p>${note}</p>`)}
          </div>
        ` : ''}
        
        <div style="margin-top: 3rem; text-align: center;">
          <button class="btn btn-secondary" @click=${this.restart}>
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"></path><path d="M3 3v5h5"></path></svg>
            Configure Another Room
          </button>
        </div>
      </div>
    `;
  }

  render() {
    const currentQuestion = questions[this.currentStep];
    const hasAnswer = currentQuestion && this.answers[currentQuestion.id] !== undefined;

    return html`
      <div class="container">
        <div class="content-wrapper">
          <div class="progress-container">
            <div class="progress-bar-bg">
              <div class="progress-bar-fill"></div>
            </div>
            <div class="progress-text">${Math.round(this.progress)}%</div>
          </div>

          <div class="step-container">
            ${!this.isComplete ? this.renderQuestion() : this.renderResults()}
          </div>

          ${!this.isComplete ? html`
            <div class="navigation">
              <button 
                class="btn btn-secondary" 
                @click=${this.prevStep} 
                style="visibility: ${this.currentStep > 0 ? 'visible' : 'hidden'}"
              >
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="19" y1="12" x2="5" y2="12"></line><polyline points="12 19 5 12 12 5"></polyline></svg>
                Back
              </button>
              <button 
                class="btn btn-primary" 
                @click=${this.nextStep} 
                ?disabled=${!hasAnswer}
              >
                ${this.currentStep === questions.length - 1 ? 'Get Recommendation' : 'Next'}
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="5" y1="12" x2="19" y2="12"></line><polyline points="12 5 19 12 12 19"></polyline></svg>
              </button>
            </div>
          ` : ''}
        </div>
      </div>
    `;
  }
}
