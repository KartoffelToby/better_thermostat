import { defineToolbarApp } from 'astro/toolbar';
import { icon  } from "../assets/bag-of-tricks";
import imgSrc from '../assets/chamber.png';

const DTB_TOKEN = 'vtbot-inspection-chamber';
const VTBAG_REOPEN = '#vtbag-ui-reopen';

export default defineToolbarApp({
	init(canvas, app) {

		createVtbotWindow();
		document.addEventListener('astro:after-swap', createVtbotWindow);

		app.addEventListener('placement-updated', (evt) => {
			const windowElement = canvas.querySelector('astro-dev-toolbar-window');
			windowElement && (windowElement.placement = evt instanceof CustomEvent && evt.detail.placement);
		});

		function createVtbotWindow() {
			const astroWindow = document.createElement('astro-dev-toolbar-window');
			astroWindow.insertAdjacentHTML('beforeend', `
<div>
	<astro-dev-toolbar-icon class="logo">${icon}</astro-dev-toolbar-icon>
	<h1>The Bag of Tricks for Astro's View Transitions</h1>
	<p>The Bag offers reusable components, tips & tricks plus detailed info about Astro's view transitions and the View Transition API in general.</p>
	<p>Visit <a href="https://events-3bg.pages.dev">The Bag's Website</a> and <a href="https://github.com/martrapp/astro-vtbot">star the Github project</a> to support this work.</p>
	<h2>The Inspection Chamber</h2>
	<img style="float:right; border-radius: 50%; border: 8px dashed #8888;
		mask-image: radial-gradient(ellipse at center, white 35%, transparent 71%);"
	src=${imgSrc.src} alt="Reopen the Inspection Chamber" />
	<p>Put your view transitions through their paces!</p><hr>
	<p><span id="inspection-chamber-status">If you see this text, bad things happened. If you got here in a web-container, like e.g. Stackblitz, try to open the preview of your project in a new tab. If this does not help, <a href="https://github.com/martrapp/astro-vtbot/issues/new/choose">please file a bug report</a> with the related errors from the browser's console.</span></p><p>You notice a little sign near the Inspection Chamber's power button:</p>
	<astro-dev-toolbar-button id="inspection-chamber-button">Out of Order</astro-dev-toolbar-button>
	<style>
		a {
			color: white;
		}
		astro-dev-toolbar-button {
			width: 20ex;
		}
		astro-dev-toolbar-icon {
			width: 1rem;
			height: 1rem;
			display: inline-block;
		}
		.logo {
			width: 3.3rem;
			height: auto;
			margin-right: 0.25rem;
			transform: translateY(0.4rem);
			float: left;
			display:block;
			vertical-align: top;
		}
		h1 {
			font-size: 1.3rem;
		}
		h2 {
			font-size: 1.2rem;
		}
	</style>
`);
			canvas.append(astroWindow);

		}
		app.onToggled((options) => {
			if (options.state) {
				const me = document.querySelector('astro-dev-toolbar')!
					.shadowRoot!.querySelector('astro-dev-toolbar-app-canvas[data-app-id="vtbot"]')!
					.shadowRoot!;
				const status = me.querySelector<HTMLSpanElement>('#inspection-chamber-status')!;
				const button = me.querySelector<HTMLButtonElement>('#inspection-chamber-button')!;

				if (!document.startViewTransition) {
					status.textContent = 'Your browser does not support view transitions.';
					button.textContent = 'Out of Order';
					button.disabled = true;
				} else if (top!.document.querySelector(VTBAG_REOPEN)) {
					status.textContent = 'The Chamber is currently in standby mode.';
					button.textContent = 'Reactivate';
					button.addEventListener('click', () => {
						top!.sessionStorage.removeItem('vtbag-ui-standby');
						top!.location.reload();
					});
				} else if (top!.document.querySelector('body > #vtbag-main-frame')
					&& top!.sessionStorage.getItem(DTB_TOKEN) !== 'true') {
					status.innerHTML = 'This page has an <code>&lt;InspectionChamber /></code> component.';
					button.textContent = 'Switch to standby mode';
					button.addEventListener('click', () => {
						top!.sessionStorage.setItem('vtbag-ui-standby', 'true');
						top!.location.reload();
					});
				} else if (top!.sessionStorage.getItem(DTB_TOKEN) === 'true') {
					status.textContent = 'Chamber was activated via Dev Toolbar.';
					button.textContent = 'Turn off';

					button.addEventListener('click', () => {
						top!.sessionStorage.removeItem(DTB_TOKEN);
						top!.location.reload();
					});
				} else {
					status.textContent = 'There is an Inspection Chamber here.';
					button.textContent = 'Power up';
					button.addEventListener('click', () => {
						top!.sessionStorage.removeItem('vtbag-ui-closed');
						top!.sessionStorage.setItem(DTB_TOKEN, 'true');
						top!.location.reload();
					});
				}
			}
		});
	}
});
