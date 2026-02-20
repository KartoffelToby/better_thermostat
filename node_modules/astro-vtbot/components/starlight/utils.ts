export const MAIN_FRAME = 'div.main-frame';
export const MAIN_SECTION = `${MAIN_FRAME} main`;
export const MOBILE_MENU_EXPANDED = 'data-mobile-menu-expanded';
export const MENU_BUTTON = 'starlight-menu-button';
export const SIDEBAR = 'nav.sidebar';
export const SIDEBAR_CONTENT = `${SIDEBAR} .sidebar-content`;
export const SIDEBAR_TOPLEVEL = `${SIDEBAR_CONTENT} .top-level`;
export const LANGUAGE_SELECTOR = 'starlight-lang-select';

/*
 * Returns the the sidebar anchor that best fits the parameter URL.
 */
export function sidebarEntry(url: URL): HTMLAnchorElement | null {
	const normalized = removeTrailingSlash(url.href);
	const target = normalized.split('');
	const anchors = document.querySelectorAll<HTMLAnchorElement>(`${SIDEBAR_CONTENT} a[href^='/']`);
	if (anchors.length === 0) return null;

	const anchorsArray = [...anchors];
	const normalizedArray = anchorsArray.map((anchor) =>
		removeTrailingSlash(new URL(anchor.href, location.href).href)
	);

	return anchorsArray[
		normalizedArray
			.map((href) => href.split('').findIndex((char, index) => char !== target[index]))
			.map((len, idx) =>
				len !== -1
					? len
					: Math.min(normalized.length, normalizedArray[idx].length) +
						(normalized.length === normalizedArray[idx].length ? 1 : 0)
			)
			.reduce((best, current, idx, arr) => (current > arr[best] ? idx : best), 0)
	];
	function removeTrailingSlash(url: string): string {
		return url.replace(/\/#/, '#').replace(/\/$/, '');
	}
}

export function clearCurrentPageMarker() {
	document
		.querySelectorAll(`${SIDEBAR_CONTENT} [aria-current="page"]`)
		?.forEach((el) => el.removeAttribute('aria-current'));
}

export function updateCurrentPageMarker(url: URL) {
	clearCurrentPageMarker();
	sidebarEntry(url)?.setAttribute('aria-current', 'page');
}

export function openCategory(url?: URL, scrollIntoView = true) {
	const currentLink = url
		? sidebarEntry(url)
		: document.querySelector(`${SIDEBAR_CONTENT} [aria-current="page"]`);
	let category = currentLink?.closest('details');
	while (category) {
		category.open = true;
		category = category.parentElement?.closest('details');
	}
	scrollIntoView && currentLink?.scrollIntoView({ block: 'center', behavior: 'instant' });
}
