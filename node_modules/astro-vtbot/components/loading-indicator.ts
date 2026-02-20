let show: () => void;
let hide: () => void;
let initializer: (() => void | Promise<void>) | undefined;

export function loading(newShow: () => void, newHide: () => void, newInit: () => void = () => {}) {
	show = newShow;
	hide = newHide;
	initialize(newInit);
}

const doShow = () => {
	document.documentElement.classList.add('loading');
	show && show();
};

const doHide = () => {
	document.documentElement.classList.remove('loading');
	hide && hide();
};

const doInit = () => {
	initializer && initializer();
};

export function initialize(onPageLoad?: () => void | Promise<void>, lowPrio = false) {
	if (!(initializer && lowPrio)) initializer = onPageLoad;
	document.addEventListener('astro:page-load', doInit);
	document.addEventListener('astro:before-preparation', (e) => {
		doShow();
		const originalLoader = e.loader;
		e.loader = async () => {
			await originalLoader();
			doHide();
		};
	});
}

type Options = {
	src: string | undefined;
	top: string | undefined;
	bottom: string | undefined;
	left: string | undefined;
	right: string | undefined;
};

export async function vtbotLoadingIndicator(options: Options) {
	const loadingIndicator = document.getElementById('vtbot-loading-indicator');
	if (loadingIndicator) return;

	const icons = options.src
		? []
		: document.querySelectorAll<HTMLLinkElement>(`head link[rel*="icon"]`);
	const favicon =
		options.src || (icons.length > 0 && icons[icons.length - 1]?.href) || '/favicon.ico';

	let src = '';
	try {
		if (!(await fetch(favicon)).ok) throw new Error();
	} catch (_) {
		// not ok, or aborted in fetch
		src =
			'data:image/svg+xml,%3Csvg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"%3E%3Ccircle cx="50" cy="50" r="50" fill="%23888" /%3E%3C/svg%3E';
	}

	let img: HTMLImageElement | SVGSVGElement | null = null;

	if (!src) {
		if (favicon?.endsWith('.svg')) {
			const response = await fetch(favicon);
			const text = await response.text();
			const parser = new DOMParser();
			const doc = parser.parseFromString(text, 'image/svg+xml');
			img = doc.querySelector('svg');
		} else {
			src = favicon;
		}
	}
	if (!img) {
		img = document.createElement('img');
		img.src = src;
		img.alt = 'Loading indicator';
	}
	const div = document.createElement('div');

	div.style[options.top || !options.bottom ? 'top' : 'bottom'] =
		options.top || options.bottom || '3vh';
	div.style[options.right || !options.left ? 'right' : 'left'] =
		options.right || options.left || '3vw';

	div.id = 'vtbot-loading-indicator';
	div.appendChild(img!);
	document.getElementById('vtbot-loading-indicator') || document.body.appendChild(div);
}
